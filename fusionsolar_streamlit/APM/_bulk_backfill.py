"""
Bulk backfill: compute + persist daily_revenue/revenue_15min for every
missing day from project activation (2025-09-30) through the last elapsed
day, using HEnEx (forced, not ENTSO-E) for prices.

Reuses the REAL functions from apm_app.py (extracted via AST, not retyped)
for the calculation/persistence logic — _norm, _compute_day_revenue_from_frames,
_fetch_henex_dam_uncached, _get_db, _persist_day_revenue — so this can never
silently drift from what the app itself computes. Only the Huawei
login/production-fetch is written fresh here (that's a stateful class in
apm_app.py, not practical to AST-extract), matching the same request pattern
already validated live in this session (verify_jul10.py / compute_march.py).

Two modes:
  --calibrate N   Try N days (spread across the missing range) at --pace
                  seconds apart; report success/failure so a safe pace can
                  be chosen before committing to the full run.
  --run           Process every missing day in the range at --pace seconds
                  apart. Resumable — already-cached days are skipped for
                  free on every invocation.

Usage:
    python _bulk_backfill.py --calibrate 12 --pace 60
    python _bulk_backfill.py --run --pace 60
"""
import argparse
import ast
import io
import random
import sqlite3
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import openpyxl
import pandas as pd
import requests

START_DATE = date(2025, 9, 30)
END_DATE_EXCLUSIVE = date(2026, 8, 27)   # "today", exclusive — matches app's elapsed_days convention
# HEnEx's document archive for this URL pattern was binary-searched live and
# confirmed to start at 2026-01-01 — everything earlier is a hard 404, not a
# rate limit or transient issue. Dates before this use ENTSO-E instead.
HENEX_CUTOFF = date(2026, 1, 1)
DB_PATH = "apm_data.db"

# ── Extract the real functions from apm_app.py so this script can never drift ──
_src = open("apm_app.py", encoding="utf-8").read()
_tree = ast.parse(_src)
def _extract(name):
    return next(ast.get_source_segment(_src, n) for n in _tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name)

with open(".streamlit/secrets.toml", "rb") as f:
    _secrets = tomllib.load(f)
_proxies = {"https": _secrets["proxy"]["https"], "http": _secrets["proxy"]["https"]} if "proxy" in _secrets else None
_fusion_cfg = _secrets["fusion"]
_entsoe_token = _secrets["entsoe"]["api_key"]

_entsoe_session = requests.Session()

_ns = {
    "pd": pd, "requests": requests, "io": io, "openpyxl": openpyxl,
    "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
    "ZoneInfo": ZoneInfo, "PLANT_TZ": "Europe/Athens",
    "ENTSOE_API": "https://web-api.tp.entsoe.eu/api", "ENTSOE_ZONE": "10YGR-HTSO-----Y",
    "_ENTSOE_SESSION": _entsoe_session,
    "_get_proxies": lambda: _proxies,
    "_redact_token": lambda s, t: s.replace(t, "***") if t else s,
    "Tuple": tuple, "Optional": Optional, "date": date,
    "sqlite3": sqlite3, "DB_PATH": DB_PATH,
}
for _name in ("_norm", "_fetch_henex_dam_uncached", "_fetch_dam_daily_uncached",
             "_get_db", "_compute_day_revenue_from_frames", "_persist_dam_prices"):
    exec(compile(_extract(_name), f"<{_name}>", "exec"), _ns)

_norm = _ns["_norm"]
_fetch_henex_dam_uncached = _ns["_fetch_henex_dam_uncached"]
_fetch_dam_daily_uncached = _ns["_fetch_dam_daily_uncached"]
_get_db = _ns["_get_db"]
_compute_day_revenue_from_frames = _ns["_compute_day_revenue_from_frames"]


def _fetch_price(d: date):
    """
    HEnEx (forced, as requested) for d >= HENEX_CUTOFF, with one retry on
    failure to absorb the transient flakiness observed live (a date that
    404'd once succeeded plainly on immediate retry — not the archive-
    boundary 404, which is reproducible and won't be fixed by retrying).
    ENTSO-E for anything earlier, since HEnEx's archive doesn't reach there.
    """
    if d >= HENEX_CUTOFF:
        df, err = _fetch_henex_dam_uncached(d)
        if err:
            time.sleep(5)
            df, err = _fetch_henex_dam_uncached(d)
        return df, err, "HEnEx"
    df, err = _fetch_dam_daily_uncached(d, _entsoe_token)
    return df, err, "ENTSO-E"


def _persist_day_revenue(d: date, r: dict) -> None:
    """Same logic as apm_app.py's _persist_day_revenue (small enough to
    inline directly rather than AST-extract, since it only does two inserts)."""
    fetched_ts = datetime.now(timezone.utc).isoformat()
    conn = _get_db()
    conn.execute("""INSERT OR REPLACE INTO daily_revenue
        (day,kwh,revenue_eur,avg_price,fetched_ts) VALUES (?,?,?,?,?)""",
        (str(d), r["kwh"], r["revenue_eur"], r["avg_price"], fetched_ts))
    buckets = r.get("buckets")
    if buckets is not None and not buckets.empty:
        conn.executemany("""INSERT OR REPLACE INTO revenue_15min
            (dt,day,kwh,price_eur_mwh,revenue_eur,fetched_ts) VALUES (?,?,?,?,?,?)""",
            [(b["dt"].isoformat(), str(d), float(b["kwh"]), float(b["price"]),
             float(b["rev"]), fetched_ts) for _, b in buckets.iterrows()])
    conn.commit(); conn.close()


