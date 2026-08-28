# ipto_fallback.py
"""
SUPERSEDED — a genuine live secondary source was found; see below.
Kept only for its harmonization/upsert logic and as investigation history.

Originally written as an IPTO/ADMIE Day-Ahead Market price fallback for when
ENTSO-E is unreachable. Live investigation against the real endpoints (see
conversation history, 2026-08-27) found the IPTO premise was wrong:

INVESTIGATION FINDINGS (IPTO/ADMIE — dead end)
  - IPTO/ADMIE's file-metadata endpoint (`getOperationMarketFile`) is real
    and reachable, and the params documented in this file's original
    docstring (`dateStart`/`dateEnd`, `YYYY-MM-DD`) do work — but there is
    NO day-ahead-market-price file in its catalog under any FileCategory or
    file_process value found. IPTO/ADMIE is Greece's Transmission System
    Operator (grid operation, balancing, Intraday Scheduling/ISP, RES
    metering); it does not publish day-ahead clearing prices at all.

RESOLVED — Hellenic Energy Exchange (HEnEx/EnEx Group) IS a working source
  - HEnEx's public web dashboard is a client-rendered React app (data not
    reachable via plain HTTP GET) and their documented API (ETSS) is a
    certified-trading-member-only order-execution interface — both dead
    ends, same as this file first assumed for the whole organization.
  - BUT: emailing trading@enexgroup.gr directly got a documented, public,
    unauthenticated URL for automated daily downloads:
        https://www.enexgroup.gr/documents/20126/366820/
            {YYYYMMDD}_EL-DAM_ResultsSummary_EN_v{01}.xlsx
    Sheet "SPOT_Summary (SELL)", row "Greece Mainland (15min MCP)" holds the
    15-min day-ahead clearing price. Verified live against ENTSO-E for the
    same day: R²=1.0, zero difference across all matched 15-min prices,
    including correct handling of a DST-transition day (92 MTUs).
  - One quirk found only by testing, not documented anywhere: despite the
    sheet's date cell showing midnight, MTU=1 is actually 01:00 Athens
    local time, not 00:00 (legacy 1-24 hour-labelling convention). Anchor
    at local 01:00, not local midnight.

WHAT'S ACTUALLY WIRED INTO apm_app.py NOW:
  `_fetch_dam_daily_with_fallback()` tries, in order: (1) ENTSO-E live,
  (2) HEnEx live (`_fetch_henex_dam_uncached` — a real second independent
  source, not a cache), (3) the durable local `dam_prices` journal
  (`_persist_dam_prices`/`_read_cached_dam_prices`) for whatever was last
  successfully fetched from either source. This gives genuine redundancy,
  not just resilience-via-caching as originally concluded.

This file's parsing (defensive header/column detection, DST-safe UTC
timestamp construction via a fixed local-time anchor + elapsed-time layout)
and upsert (source-priority ON CONFLICT) logic remain correct and were the
template for the HEnEx implementation — reuse them if IPTO ever adds a
genuine DAM file, or for any other Excel-distributed market data source.

Requires: pandas, requests, openpyxl (`pip install openpyxl` if missing —
pandas needs it to read .xlsx).

CLI usage:
    python ipto_fallback.py --date 2026-03-10
    python ipto_fallback.py --start 2026-03-01 --end 2026-03-07
    python ipto_fallback.py --date 2026-03-10 --inspect     # debug parsing, no DB write
    python ipto_fallback.py --date 2026-03-10 --dry-run      # parse + print, no DB write

Library usage (as a fallback inside another app):
    from ipto_fallback import ensure_dam_prices

    df = ensure_dam_prices(date(2026, 3, 10), entsoe_fetch=my_entsoe_fetch_fn)
"""
from __future__ import annotations

import argparse
import io
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

log = logging.getLogger("ipto_fallback")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
ATHENS_TZ = ZoneInfo("Europe/Athens")
DEFAULT_BIDDING_ZONE = "10YGR-HTSO-----Y"          # Greece, same EIC apm_app.py uses
DEFAULT_DB_PATH = str(Path(__file__).with_name("apm_data.db"))

IPTO_INDEX_URL = "https://www.admie.gr/getOperationMarketFile"
IPTO_FILE_CATEGORY = "DAM"
REQUEST_TIMEOUT = 30

