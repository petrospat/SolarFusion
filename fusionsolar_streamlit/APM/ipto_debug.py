"""
ipto_debug.py  —  ADMIE FileCategory discovery
================================================
Tries every known ADMIE FileCategory for 17/02/2026 (and 16/02 as fallback),
downloads each Excel file, and scores it against the ENTSO-E reference SMP:

  Periods 4-10  (01:00-02:45):  2.57 | 0.01 | 0.01 | 0.01 | 0.01 | 0.01 | 39.96

Run:
    python ipto_debug.py

Saves full output to ipto_debug_output.txt — paste it into the chat.
"""

import io, json
from datetime import date, timedelta
from pathlib import Path

import requests
import pandas as pd

requests.packages.urllib3.disable_warnings()

# ── Reference values (ENTSO-E verified) ──────────────────────────────────────
ENTSO_REF    = [2.57, 0.01, 0.01, 0.01, 0.01, 0.01, 39.96]  # periods 4-10
SIGNATURE    = {2.57, 39.96}   # distinctive values — must both appear to be a winner

ADMIE_API    = "https://www.admie.gr/getOperationMarketFile"
TARGET_DATE  = date(2026, 2, 17)
TIMEOUT      = 25

ALL_CATEGORIES = [
    "DAM_ResultsSummary",
    "ISP1ISPResults",
    "ISP2ISPResults",
    "ISP3ISPResults",
    "ISP4ISPResults",
    "ISP1DayAheadSchedulingResults",
    "ISP2DayAheadSchedulingResults",
    "DASPResults",
    "ISPResults",
    "ISP1MarketResults",
    "ISP2MarketResults",
    "MarketResults",
    "SMPResults",
    "EnergyPrices",
    "DayAheadResults",
    "IntraDayResults",
    "BalancingResults",
    "ImbalancePrices",
    "ISP1ImbalanceResults",
    "ISP2ImbalanceResults",
    "ISPImbalancePrices",
    "ISP1ActivatedEnergy",
    "RealTimeSchedulingResults",
]

OUTPUT = []

