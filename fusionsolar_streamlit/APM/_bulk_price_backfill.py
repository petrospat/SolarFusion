"""
Price-only bulk backfill: populate dam_prices for every day from project
activation (2025-09-30) through the last elapsed day (2026-08-26), using
ENTSO-E for dates before HEnEx's archive starts (2026-01-01) and HEnEx from
then on. No FusionSolar/production calls at all — this is independent of
the Huawei rate-limit issue, so daily_revenue/revenue_15min are NOT touched
here; that requires production data and stays blocked until FusionSolar is
available again.

Reuses the real apm_app.py functions (AST-extracted, not retyped) —
_fetch_dam_daily_uncached and _fetch_henex_dam_uncached already persist to
dam_prices as a side effect on success, so this script just needs to call
them for each day; no separate persistence step.

Usage: python _bulk_price_backfill.py
"""
import ast
import io
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
END_DATE_EXCLUSIVE = date(2026, 8, 27)
HENEX_CUTOFF = date(2026, 1, 1)
DB_PATH = "apm_data.db"
BIDDING_ZONE = "10YGR-HTSO-----Y"

_src = open("apm_app.py", encoding="utf-8").read()
_tree = ast.parse(_src)
def _extract(name):
    return next(ast.get_source_segment(_src, n) for n in _tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name)

with open(".streamlit/secrets.toml", "rb") as f:
    _secrets = tomllib.load(f)
_proxies = {"https": _secrets["proxy"]["https"], "http": _secrets["proxy"]["https"]} if "proxy" in _secrets else None
_entsoe_token = _secrets["entsoe"]["api_key"]

_ns = {
    "pd": pd, "requests": requests, "io": io, "openpyxl": openpyxl,
    "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
    "ZoneInfo": ZoneInfo, "PLANT_TZ": "Europe/Athens",
    "ENTSOE_API": "https://web-api.tp.entsoe.eu/api", "ENTSOE_ZONE": BIDDING_ZONE,
    "_ENTSOE_SESSION": requests.Session(),
    "_get_proxies": lambda: _proxies,
    "_redact_token": lambda s, t: s.replace(t, "***") if t else s,
    "Tuple": tuple, "Optional": Optional, "date": date,
    "sqlite3": sqlite3, "DB_PATH": DB_PATH,
}
for _name in ("_get_db", "_persist_dam_prices", "_fetch_dam_daily_uncached", "_fetch_henex_dam_uncached"):
    exec(compile(_extract(_name), f"<{_name}>", "exec"), _ns)

_fetch_dam_daily_uncached = _ns["_fetch_dam_daily_uncached"]
_fetch_henex_dam_uncached = _ns["_fetch_henex_dam_uncached"]


def _day_is_complete(d: date) -> bool:
    """
    Mirrors apm_app.py's _incomplete_day threshold, read straight from
    dam_prices. Must use the ATHENS-LOCAL day boundary, not UTC calendar
    day: MTU=1 is anchored at 01:00 Athens local (see _fetch_henex_dam_uncached),
    so a day's data spans roughly [D-1 21:00-22:00 UTC, D 21:45-22:45 UTC] —
    a naive UTC-midnight window only catches ~87-91 of the 96 periods,
    intermittently mis-flagging complete days as incomplete depending on
    the season's UTC offset.
    """
    day_start = pd.Timestamp(d, tz="Europe/Athens").tz_convert("UTC")
    day_end = day_start + timedelta(days=1)
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM dam_prices WHERE bidding_zone=? AND timestamp_utc>=? AND timestamp_utc<? AND price_eur_mwh IS NOT NULL",
        (BIDDING_ZONE, day_start.isoformat(), day_end.isoformat())).fetchone()[0]
    conn.close()
    return n >= 90   # tolerant of DST-short days (92); anything much less is a real gap


def fetch_one_day(d: date):
    if d >= HENEX_CUTOFF:
        _, err = _fetch_henex_dam_uncached(d)
        return err, "HEnEx"
    _, err = _fetch_dam_daily_uncached(d, _entsoe_token)
    return err, "ENTSO-E"


def main():
    days = []
    d = START_DATE
    while d < END_DATE_EXCLUSIVE:
        days.append(d)
        d += timedelta(days=1)

    todo = [d for d in days if not _day_is_complete(d)]
    print(f"{len(days)} total days, {len(days) - len(todo)} already complete, {len(todo)} to fetch", flush=True)

    ok, failed = 0, []
    for i, d in enumerate(todo):
        err, src = fetch_one_day(d)
        if err:
            print(f"[{i+1}/{len(todo)}] {d} ({src})  FAILED  {err}", flush=True)
            failed.append((d, src, err))
        else:
            ok += 1
            print(f"[{i+1}/{len(todo)}] {d} ({src})  OK", flush=True)
        time.sleep(1)

    print(f"\nDone. {ok}/{len(todo)} fetched successfully. {len(failed)} failed.", flush=True)
    if failed:
        print("Failed days:")
        for d, src, err in failed:
            print(f"  {d} ({src}): {err}")


if __name__ == "__main__":
    main()