# Candidate header keywords for locating columns in the IPTO workbook.
# Matched case-insensitively against Unicode-normalized (accent-stripped)
# text, so "Τιμή Εκκαθάρισης" and "TIMH EKKATHARISHS" both match "τιμη".
PRICE_COL_KEYWORDS = [
    "τιμη εκκαθαρισησ", "τιμη αγορασ", "mcp", "market clearing price",
    "clearing price", "smp", "system marginal price", "eur/mwh", "price",
]
PERIOD_COL_KEYWORDS = [
    "ωρα", "hour", "delivery period", "χρονικη περιοδοσ", "period", "διαστημα",
]

DAM_PRICES_DDL = """
CREATE TABLE IF NOT EXISTS dam_prices (
    timestamp_utc  TEXT    NOT NULL,   -- ISO-8601 UTC, e.g. 2026-03-10T05:00:00+00:00
    bidding_zone   TEXT    NOT NULL,   -- EIC code, e.g. 10YGR-HTSO-----Y
    price_eur_mwh  REAL,
    resolution_min INTEGER,            -- 15 or 60
    source         TEXT    NOT NULL,   -- 'ENTSOE' or 'IPTO'
    fetched_ts     TEXT    NOT NULL,
    PRIMARY KEY (timestamp_utc, bidding_zone)
)
"""


@dataclass
class DamPriceRow:
    timestamp_utc: datetime
    price_eur_mwh: float
    resolution_min: int


# ─────────────────────────────────────────────────────────────────────────────
# NETWORK HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _load_proxies() -> Optional[dict]:
    """
    Best-effort proxy config: reuse the same [proxy] secrets.toml section
    apm_app.py uses, if present, so this script works unmodified in the same
    corporate-network environment. Falls back to no explicit proxy (requests
    will still honour HTTPS_PROXY/HTTP_PROXY env vars on its own).
    """
    secrets_path = Path(__file__).with_name(".streamlit") / "secrets.toml"
    if not secrets_path.exists():
        return None
    try:
        import tomllib
        with open(secrets_path, "rb") as f:
            secrets = tomllib.load(f)
        https_proxy = secrets.get("proxy", {}).get("https")
        if https_proxy:
            return {"https": https_proxy, "http": https_proxy}
    except Exception as e:
        log.debug("Could not read proxy config from secrets.toml: %s", e)
    return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "apm-ipto-fallback/1.0"})
    return s