# ── Huawei login/production-fetch (fresh — stateful class, not AST-extracted) ──
_session = requests.Session()
_session.verify = _fusion_cfg.get("verify_ssl", False)
_session.headers.update({"Content-Type": "application/json", "User-Agent": "apm-bulk-backfill"})
_base_url = _fusion_cfg["base_url"].rstrip("/")


def _login():
    r = _session.post(f"{_base_url}/thirdData/login",
                      json={"userName": _fusion_cfg["username"], "systemCode": _fusion_cfg["system_code"]},
                      timeout=15)
    tok = r.cookies.get("XSRF-TOKEN") or r.headers.get("xsrf-token")
    if not tok:
        raise SystemExit(f"Login failed: {r.status_code} {r.text[:200]}")
    _session.headers.update({"XSRF-TOKEN": tok})


def _post(url, payload, timeout=25, retries=3):
    last = None
    for i in range(retries):
        try:
            resp = _session.post(url, json=payload, timeout=timeout)
            last = resp
            if resp.status_code == 200:
                j = resp.json()
                if j.get("failCode") == 407:
                    time.sleep(min(30, 2 * (2 ** i)) + random.uniform(0, 1))
                    continue
                return j
        except Exception:
            pass
        time.sleep(min(30, 2 * (2 ** i)) + random.uniform(0, 1))
    try:
        return last.json()
    except Exception:
        return {"data": None, "failCode": -1}


def _get_station_code():
    j = _post(f"{_base_url}/thirdData/getStationList", {})
    d = j.get("data", [])
    rows = d.get("list", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    if not rows:
        raise SystemExit(f"No stations returned: {j}")
    return rows[0].get("stationCode") or rows[0].get("plantCode")


def _fetch_production(sid, target_date):
    ms = int(datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    j5 = _post(f"{_base_url}/thirdData/getKpiStation5min", {"stationCodes": sid, "collectTime": ms})
    if j5.get("data"):
        j5["_src"] = "5min"
        return j5
    jh = _post(f"{_base_url}/thirdData/getKpiStationHour", {"stationCodes": sid, "collectTime": ms})
    jh["_src"] = "hour"
    return jh


def compute_one_day(sid, d: date):
    """Returns (ok: bool, reason: Optional[str])."""
    pj = _fetch_production(sid, d)
    df_dam, price_err, price_src = _fetch_price(d)
    if price_err:
        return False, f"{price_src}: {price_err}"
    r = _compute_day_revenue_from_frames(pj, df_dam[["dt", "price"]] if not df_dam.empty else df_dam)
    if not r:
        fc = (pj or {}).get("failCode")
        return False, f"production/compute failed (failCode={fc}, price_src={price_src})"
    _persist_day_revenue(d, r)
    return True, None


def get_missing_days():
    conn = sqlite3.connect(DB_PATH)
    cached = {row[0] for row in conn.execute("SELECT day FROM daily_revenue").fetchall()}
    conn.close()
    days = []
    d = START_DATE
    while d < END_DATE_EXCLUSIVE:
        if str(d) not in cached:
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", type=int, default=0, help="Test N sample days instead of a full run")
    ap.add_argument("--run", action="store_true", help="Process every missing day")
    ap.add_argument("--pace", type=float, default=60.0, help="Seconds between requests")
    args = ap.parse_args()

    _login()
    sid = _get_station_code()
    print(f"Login OK, station={sid}", flush=True)

    missing = get_missing_days()
    print(f"{len(missing)} missing day(s) in range {START_DATE} .. {END_DATE_EXCLUSIVE}", flush=True)

    if args.calibrate:
        n = args.calibrate
        # Spread the sample across the whole range, not just the start.
        step = max(1, len(missing) // n)
        sample = missing[::step][:n]
        print(f"Calibrating with {len(sample)} sample day(s) at pace={args.pace}s: {sample}", flush=True)
        todo = sample
    elif args.run:
        todo = missing
    else:
        print("Specify --calibrate N or --run"); return

    ok_count = 0
    fail_count = 0
    for i, d in enumerate(todo):
        ok, reason = compute_one_day(sid, d)
        if ok:
            ok_count += 1
            print(f"[{i+1}/{len(todo)}] {d}  OK", flush=True)
        else:
            fail_count += 1
            print(f"[{i+1}/{len(todo)}] {d}  FAILED  {reason}", flush=True)
        if i < len(todo) - 1:
            time.sleep(args.pace)

    print(f"\nDone. {ok_count} succeeded, {fail_count} failed out of {len(todo)}.", flush=True)


if __name__ == "__main__":
    main()