def log(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    OUTPUT.append(line)


def get_url(entry):
    furl = entry.get("file_path") or entry.get("url") or entry.get("link")
    if not furl:
        for v in entry.values():
            if isinstance(v, str) and (v.startswith("http") or ".xls" in v):
                return v
    return furl


def search_excel(xl: dict):
    """
    Search every sheet in both row-format and column-format for signature values.
    Returns list of (score, description, row_or_col_values) tuples.
    """
    hits = []
    for sheet, df in xl.items():
        # --- Column format: look for a column containing 39.96 AND 2.57 ---
        for col in df.columns:
            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            v = vals.tolist()
            has_3996 = any(abs(x - 39.96) < 0.05 for x in v)
            has_257  = any(abs(x - 2.57)  < 0.05 for x in v)
            if has_3996 and has_257:
                hits.append((100, f"COLUMN format | sheet='{sheet}' col='{col}'", v))
            elif has_3996 or has_257:
                hits.append((50,  f"PARTIAL column | sheet='{sheet}' col='{col}'", v))

        # --- Row format: look for a row where cols 1..N contain signature ---
        if df.shape[1] >= 20:
            for _, row in df.iterrows():
                label = str(row.iloc[0])
                rvals = pd.to_numeric(row.iloc[1:], errors="coerce").dropna()
                v = rvals.tolist()
                has_3996 = any(abs(x - 39.96) < 0.05 for x in v)
                has_257  = any(abs(x - 2.57)  < 0.05 for x in v)
                if has_3996 and has_257:
                    hits.append((100, f"ROW format | sheet='{sheet}' row_label='{label}'", v))
                elif has_3996 or has_257:
                    hits.append((50,  f"PARTIAL row | sheet='{sheet}' row_label='{label}'", v))

    return sorted(hits, key=lambda x: -x[0])


# ── Step 0: reachability ─────────────────────────────────────────────────────
log("=" * 70)
log(f"ADMIE FileCategory Discovery  |  target: {TARGET_DATE}")
log(f"ENTSO-E signature: 2.57 and 39.96 must both appear")
log("=" * 70)

try:
    r0 = requests.get("https://www.admie.gr/", timeout=10, verify=False)
    log(f"\n[0] admie.gr: HTTP {r0.status_code}  -> reachable")
except Exception as e:
    log(f"\n[0] UNREACHABLE: {e}")
    raise SystemExit(1)

# ── Step 1: try all categories ───────────────────────────────────────────────
winners = []
dates_to_try = [
    TARGET_DATE.strftime("%Y-%m-%d"),
    (TARGET_DATE - timedelta(days=1)).strftime("%Y-%m-%d"),
]

for fc in ALL_CATEGORIES:
    log(f"\n{'─'*60}")
    log(f"FileCategory: {fc}")

    for ds in dates_to_try:
        try:
            r = requests.get(ADMIE_API,
                params={"dateStart": ds, "dateEnd": ds, "FileCategory": fc},
                timeout=TIMEOUT, verify=False)

            if r.status_code != 200:
                log(f"  [{ds}] HTTP {r.status_code} — skip")
                continue

            j = r.json()
            if not j:
                log(f"  [{ds}] empty list")
                continue

            log(f"  [{ds}] {len(j)} file(s)  keys={list(j[-1].keys())}")

            furl = get_url(j[-1])
            if not furl:
                log(f"  [{ds}] no URL found in entry")
                continue

            log(f"  [{ds}] downloading {furl[-70:]}")
            fd = requests.get(furl, timeout=TIMEOUT, verify=False)
            if fd.status_code != 200:
                log(f"  [{ds}] download HTTP {fd.status_code}")
                continue

            log(f"  [{ds}] {len(fd.content):,} bytes")

            xl = pd.read_excel(io.BytesIO(fd.content), sheet_name=None)
            log(f"  [{ds}] sheets: {list(xl.keys())}")

            hits = search_excel(xl)
            if hits:
                for score, desc, vals in hits:
                    log(f"  [{ds}] SCORE={score}  {desc}")
                    log(f"         sample values (first 12): {[round(x,2) for x in vals[:12]]}")
                    if score == 100:
                        winners.append({
                            "fc": fc, "date": ds, "desc": desc,
                            "vals": vals, "url": furl
                        })
            else:
                log(f"  [{ds}] no match — sheets had no 2.57 or 39.96 values")

        except Exception as e:
            log(f"  [{ds}] ERROR: {type(e).__name__}: {str(e)[:120]}")

# ── Summary ──────────────────────────────────────────────────────────────────
log(f"\n{'='*70}")
log("WINNERS  (score=100, both 2.57 AND 39.96 found):")
log(f"{'='*70}")

if winners:
    for w in winners:
        log(f"\n  FileCategory : {w['fc']}")
        log(f"  Date used    : {w['date']}")
        log(f"  Match        : {w['desc']}")
        log(f"  Values[0:12] : {[round(x,2) for x in w['vals'][:12]]}")
        log(f"  File URL     : {w['url']}")

        # Try to align periods 4-10 with ENTSO-E reference
        v = w["vals"]
        if len(v) >= 11:
            # Look for 2.57 to find the offset
            for offset in range(len(v) - 6):
                if abs(v[offset] - 2.57) < 0.05:
                    candidate = [round(v[offset+i], 2) for i in range(7)]
                    match = sum(1 for a, b in zip(candidate, ENTSO_REF)
                                if abs(a - b) < 0.5)
                    log(f"  Period alignment at offset {offset}: {candidate}")
                    log(f"  ENTSO-E ref:                         {ENTSO_REF}")
                    log(f"  Matching periods: {match}/7")
                    break
else:
    log("\n  NONE — signature values not found in any FileCategory.")
    log()
    log("  Next step: visit https://www.admie.gr/en/market/market-statistics")
    log(f"  Find the SMP/price file for {TARGET_DATE} manually.")
    log("  Right-click the download link, copy the URL, and note the")
    log("  'FileCategory' parameter in the URL or filename.")

log(f"\n{'='*70}")
log("Done.")
log(f"{'='*70}")

Path("ipto_debug_output.txt").write_text("\n".join(OUTPUT), encoding="utf-8")
print("\nSaved to: ipto_debug_output.txt")