# ─────────────────────────────────────────────────────────────────────────────
# 1. IPTO FILE DISCOVERY + DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────
def fetch_ipto_file_index(target_date: date, session: Optional[requests.Session] = None) -> List[dict]:
    """
    Query the ADMIE file-metadata endpoint for DAM result files published on
    `target_date`. Returns the raw list of file-descriptor dicts from the
    JSON response — deliberately not narrowed to specific keys yet, since
    the exact field names should be confirmed with --inspect before relying
    on them.
    """
    session = session or _session()
    params = {
        "dateStart": target_date.strftime("%Y-%m-%d"),
        "dateEnd": target_date.strftime("%Y-%m-%d"),
        "FileCategory": IPTO_FILE_CATEGORY,
    }
    r = session.get(IPTO_INDEX_URL, params=params, timeout=REQUEST_TIMEOUT,
                    proxies=_load_proxies(), verify=True)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        # Some ADMIE endpoints wrap the list under a key like "data"/"files".
        for key in ("data", "files", "result", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected IPTO index response shape: {type(data)}")


def _extract_file_url(entry: dict) -> Optional[str]:
    """
    Defensive key lookup for the file's download URL — tries the field
    names most commonly seen across ADMIE's file-index endpoints. Add to
    this list (after checking with --inspect) if none match for your file
    category.
    """
    candidate_keys = ["file_path", "filePath", "FilePath", "path", "Path",
                      "url", "Url", "URL", "fileUrl", "FileUrl", "download_url"]
    for key in candidate_keys:
        val = entry.get(key)
        if val:
            return val if str(val).startswith("http") else f"https://www.admie.gr{val}"
    return None


def download_ipto_excel(url: str, session: Optional[requests.Session] = None) -> bytes:
    session = session or _session()
    r = session.get(url, timeout=REQUEST_TIMEOUT, proxies=_load_proxies(), verify=True)
    r.raise_for_status()
    return r.content


# ─────────────────────────────────────────────────────────────────────────────
# 2. HARMONIZATION: EXCEL PARSING → UTC-ALIGNED PRICE SERIES
# ─────────────────────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    """Lowercase + strip accents, so Greek diacritics don't break keyword matching."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.strip().lower()


def _find_header_row(raw: pd.DataFrame, max_scan_rows: int = 20) -> int:
    """
    Real ADMIE workbooks typically have a few title/metadata rows before the
    actual table header. Scan the first `max_scan_rows` rows for the one
    that contains a recognizable price-column keyword.
    """
    all_keywords = PRICE_COL_KEYWORDS + PERIOD_COL_KEYWORDS
    for i in range(min(max_scan_rows, len(raw))):
        row_text = " ".join(_normalize(v) for v in raw.iloc[i].tolist() if pd.notna(v))
        if any(kw in row_text for kw in all_keywords):
            return i
    raise ValueError(
        "Could not locate a header row in the IPTO workbook — none of the "
        f"first {max_scan_rows} rows matched known price/period keywords. "
        "Run with --inspect to see the raw sheet and add the real header "
        "text to PRICE_COL_KEYWORDS/PERIOD_COL_KEYWORDS."
    )


def _resolve_column(columns: List[str], keywords: List[str]) -> Optional[str]:
    normalized = {c: _normalize(c) for c in columns}
    for kw in keywords:
        for orig, norm in normalized.items():
            if kw in norm:
                return orig
    return None


def _parse_price_value(v) -> Optional[float]:
    """Handles European decimal-comma formatting (e.g. '45,23') as well as plain floats."""
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("€", "").replace(" ", "")
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")   # "1.234,56" style
    try:
        return float(s)
    except ValueError:
        return None


def parse_ipto_dam_excel(xlsx_bytes: bytes, target_date: date,
                         inspect: bool = False) -> pd.DataFrame:
    """
    Parse one day's IPTO DAM workbook into a DataFrame[timestamp_utc,
    price_eur_mwh, resolution_min].

    Timezone handling: IPTO prints local (Europe/Athens) hour labels, which
    are ambiguous on the two DST-transition days each year (23 or 25 rows
    instead of 24). Rather than trying to parse and disambiguate each local
    label — fragile exactly on the days it matters most — this anchors on
    local midnight (never ambiguous) converted once to UTC, then lays the
    day's N rows out as evenly-spaced absolute time from that anchor. That
    is correct by construction for however many periods the file actually
    contains, DST or not, since elapsed real time from a fixed UTC instant
    is unambiguous regardless of local labels.
    """
    raw = pd.read_excel(io.BytesIO(xlsx_bytes), sheet_name=0, header=None, engine="openpyxl")

    header_row = _find_header_row(raw)
    df = raw.iloc[header_row + 1:].copy()
    df.columns = [str(c) for c in raw.iloc[header_row].tolist()]
    df = df.dropna(how="all")

    price_col = _resolve_column(list(df.columns), PRICE_COL_KEYWORDS)
    period_col = _resolve_column(list(df.columns), PERIOD_COL_KEYWORDS)

    if inspect:
        print(f"[inspect] header row index: {header_row}")
        print(f"[inspect] columns found: {list(df.columns)}")
        print(f"[inspect] resolved price column: {price_col!r}")
        print(f"[inspect] resolved period column: {period_col!r}")
        print(f"[inspect] first 5 data rows:\n{df.head()}")

    if price_col is None:
        raise ValueError(
            f"Could not resolve a price column among {list(df.columns)} using "
            f"keywords {PRICE_COL_KEYWORDS} — inspect the sheet with --inspect "
            "and extend PRICE_COL_KEYWORDS."
        )

    prices = df[price_col].map(_parse_price_value)
    # Keep only rows that actually parsed to a price — title/footer/blank
    # rows commonly survive the header slice and would otherwise become
    # spurious NaN periods.
    valid = prices.notna()
    prices = prices[valid].reset_index(drop=True)
    n_rows = len(prices)
    if n_rows == 0:
        raise ValueError("No valid price rows parsed from the IPTO workbook.")

    # 92/96/100 ⇒ 15-min resolution (100 covers a padded/duplicated edge
    # case some vintages of the file have on DST-changeover days);
    # 23/24/25 ⇒ hourly. Anything else: best-effort round to the nearest.
    if n_rows in (92, 96, 100):
        resolution_min = 15
    elif n_rows in (23, 24, 25):
        resolution_min = 60
    else:
        resolution_min = max(5, round(1440 / n_rows / 5) * 5)
        log.warning("Unexpected row count %d for %s — inferring resolution=%dmin",
                   n_rows, target_date, resolution_min)

    local_midnight = datetime(target_date.year, target_date.month, target_date.day,
                              0, 0, tzinfo=ATHENS_TZ)
    utc_midnight = local_midnight.astimezone(timezone.utc)

    timestamps_utc = [utc_midnight + timedelta(minutes=resolution_min * i) for i in range(n_rows)]

    out = pd.DataFrame({
        "timestamp_utc": timestamps_utc,
        "price_eur_mwh": prices.astype(float).values,
        "resolution_min": resolution_min,
    })
    return out


def get_ipto_dam_prices(target_date: date, session: Optional[requests.Session] = None,
                        inspect: bool = False) -> pd.DataFrame:
    """End-to-end: discover → download → parse one day of IPTO DAM prices."""
    session = session or _session()
    entries = fetch_ipto_file_index(target_date, session)
    if not entries:
        raise ValueError(f"IPTO returned no {IPTO_FILE_CATEGORY} files for {target_date}")

    url = None
    for entry in entries:
        url = _extract_file_url(entry)
        if url:
            break
    if not url:
        raise ValueError(
            f"Could not extract a download URL from IPTO index entries: {entries[:1]!r} "
            "— inspect the raw entry and add the real key to _extract_file_url."
        )

    log.info("Downloading IPTO DAM file for %s: %s", target_date, url)
    xlsx_bytes = download_ipto_excel(url, session)
    return parse_ipto_dam_excel(xlsx_bytes, target_date, inspect=inspect)


# ─────────────────────────────────────────────────────────────────────────────
# 3. RESILIENT DB UPSERT
# ─────────────────────────────────────────────────────────────────────────────
def ensure_schema(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(DAM_PRICES_DDL)
    conn.commit()
    conn.close()


def upsert_dam_prices(df: pd.DataFrame, bidding_zone: str, source: str,
                      db_path: str = DEFAULT_DB_PATH) -> int:
    """
    Upsert a batch of [timestamp_utc, price_eur_mwh, resolution_min] rows.

    Conflict policy (source priority, not last-write-wins): ENTSO-E is the
    primary source used everywhere else in this app, so an ENTSO-E row
    always overwrites whatever's there. An IPTO row only ever fills a slot
    that's genuinely empty (no existing row, or an existing row with a NULL
    price) — it never clobbers a good ENTSO-E value just because IPTO was
    fetched more recently. This is what lets the fallback "safely resolve
    the missing values" without risking silently degrading already-good data.
    """
    if df.empty:
        return 0
    ensure_schema(db_path)
    fetched_ts = datetime.now(timezone.utc).isoformat()

    rows = [
        (row.timestamp_utc.isoformat(), bidding_zone, float(row.price_eur_mwh),
         int(row.resolution_min), source, fetched_ts)
        for row in df.itertuples(index=False)
    ]

    conn = sqlite3.connect(db_path)
    conn.executemany(f"""
        INSERT INTO dam_prices
            (timestamp_utc, bidding_zone, price_eur_mwh, resolution_min, source, fetched_ts)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(timestamp_utc, bidding_zone) DO UPDATE SET
            price_eur_mwh  = CASE
                WHEN excluded.source = 'ENTSOE' THEN excluded.price_eur_mwh
                WHEN dam_prices.price_eur_mwh IS NULL THEN excluded.price_eur_mwh
                ELSE dam_prices.price_eur_mwh
            END,
            resolution_min = CASE
                WHEN excluded.source = 'ENTSOE' THEN excluded.resolution_min
                WHEN dam_prices.price_eur_mwh IS NULL THEN excluded.resolution_min
                ELSE dam_prices.resolution_min
            END,
            source = CASE
                WHEN excluded.source = 'ENTSOE' THEN excluded.source
                WHEN dam_prices.price_eur_mwh IS NULL THEN excluded.source
                ELSE dam_prices.source
            END,
            fetched_ts = CASE
                WHEN excluded.source = 'ENTSOE' THEN excluded.fetched_ts
                WHEN dam_prices.price_eur_mwh IS NULL THEN excluded.fetched_ts
                ELSE dam_prices.fetched_ts
            END
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def read_dam_prices(target_date: date, bidding_zone: str = DEFAULT_BIDDING_ZONE,
                    db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Read back a day's stored prices, regardless of which source filled them."""
    ensure_schema(db_path)
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc).isoformat()
    day_end = (datetime(target_date.year, target_date.month, target_date.day,
                       tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """SELECT timestamp_utc, price_eur_mwh, resolution_min, source
           FROM dam_prices
           WHERE bidding_zone = ? AND timestamp_utc >= ? AND timestamp_utc < ?
           ORDER BY timestamp_utc""",
        conn, params=(bidding_zone, day_start, day_end))
    conn.close()
    if not df.empty:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATION: ENTSO-E FIRST, IPTO ON FAILURE
# ─────────────────────────────────────────────────────────────────────────────
def ingest_ipto_day(target_date: date, bidding_zone: str = DEFAULT_BIDDING_ZONE,
                    db_path: str = DEFAULT_DB_PATH, inspect: bool = False,
                    dry_run: bool = False) -> int:
    """Fetch one day from IPTO and upsert it. Returns the number of rows written."""
    df = get_ipto_dam_prices(target_date, inspect=inspect)
    log.info("Parsed %d IPTO price rows for %s (resolution=%dmin)",
             len(df), target_date, df["resolution_min"].iloc[0] if len(df) else 0)
    if dry_run:
        print(df.to_string(index=False))
        return 0
    return upsert_dam_prices(df, bidding_zone, source="IPTO", db_path=db_path)


def ensure_dam_prices(target_date: date, bidding_zone: str = DEFAULT_BIDDING_ZONE,
                      db_path: str = DEFAULT_DB_PATH,
                      entsoe_fetch: Optional[Callable[[date], pd.DataFrame]] = None,
                      min_expected_rows: int = 20) -> pd.DataFrame:
    """
    The actual "alternative way to pull data if the ENTSO-E pull fails"
    entry point: return a day's DAM prices, trying (in order)
      1. what's already cached in dam_prices,
      2. a live ENTSO-E fetch (if `entsoe_fetch` is supplied — inject
         apm_app.py's own ENTSO-E function here so this module doesn't need
         to duplicate that XML-parsing logic),
      3. IPTO, as the fallback.

    `entsoe_fetch(target_date)` is expected to return a DataFrame with at
    least ['dt' or 'timestamp_utc', 'price'] columns (matching apm_app.py's
    dam_daily() shape) — adapt the column names below if wiring in a
    different function.
    """
    cached = read_dam_prices(target_date, bidding_zone, db_path)
    if len(cached) >= min_expected_rows:
        log.info("Using %d cached dam_prices rows for %s", len(cached), target_date)
        return cached

    if entsoe_fetch is not None:
        try:
            entsoe_df = entsoe_fetch(target_date)
            if entsoe_df is not None and not entsoe_df.empty:
                ts_col = "timestamp_utc" if "timestamp_utc" in entsoe_df.columns else "dt"
                norm = pd.DataFrame({
                    "timestamp_utc": pd.to_datetime(entsoe_df[ts_col], utc=True),
                    "price_eur_mwh": entsoe_df["price"].astype(float),
                })
                norm["resolution_min"] = 15 if len(norm) > 30 else 60
                n = upsert_dam_prices(norm, bidding_zone, source="ENTSOE", db_path=db_path)
                log.info("ENTSO-E supplied %d rows for %s", n, target_date)
                cached = read_dam_prices(target_date, bidding_zone, db_path)
                if len(cached) >= min_expected_rows:
                    return cached
        except Exception as e:
            log.warning("ENTSO-E fetch failed for %s (%s) — falling back to IPTO", target_date, e)

    try:
        ingest_ipto_day(target_date, bidding_zone, db_path)
    except Exception as e:
        log.error("IPTO fallback also failed for %s: %s", target_date, e)

    return read_dam_prices(target_date, bidding_zone, db_path)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, help="Single date, YYYY-MM-DD")
    parser.add_argument("--start", type=date.fromisoformat, help="Range start, YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="Range end, YYYY-MM-DD")
    parser.add_argument("--zone", default=DEFAULT_BIDDING_ZONE, help="Bidding zone EIC code")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Path to apm_data.db")
    parser.add_argument("--inspect", action="store_true",
                       help="Print detected header/columns while parsing (no DB write)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Parse and print the result; don't write to the DB")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    if args.date:
        dates = [args.date]
    elif args.start and args.end:
        dates = list(_daterange(args.start, args.end))
    else:
        parser.error("Provide either --date or both --start and --end")
        return

    ok, failed = 0, []
    for d in dates:
        try:
            n = ingest_ipto_day(d, bidding_zone=args.zone, db_path=args.db_path,
                               inspect=args.inspect, dry_run=args.dry_run)
            log.info("%s: upserted %d row(s)", d, n)
            ok += 1
        except Exception as e:
            log.error("%s: FAILED — %s", d, e)
            failed.append(d)

    log.info("Done. %d/%d day(s) succeeded.", ok, len(dates))
    if failed:
        log.warning("Failed dates: %s", ", ".join(str(d) for d in failed))


if __name__ == "__main__":
    main()
