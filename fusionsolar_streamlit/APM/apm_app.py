# apm_app.py
# FusionSolar Asset Performance Management Dashboard — Full APM Edition
# Tiers 1 + 2 + 3: KPI Scorecard, WCPR, Alerts, Revenue Waterfall,
#   Loss Cascade, Degradation, Capture Rate, Availability, PDF Report,
#   DSCR Monitor, ML Anomaly Detection, Production Forecast, OPEX Tracker,
#   Soiling Optimisation

import io
import time
import random
import calendar
import sqlite3
import urllib3
import warnings
from datetime import datetime, timezone, date, timedelta
from typing import Tuple, List, Optional, Dict

import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FusionSolar APM",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# PLANT CONSTANTS  ← configure these for your site
# ─────────────────────────────────────────────────────────────────────────────
PLANT_TZ          = "Europe/Athens"
PLANT_START_YEAR  = 2025          # COD year
PLANT_PEAK_KW     = 1100.0        # Installed DC capacity (kWp)
GAMMA             = -0.0035       # Temperature coefficient (%/°C) — typical mono-PERC
NOCT              = 45.0          # Nominal Operating Cell Temperature (°C)
INV_DEV_TYPE_IDS  = [1, 38, 39]   # Huawei device type IDs for inverters

# Finance constants (update per project)
ANNUAL_DEBT_SVC   = 120_000.0     # € — annual principal + interest
FIXED_OPEX        = 25_000.0      # € — fixed O&M, insurance, lease per year
VAR_OPEX_PER_MWH  = 2.5           # € per MWh variable O&M

# Alert thresholds
PR_ALERT_RED      = 0.65
PR_ALERT_AMBER    = 0.75
INV_TEMP_WARN     = 75.0
STRING_CV_WARN    = 0.12
PROD_VS_EXP_WARN  = 0.70          # flag if actual < 70% of PVGIS expected

# PVGIS-SARAH3 long-term monthly GTI (kWh/m²) — Asvestochori, Thessaloniki
PVGIS_GTI = {1:55.2,2:73.8,3:118.6,4:155.4,5:185.2,6:205.7,
             7:215.3,8:196.4,9:152.8,10:103.5,11:62.4,12:46.1}
T_AMB     = {1:4.5,2:5.8,3:9.3,4:14.6,5:20.1,6:25.2,
             7:27.8,8:27.4,9:22.5,10:16.2,11:10.6,12:6.1}

MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"]
PALETTE      = ["#f0b429","#3ecfcf","#60a5fa","#a78bfa","#fb923c","#34d399","#f472b6"]
ADMIE_API    = "https://www.admie.gr/getOperationMarketFile"

# Failure-predictor telemetry signals and their warning thresholds
FAILURE_SIGNALS = {
    "dataItemMap.temperature":  {"label":"Inverter Temp (°C)",   "warn":65.0,  "crit":75.0,  "unit":"°C",   "color":"#fb923c"},
    "dataItemMap.power_factor": {"label":"Power Factor",          "warn":0.90,  "crit":0.85,  "unit":"",     "color":"#60a5fa",  "low_bad":True},
    "dataItemMap.efficiency":   {"label":"Efficiency (%)",        "warn":95.0,  "crit":92.0,  "unit":"%",    "color":"#34d399",  "low_bad":True},
    "dataItemMap.elec_freq":    {"label":"Grid Frequency (Hz)",   "warn":49.8,  "crit":49.5,  "unit":"Hz",   "color":"#a78bfa"},
    "dataItemMap.active_power": {"label":"AC Power (kW)",         "warn":None,  "crit":None,  "unit":"kW",   "color":"#f0b429"},
}

# ─────────────────────────────────────────────────────────────────────────────
# THEME HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_BG  = "#0e1117"
_SRF = "#161b22"
_GRD = "#1f2333"
_TXT = "#e2e8f0"

def _base_layout(fig, title="", xaxis_title="", yaxis_title="",
                 height=None, barmode=None, showlegend=True):
    kw = dict(paper_bgcolor=_BG, plot_bgcolor=_BG,
              font=dict(color=_TXT),
              legend=dict(bgcolor="rgba(0,0,0,0)"),
              margin=dict(t=55,b=40,l=10,r=10),
              hovermode="x unified")
    if title:    kw["title"]   = dict(text=title, font=dict(size=15))
    if barmode:  kw["barmode"] = barmode
    if height:   kw["height"]  = height
    if not showlegend: kw["showlegend"] = False
    fig.update_layout(**kw)
    fig.update_xaxes(gridcolor=_GRD, zerolinecolor=_GRD,
                     title_text=xaxis_title, title_font=dict(color=_TXT))
    fig.update_yaxes(gridcolor=_GRD, zerolinecolor=_GRD,
                     title_text=yaxis_title, title_font=dict(color=_TXT))
    return fig

def _dual_layout(fig, title="", left_title="", right_title="",
                 left_color="#f0b429", right_color="#3ecfcf", height=None):
    kw = dict(paper_bgcolor=_BG, plot_bgcolor=_BG,
              font=dict(color=_TXT),
              legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h",
                          yanchor="bottom", y=1.02),
              margin=dict(t=80,b=40,l=70,r=70),
              hovermode="x unified")
    if title:  kw["title"]  = dict(text=title, font=dict(size=15))
    if height: kw["height"] = height
    fig.update_layout(**kw)
    fig.update_xaxes(gridcolor=_GRD, zerolinecolor=_GRD, tickformat="%H:%M")
    fig.update_yaxes(title_text=left_title, gridcolor=_GRD, zerolinecolor=_GRD,
                     title_font=dict(color=left_color),
                     tickfont=dict(color=left_color), secondary_y=False)
    fig.update_yaxes(title_text=right_title, gridcolor="rgba(0,0,0,0)",
                     zerolinecolor=_GRD,
                     title_font=dict(color=right_color),
                     tickfont=dict(color=right_color), secondary_y=True)
    return fig

# ─────────────────────────────────────────────────────────────────────────────
# SQLITE  — alerts, opex, downtime, cleaning events
# ─────────────────────────────────────────────────────────────────────────────
DB_PATH = "apm_data.db"

def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, severity TEXT, category TEXT,
        message TEXT, status TEXT DEFAULT 'Open')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS opex(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dt TEXT, category TEXT, description TEXT, amount_eur REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS downtime(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_dt TEXT, end_dt TEXT, inverter TEXT,
        lost_kwh REAL, cause TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS cleaning(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dt TEXT, cost_eur REAL, yield_recovery_pct REAL, notes TEXT)""")
    # Telemetry history: snapshots of inverter health signals over time
    conn.execute("""CREATE TABLE IF NOT EXISTS telemetry_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, inverter_id TEXT, signal TEXT, value REAL)""")
    # Price cache: persists last-fetched DAM prices so stale data can be shown
    # when ADMIE is temporarily unreachable
    conn.execute("""CREATE TABLE IF NOT EXISTS price_cache(
        ym TEXT PRIMARY KEY, avg_price_eur_mwh REAL, fetched_ts TEXT)""")
    conn.commit()
    return conn


def _save_telemetry_snapshot(df_kpi: pd.DataFrame):
    """Persist current inverter telemetry to history table for trend analysis."""
    if df_kpi.empty: return
    conn = _get_db()
    ts = datetime.now().isoformat()
    rows = []
    inv_id_col = next((c for c in df_kpi.columns
                       if "id" in c.lower() or "sn" in c.lower() or "dev" in c.lower()), None)
    for idx, row in df_kpi.iterrows():
        inv_id = str(row[inv_id_col]) if inv_id_col else str(idx)
        for sig_col, meta in FAILURE_SIGNALS.items():
            if sig_col in df_kpi.columns:
                val = pd.to_numeric(row.get(sig_col), errors="coerce")
                if pd.notna(val):
                    rows.append((ts, inv_id, meta["label"], float(val)))
    if rows:
        conn.executemany(
            "INSERT INTO telemetry_history(ts,inverter_id,signal,value) VALUES(?,?,?,?)",
            rows)
        conn.commit()
    conn.close()


def _cache_dam_price(ym: str, price: float):
    conn = _get_db()
    conn.execute("INSERT OR REPLACE INTO price_cache VALUES(?,?,?)",
                 (ym, price, datetime.now().isoformat()))
    conn.commit(); conn.close()


def _get_cached_dam_price(ym: str) -> Optional[float]:
    conn = _get_db()
    row = conn.execute("SELECT avg_price_eur_mwh FROM price_cache WHERE ym=?",
                       (ym,)).fetchone()
    conn.close()
    return row[0] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# IPTO / ADMIE CONNECTIVITY DIAGNOSTICS
# ─────────────────────────────────────────────────────────────────────────────
def _classify_admie_error(exc: Exception) -> Tuple[str, str]:
    """
    Return (error_type, user_message) from an exception raised by an ADMIE call.
    Distinguishes network-level failures from application-level ones.
    """
    msg = str(exc)
    if "NameResolution" in msg or "Failed to resolve" in msg or "Name or service" in msg:
        return ("DNS_FAILURE",
                "❌ **DNS failure** — cannot resolve `www.admie.gr`. "
                "Your deployment server's outbound DNS/firewall is blocking the connection. "
                "Check that `www.admie.gr` (port 443) is whitelisted.")
    if "ConnectionRefused" in msg or "Connection refused" in msg:
        return ("CONN_REFUSED",
                "❌ **Connection refused** — `admie.gr` is actively rejecting connections. "
                "May be a temporary outage or IP block.")
    if "timed out" in msg.lower() or "Timeout" in msg:
        return ("TIMEOUT",
                "⏱️ **Timeout** — `admie.gr` did not respond within the timeout window. "
                "Try increasing the timeout or retry later.")
    if "SSLError" in msg or "SSL" in msg:
        return ("SSL_ERROR",
                "🔒 **SSL/TLS error** — certificate validation failed. "
                "Try setting `verify=False` (already set) or check system CA bundle.")
    if "Max retries" in msg:
        return ("MAX_RETRIES",
                "🔄 **Max retries exceeded** — usually wraps a DNS or connection failure. "
                "Check egress proxy and firewall rules for `admie.gr:443`.")
    return ("UNKNOWN", f"❓ Unknown error: `{msg[:200]}`")


def probe_admie(timeout: int = 8) -> dict:
    """
    Run a live connectivity probe of the ADMIE API and return a detailed
    diagnostic report dict.
    Tests both DAM_ResultsSummary (legacy column format) and
    ISP1ISPResults (current transposed row format).
    """
    result = dict(reachable=False, status_code=None, error_type=None,
                  user_message=None, file_url_found=False,
                  price_parseable=False, sample_price=None,
                  json_keys=None, n_files=0, working_format=None)

    # Use a date 3 days ago to ensure files exist (ISP published D-1 ~19:00 EET)
    test_date = date.today() - timedelta(days=3)
    ds = test_date.strftime("%Y-%m-%d")

    # Step 1 — basic reachability
    try:
        r0 = requests.get("https://www.admie.gr/", timeout=timeout, verify=False)
        result["status_code"] = r0.status_code
        deny = r0.headers.get("x-deny-reason")
        if deny:
            result["error_type"] = "PROXY_BLOCKED"
            result["user_message"] = (
                f"🚫 **Egress proxy blocked** — `x-deny-reason: {deny}`. "
                "Add `admie.gr` to your network allowlist.")
            return result
        result["reachable"] = True
    except Exception as e:
        et, um = _classify_admie_error(e)
        result["error_type"] = et
        result["user_message"] = um
        return result

    # Step 2 — probe each FileCategory
    for fc in ["DAM_ResultsSummary", "ISP1ISPResults"]:
        try:
            r = requests.get(ADMIE_API,
                params={"dateStart": ds, "dateEnd": ds, "FileCategory": fc},
                timeout=timeout, verify=False)
            if r.status_code != 200:
                continue
            j = r.json()
            if not j:
                continue   # empty list — no files for this date in this category
            result["n_files"] = len(j) if isinstance(j, list) else 1
            if isinstance(j, list) and j and isinstance(j[0], dict):
                result["json_keys"] = list(j[0].keys())

            furl = _extract_url_from_entry(j[-1] if isinstance(j, list) else j)
            if not furl:
                result["error_type"] = "NO_FILE_URL"
                result["user_message"] = (
                    f"⚠️ `{fc}` returned {result['n_files']} entries but no URL "
                    f"could be extracted. Keys seen: `{result['json_keys']}`")
                continue

            result["file_url_found"] = True
            fd = requests.get(furl, timeout=25, verify=False)
            if fd.status_code != 200:
                result["error_type"] = "FILE_DOWNLOAD_FAIL"
                result["user_message"] = (
                    f"⚠️ File download returned HTTP {fd.status_code}")
                continue

            xl = pd.read_excel(io.BytesIO(fd.content), sheet_name=None)

            # Try column format first, then ISP row format
            df_p = _parse_excel_column_format(xl, test_date, fc)
            if df_p is None:
                df_p = _parse_excel_isp_format(xl, test_date, fc)

            if df_p is not None and not df_p.empty:
                prices = pd.to_numeric(df_p["price"], errors="coerce").dropna()
                if len(prices) > 0:
                    result["price_parseable"] = True
                    result["sample_price"] = float(prices.mean())
                    result["working_format"] = fc
                    result["user_message"] = (
                        f"✅ **ADMIE API fully operational** via `{fc}`. "
                        f"Date tested: {ds}. "
                        f"**{len(prices)} periods** parsed. "
                        f"Avg price: **{result['sample_price']:.2f} €/MWh**.")
                    return result

        except Exception as e:
            et, _ = _classify_admie_error(e)
            result["error_type"] = et
            result["user_message"] = f"Error testing `{fc}`: `{str(e)[:200]}`"
            continue

    # If we got here, reachable but no price data parsed
    if result["reachable"] and not result["price_parseable"]:
        if not result.get("user_message"):
            result["error_type"] = "NO_SMP_ROW"
            result["user_message"] = (
                "⚠️ API is reachable and files downloaded, but the SMP/price row "
                "could not be identified in either `DAM_ResultsSummary` or "
                "`ISP1ISPResults` for the test date. "
                "ADMIE may have changed the row label. "
                "Check the **Raw ISP Sheet Inspector** below for the actual row labels.")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# API BACKOFF
# ─────────────────────────────────────────────────────────────────────────────
def _post(session, url, payload, timeout=25, retries=3):
    last = None
    for i in range(retries):
        try:
            r = session.post(url, json=payload, timeout=timeout)
            last = r
            if r.status_code == 200:
                j = r.json()
                if j.get("failCode") == 407:
                    time.sleep(min(30, 2*(2**i)) + random.uniform(0,1))
                    continue
                return j
        except Exception:
            pass
        time.sleep(min(30, 2*(2**i)) + random.uniform(0,1))
    try:    return last.json()
    except: return {"data": None, "failCode": -1}

# ─────────────────────────────────────────────────────────────────────────────
# HUAWEI CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class HuaweiClient:
    def __init__(self, cfg):
        self.base_url    = cfg["base_url"].rstrip("/")
        self.username    = cfg["username"]
        self.system_code = cfg["system_code"]
        self.verify_ssl  = cfg.get("verify_ssl", True)
        self.s = requests.Session()
        self.s.verify = self.verify_ssl
        self.s.headers.update({"Content-Type":"application/json",
                               "User-Agent":"Streamlit-APM"})

    def login(self) -> Tuple[bool, str]:
        try:
            r = self.s.post(f"{self.base_url}/thirdData/login",
                            json={"userName":self.username,
                                  "systemCode":self.system_code}, timeout=15)
            tok = r.cookies.get("XSRF-TOKEN") or r.headers.get("xsrf-token")
            if not tok: return False, "No XSRF token — check credentials."
            self.s.headers.update({"XSRF-TOKEN": tok})
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def get_stations(self):
        r = _post(self.s, f"{self.base_url}/thirdData/getStationList", {})
        d = r.get("data", [])
        rows = d.get("list",[]) if isinstance(d,dict) else (d if isinstance(d,list) else [])
        return (rows, None) if rows else (None, "No stations returned")

    @property
    def xsrf(self): return self.s.headers.get("XSRF-TOKEN","")

def ensure_client():
    if "hw_client" not in st.session_state:
        try:    cfg = st.secrets["fusion"]
        except: st.error("❌ `[fusion]` missing from secrets.toml"); return None, None
        c = HuaweiClient(cfg)
        ok, msg = c.login()
        if not ok: st.error(f"❌ Login: {msg}"); return None, None
        stations, err = c.get_stations()
        if not stations: st.error(f"❌ Stations: {err}"); return None, None
        st.session_state["hw_client"]   = c
        st.session_state["hw_stations"] = stations
    return st.session_state["hw_client"], st.session_state["hw_stations"]

# ─────────────────────────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _norm(rows):
    if not rows: return pd.DataFrame()
    df = pd.json_normalize(rows, sep=".")
    return df.loc[:, ~df.columns.duplicated()].copy()

def _resolve(df):
    t = next((c for c in df.columns if "time" in c.lower() or "collect" in c.lower()), None)
    e = next((c for c in df.columns if "inverterYield" in c or "month_cap" in c
              or "energy" in c.lower()), None)
    return t, e

def get_budget(year):
    tgts = [117077,89742,140573,172775,177950,186287,
            197265,190014,168524,132649,86079,82732]
    if year == 2025:
        for i in range(9): tgts[i] = 0
    return pd.DataFrame({"Month":MONTH_LABELS,"Budget_kWh":tgts,
                          "MonthNum":range(1,13),"Year":year})

def pvgis_df(years, ref_pr):
    rows=[]
    for yr in years:
        for m in range(1,13):
            g = PVGIS_GTI[m]
            rows.append({"YearMonth":f"{yr}-{m:02d}","MonthNum":m,"Year":yr,
                         "GTI":g,"T_amb":T_AMB[m],
                         "Expected_kWh": g * PLANT_PEAK_KW * ref_pr})
    return pd.DataFrame(rows)

def wcpr(energy_kwh, gti_kwh_m2, t_amb_c):
    """
    Weather-corrected PR — IEC 61724.
    T_cell estimated from T_amb + NOCT proxy.
    """
    t_cell = t_amb_c + (NOCT - 20) / 800 * (gti_kwh_m2 * 1000 / 730)  # rough irr proxy
    ref = gti_kwh_m2 * PLANT_PEAK_KW * (1 + GAMMA * (t_cell - 25))
    return energy_kwh / ref if ref > 0 else np.nan

def add_alert(severity, category, message):
    conn = _get_db()
    conn.execute("INSERT INTO alerts(ts,severity,category,message) VALUES(?,?,?,?)",
                 (datetime.now().isoformat(), severity, category, message))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# CACHED API CALLS
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def api_monthly(base_url, sid, year, xsrf, verify):
    s = requests.Session(); s.verify=verify
    s.headers.update({"Content-Type":"application/json","XSRF-TOKEN":xsrf})
    dt = datetime(year,1,1,tzinfo=timezone.utc)
    j  = _post(s, f"{base_url}/thirdData/getKpiStationMonth",
               {"stationCodes":sid,"collectTime":int(dt.timestamp()*1000)})
    raw = j.get("data",[])
    if raw and isinstance(raw,list) and "kpiList" in raw[0]: raw=raw[0]["kpiList"]
    return _norm(raw)

@st.cache_data(ttl=1800, show_spinner=False)
def api_15min(base_url, sid, target_date, xsrf, verify):
    s = requests.Session(); s.verify=verify
    s.headers.update({"Content-Type":"application/json","XSRF-TOKEN":xsrf})
    ms = int(datetime.combine(target_date, datetime.min.time(),
                              tzinfo=timezone.utc).timestamp()*1000)
    j5 = _post(s, f"{base_url}/thirdData/getKpiStation5min",
               {"stationCodes":sid,"collectTime":ms})
    if j5.get("data"): j5["_src"]="5min"; return j5
    jh = _post(s, f"{base_url}/thirdData/getKpiStationHour",
               {"stationCodes":sid,"collectTime":ms})
    jh["_src"]="hour"; return jh

@st.cache_data(ttl=300, show_spinner=False)
def api_realtime(base_url, dev_type, dev_ids, xsrf, verify):
    s = requests.Session(); s.verify=verify
    s.headers.update({"Content-Type":"application/json","XSRF-TOKEN":xsrf})
    j = _post(s, f"{base_url}/thirdData/getDevRealKpi",
              {"devTypeId":dev_type,"devIds":",".join(map(str,dev_ids))})
    return _norm(j.get("data",[]))

# ── ADMIE Excel parsing helpers ───────────────────────────────────────────────
# Diagnostic output revealed two distinct file formats:
#
#  Format A — DAM_ResultsSummary (legacy, pre-ISP reform):
#    Traditional layout: rows = periods, named price COLUMN (MCP/SMP/Price/Τιμή)
#    Status: Now returns [] for recent dates — Greece fully migrated to ISP.
#
#  Format B — ISP1ISPResults (current format as of 2025-26):
#    TRANSPOSED layout: col 0 = entity/metric label, cols 1-96 = 15-min periods.
#    The SMP is a specific ROW identified by its label in col 0.
#    Timestamps run across the column headers, not down a column.

# SMP row label keywords to search for in col 0 of ISP sheets
_SMP_ROW_KEYWORDS = [
    "system marginal price", "smp", "marginal price", "clearing price",
    "imbalance settlement price", "balancing price", "settlement price",
    "οριακή τιμή", "τιμή εκκαθάρισης", "τιμή αγοράς", "ota",
    # Broader fallback — any row whose label contains both "price" and "system"
    # is evaluated after exact matches fail
]

def _extract_url_from_entry(fe: dict) -> Optional[str]:
    """Pull the file download URL out of an ADMIE API file metadata dict."""
    furl = fe.get("file_path") or fe.get("url") or fe.get("link")
    if not furl:
        for v in fe.values():
            if isinstance(v, str) and (
                v.startswith("http") or v.endswith(".xlsx") or v.endswith(".xls")
            ):
                furl = v; break
    return furl


def _parse_excel_column_format(xl: dict, target_date: date, fc: str
                                ) -> Optional[pd.DataFrame]:
    """
    Format A: traditional column layout — one row per period, one column = price.
    Used by DAM_ResultsSummary files.
    """
    for df_s in xl.values():
        cl = [str(c).lower() for c in df_s.columns]
        pc = next((df_s.columns[i] for i, c in enumerate(cl)
                   if any(k in c for k in ["mcp", "smp", "price", "τιμή"])), None)
        tc = next((df_s.columns[i] for i, c in enumerate(cl)
                   if any(k in c for k in ["period", "time", "ώρα", "dp", "hour"])), None)
        if pc and tc:
            df_p = df_s[[tc, pc]].copy()
            df_p.columns = ["Period", "price"]
            df_p["price"] = pd.to_numeric(df_p["price"], errors="coerce")
            df_p = df_p.dropna(subset=["price"])
            n = len(df_p)
            if n == 0: continue
            freq = 15 if n >= 88 else 60
            base = datetime(target_date.year, target_date.month,
                            target_date.day, tzinfo=timezone.utc)
            df_p["dt"] = [base + timedelta(minutes=freq * i) for i in range(n)]
            df_p["dt"] = pd.to_datetime(df_p["dt"]).dt.tz_convert(PLANT_TZ)
            df_p["_src"] = fc
            return df_p[["dt", "price", "_src"]]
    return None


def _parse_excel_isp_format(xl: dict, target_date: date, fc: str
                             ) -> Optional[pd.DataFrame]:
    """
    Format B: ISP1ISPResults transposed layout.
    - col 0  = entity/metric label (search this for SMP keywords)
    - cols 1..96 = values per 15-min period
    - col 97 = TOTAL (ignore)
    The SMP row is identified by its label in column 0.
    If exact keyword match fails, fall back to finding a row with 96 numeric
    values in a plausible €/MWh range (5 – 600).
    """
    for sheet_name, df_s in xl.items():
        if df_s.shape[1] < 25:
            continue  # too few columns to be a time-series sheet

        label_col = df_s.columns[0]  # 'Unnamed: 0' in ISP files

        # Pass 1 — exact keyword match on row label
        for idx, row in df_s.iterrows():
            label = str(row.iloc[0]).lower().strip()
            if any(kw in label for kw in _SMP_ROW_KEYWORDS):
                result = _build_isp_price_series(row, target_date, fc, sheet_name)
                if result is not None:
                    return result

        # Pass 2 — broader heuristic: row label contains both "price" and "system"
        for idx, row in df_s.iterrows():
            label = str(row.iloc[0]).lower().strip()
            if "price" in label and "system" in label:
                result = _build_isp_price_series(row, target_date, fc, sheet_name)
                if result is not None:
                    return result

        # Pass 3 — numeric fallback: find first row with 88-96 numeric values
        # all in the range 0-600 €/MWh (rules out load/generation MW rows)
        for idx, row in df_s.iterrows():
            vals = pd.to_numeric(row.iloc[1:97], errors="coerce").dropna()
            if len(vals) >= 88 and (vals >= 0).all() and (vals <= 600).all():
                result = _build_isp_price_series(row, target_date, fc,
                                                  f"{sheet_name}[heuristic]")
                if result is not None:
                    return result

    return None


def _build_isp_price_series(row: pd.Series, target_date: date,
                              fc: str, source_label: str) -> Optional[pd.DataFrame]:
    """Convert a single ISP row (96 period values) into a dt/price DataFrame."""
    vals = pd.to_numeric(row.iloc[1:97], errors="coerce").dropna()
    n = len(vals)
    if n < 24:
        return None
    freq = 15 if n >= 88 else 60
    base = datetime(target_date.year, target_date.month,
                    target_date.day, tzinfo=timezone.utc)
    dts = pd.to_datetime(
        [base + timedelta(minutes=freq * i) for i in range(n)]
    ).tz_convert(PLANT_TZ)
    return pd.DataFrame({"dt": dts, "price": vals.values,
                          "_src": f"{fc}:{source_label}"})


def _fetch_admie_file(fc: str, date_start: str, date_end: str,
                      timeout: int = 15) -> Optional[bytes]:
    """
    Call the ADMIE metadata API and download the latest Excel file for a given
    FileCategory and date range. Returns raw bytes or None on any failure.
    Records errors in session state.
    """
    try:
        r = requests.get(ADMIE_API,
            params={"dateStart": date_start, "dateEnd": date_end,
                    "FileCategory": fc},
            timeout=timeout, verify=False)
        deny = r.headers.get("x-deny-reason")
        if deny:
            st.session_state["_admie_last_error"] = \
                f"Egress proxy blocked: x-deny-reason={deny}"
            return None
        if r.status_code != 200:
            st.session_state["_admie_last_error"] = \
                f"HTTP {r.status_code} from ADMIE metadata API"
            return None
        files = r.json()
        if not files:
            return None   # no files for this date — not an error
        furl = _extract_url_from_entry(files[-1])
        if not furl:
            st.session_state["_admie_last_error"] = \
                f"No URL found in file entry: {list(files[-1].keys())}"
            return None
        fd = requests.get(furl, timeout=25, verify=False)
        if fd.status_code != 200:
            st.session_state["_admie_last_error"] = \
                f"File download HTTP {fd.status_code} for {furl[:80]}"
            return None
        return fd.content
    except Exception as e:
        et, _ = _classify_admie_error(e)
        st.session_state["_admie_last_error"] = f"{et}: {str(e)[:180]}"
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def dam_daily(target_date: date) -> pd.DataFrame:
    """
    Fetch 15-min SMP/DAM clearing prices from ADMIE for a single day.
    Returns DataFrame[dt, price, _src] or empty frame on failure.

    Strategy (in order):
    1. Try DAM_ResultsSummary (column format) — works for pre-2025 dates
    2. Try ISP1ISPResults (transposed row format) — current Greek market format
    3. If today has no ISP file yet (published D-1 ~19:00 EET), try yesterday
    """
    ds  = target_date.strftime("%Y-%m-%d")
    ds1 = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    empty = pd.DataFrame(columns=["dt", "price", "_src"])

    # ── Pass 1: DAM_ResultsSummary (legacy column format) ────────────────────
    content = _fetch_admie_file("DAM_ResultsSummary", ds, ds)
    if content:
        xl = pd.read_excel(io.BytesIO(content), sheet_name=None)
        result = _parse_excel_column_format(xl, target_date, "DAM_ResultsSummary")
        if result is not None:
            st.session_state.pop("_admie_last_error", None)
            return result

    # ── Pass 2: ISP1ISPResults for the requested date (transposed row format) ─
    content = _fetch_admie_file("ISP1ISPResults", ds, ds)
    if content:
        xl = pd.read_excel(io.BytesIO(content), sheet_name=None)
        result = _parse_excel_isp_format(xl, target_date, "ISP1ISPResults")
        if result is not None:
            st.session_state.pop("_admie_last_error", None)
            return result

    # ── Pass 3: ISP1ISPResults published the day before (D-1 publication) ────
    content = _fetch_admie_file("ISP1ISPResults", ds1, ds1)
    if content:
        xl = pd.read_excel(io.BytesIO(content), sheet_name=None)
        result = _parse_excel_isp_format(xl, target_date - timedelta(days=1),
                                          "ISP1ISPResults")
        if result is not None:
            # re-stamp timestamps to target_date
            result["dt"] = result["dt"] + pd.Timedelta(days=1)
            result["_src"] += ":D-1proxy"
            st.session_state.pop("_admie_last_error", None)
            return result

    # Record a clear diagnostic if all passes fail
    if "_admie_last_error" not in st.session_state:
        st.session_state["_admie_last_error"] = (
            f"No price data found for {ds} in DAM_ResultsSummary or ISP1ISPResults. "
            f"ISP files are published around 19:00 EET the day before."
        )
    return empty


@st.cache_data(ttl=7200, show_spinner=False)
def dam_monthly_avg(year: int, month: int) -> Optional[float]:
    """
    Monthly average SMP/DAM price from ADMIE.
    Tries DAM_ResultsSummary first (for historical months), then samples
    ISP1ISPResults across the month (current format).
    Falls back to SQLite price cache if the API is unreachable.
    """
    ym   = f"{year}-{month:02d}"
    ds1  = f"{year}-{month:02d}-01"
    last = calendar.monthrange(year, month)[1]
    dsN  = f"{year}-{month:02d}-{last:02d}"

    # ── Attempt 1: DAM_ResultsSummary (legacy, works for older months) ────────
    try:
        r = requests.get(ADMIE_API,
            params={"dateStart": ds1, "dateEnd": dsN,
                    "FileCategory": "DAM_ResultsSummary"},
            timeout=15, verify=False)
        deny = r.headers.get("x-deny-reason")
        if deny:
            st.session_state["_admie_last_error"] = f"Egress proxy blocked: {deny}"
            return _get_cached_dam_price(ym)
        if r.status_code == 200 and r.json():
            prices = []
            for fe in r.json():
                furl = _extract_url_from_entry(fe)
                if not furl: continue
                fd = requests.get(furl, timeout=25, verify=False)
                if fd.status_code != 200: continue
                xl = pd.read_excel(io.BytesIO(fd.content), sheet_name=None)
                for df_s in xl.values():
                    cl = [str(c).lower() for c in df_s.columns]
                    pc = next((df_s.columns[i] for i, c in enumerate(cl)
                               if any(k in c for k in
                                      ["mcp", "smp", "price", "τιμή"])), None)
                    if pc:
                        v = pd.to_numeric(df_s[pc], errors="coerce").dropna()
                        prices.extend(v[v > 0].tolist()); break
            if prices:
                avg = float(np.mean(prices))
                _cache_dam_price(ym, avg)
                return avg
    except Exception as e:
        et, _ = _classify_admie_error(e)
        st.session_state["_admie_last_error"] = f"{et}: {str(e)[:180]}"

    # ── Attempt 2: ISP1ISPResults — sample ~8 representative days per month ──
    # Fetching all ~30 daily files is too slow; sample every 4th day.
    try:
        sample_days = list(range(1, last + 1, 4))  # days 1,5,9,...
        all_prices: List[float] = []
        for day in sample_days:
            ds = f"{year}-{month:02d}-{day:02d}"
            content = _fetch_admie_file("ISP1ISPResults", ds, ds, timeout=20)
            if not content:
                continue
            xl = pd.read_excel(io.BytesIO(content), sheet_name=None)
            d  = date(year, month, day)
            df_day = _parse_excel_isp_format(xl, d, "ISP1ISPResults")
            if df_day is not None and not df_day.empty:
                day_prices = pd.to_numeric(df_day["price"], errors="coerce").dropna()
                all_prices.extend(day_prices[day_prices > 0].tolist())
        if all_prices:
            avg = float(np.mean(all_prices))
            _cache_dam_price(ym, avg)
            return avg
    except Exception as e:
        et, _ = _classify_admie_error(e)
        st.session_state["_admie_last_error"] = f"{et}: {str(e)[:180]}"

    # ── Fallback: cached value from previous successful fetch ─────────────────
    return _get_cached_dam_price(ym)

# ─────────────────────────────────────────────────────────────────────────────
# ALERT ENGINE — run after every data load
# ─────────────────────────────────────────────────────────────────────────────
def run_alert_checks(df_bench: pd.DataFrame):
    """Evaluate rule-based alerts on the monthly benchmark dataframe and persist."""
    if df_bench.empty: return
    recent = df_bench.dropna(subset=["PR"]).tail(3)
    if len(recent)==3 and (recent["PR"] < PR_ALERT_RED).all():
        add_alert("Critical","PR",
            f"PR below {PR_ALERT_RED:.0%} for 3 consecutive months "
            f"(last: {recent['PR'].iloc[-1]:.1%})")
    last = df_bench.dropna(subset=["Energy_kWh","Expected_kWh"]).iloc[-1] \
           if not df_bench.empty else None
    if last is not None and last["Expected_kWh"]>0:
        ratio = last["Energy_kWh"] / last["Expected_kWh"]
        if ratio < PROD_VS_EXP_WARN:
            add_alert("Warning","Production",
                f"Last month actual production {ratio:.0%} of PVGIS expected "
                f"({last['Energy_kWh']:,.0f} vs {last['Expected_kWh']:,.0f} kWh)")

# ─────────────────────────────────────────────────────────────────────────────
# WEATHER FORECAST (Open-Meteo — free, no key)
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_forecast() -> pd.DataFrame:
    """7-day hourly GHI + T_amb from Open-Meteo for Thessaloniki."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude":40.694,"longitude":22.990,
                    "hourly":"shortwave_radiation,temperature_2m",
                    "timezone":"Europe/Athens","forecast_days":7},
            timeout=10)
        if r.status_code!=200: return pd.DataFrame()
        j = r.json()
        df = pd.DataFrame({"dt": pd.to_datetime(j["hourly"]["time"]),
                           "GHI_Wm2": j["hourly"]["shortwave_radiation"],
                           "T_amb":   j["hourly"]["temperature_2m"]})
        df["dt"] = df["dt"].dt.tz_localize("Europe/Athens", ambiguous="NaT",
                                           nonexistent="NaT")
        # Simple GTI = GHI × 1.15 proxy for fixed 30° south tilt
        df["GTI_Wm2"] = df["GHI_Wm2"] * 1.15
        # Estimated yield (kWh per hour): GTI(W/m²)/1000 × capacity × WCPR
        t_cell = df["T_amb"] + (NOCT-20)/800 * df["GTI_Wm2"]
        df["Yield_kWh"] = (df["GTI_Wm2"]/1000 * PLANT_PEAK_KW
                           * (1 + GAMMA*(t_cell-25)) * 0.78).clip(lower=0)
        return df.dropna()
    except: return pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# DEGRADATION FIT
# ─────────────────────────────────────────────────────────────────────────────
def fit_degradation(df_monthly: pd.DataFrame) -> Optional[dict]:
    """
    Linear regression on annual specific yield (kWh/kWp) vs year.
    Returns slope (%/year), intercept, r², projection dict.
    """
    try:
        from scipy.stats import linregress
        df_a = (df_monthly.groupby("Year")["Energy_kWh"].sum()
                .reset_index())
        df_a["Specific_Yield"] = df_a["Energy_kWh"] / PLANT_PEAK_KW
        if len(df_a) < 2: return None
        sl,ic,r,p,se = linregress(df_a["Year"], df_a["Specific_Yield"])
        pct_yr = sl / ic * 100
        return {"slope":sl,"intercept":ic,"r2":r**2,"pct_per_year":pct_yr,
                "data":df_a}
    except: return None

# ─────────────────────────────────────────────────────────────────────────────
# ML ANOMALY DETECTION (Isolation Forest)
# ─────────────────────────────────────────────────────────────────────────────
def run_anomaly_detection(df_all: pd.DataFrame) -> pd.DataFrame:
    """
    Isolation Forest on monthly aggregated telemetry features.
    Returns df with anomaly_score column (-1 = anomaly).
    """
    try:
        from sklearn.ensemble import IsolationForest
        feat_cols = []
        for c in df_all.columns:
            cl = c.lower()
            if any(k in cl for k in ["efficiency","power_factor","temperature",
                                     "active_power","elec_freq"]):
                feat_cols.append(c)
        if not feat_cols: return pd.DataFrame()
        df_f = df_all[feat_cols].apply(pd.to_numeric, errors="coerce").dropna()
        if len(df_f) < 10: return pd.DataFrame()
        clf = IsolationForest(contamination=0.1, random_state=42)
        df_f["anomaly"] = clf.fit_predict(df_f)
        df_f["score"]   = clf.score_samples(df_f[feat_cols])
        return df_f
    except: return pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# PDF REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_pdf_report(merged, df_bench, alert_rows, year) -> Optional[bytes]:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer, HRFlowable)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=2*cm, rightMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        H1 = ParagraphStyle("h1",parent=styles["Heading1"],
                            fontSize=18,textColor=colors.HexColor("#f0b429"))
        H2 = ParagraphStyle("h2",parent=styles["Heading2"],
                            fontSize=12,textColor=colors.HexColor("#3ecfcf"))
        BODY = ParagraphStyle("body",parent=styles["Normal"],fontSize=9)
        story=[]
        story.append(Paragraph(f"☀ FusionSolar APM — Monthly Report {year}", H1))
        story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", BODY))
        story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor("#334155")))
        story.append(Spacer(1,0.4*cm))

        # KPI table
        story.append(Paragraph("Performance Summary", H2))
        ytd_kwh  = merged["Energy_kWh"].sum(skipna=True)
        ytd_budg = merged["Budget_kWh"].sum()
        ach = ytd_kwh/ytd_budg*100 if ytd_budg else 0
        avg_pr = df_bench["PR"].mean() if "PR" in df_bench.columns else np.nan
        data=[["KPI","Value"],
              ["YTD Production (kWh)", f"{ytd_kwh:,.0f}"],
              ["YTD Budget (kWh)", f"{ytd_budg:,.0f}"],
              ["Achievement", f"{ach:.1f}%"],
              ["Avg PR", f"{avg_pr:.2f}" if not np.isnan(avg_pr) else "—"]]
        t=Table(data,colWidths=[9*cm,7*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1f2333")),
            ("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#f0b429")),
            ("FONTSIZE",(0,0),(-1,-1),9),
            ("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#334155")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),
             [colors.HexColor("#161b22"),colors.HexColor("#0e1117")]),
            ("TEXTCOLOR",(0,1),(-1,-1),colors.HexColor("#e2e8f0"))]))
        story.append(t)
        story.append(Spacer(1,0.5*cm))

        # Monthly detail
        story.append(Paragraph("Monthly Energy vs Budget", H2))
        hdr=["Month","Budget kWh","Actual kWh","Delta kWh","Achievement"]
        rows_=[hdr]
        for _,row in merged.iterrows():
            rows_.append([row["Month"],
                f"{row['Budget_kWh']:,.0f}",
                f"{row['Energy_kWh']:,.0f}" if pd.notna(row.get('Energy_kWh')) else "—",
                f"{row.get('Delta_kWh',np.nan):+,.0f}" if pd.notna(row.get('Delta_kWh')) else "—",
                f"{row.get('Achievement_%',np.nan):.1f}%" if pd.notna(row.get('Achievement_%')) else "—"])
        t2=Table(rows_,colWidths=[3*cm,3.5*cm,3.5*cm,3.5*cm,3*cm])
        t2.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1f2333")),
            ("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#f0b429")),
            ("FONTSIZE",(0,0),(-1,-1),8),
            ("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#334155")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),
             [colors.HexColor("#161b22"),colors.HexColor("#0e1117")]),
            ("TEXTCOLOR",(0,1),(-1,-1),colors.HexColor("#e2e8f0"))]))
        story.append(t2)
        story.append(Spacer(1,0.5*cm))

        # Alerts
        if alert_rows:
            story.append(Paragraph("Open Alerts", H2))
            ah=[["Timestamp","Severity","Category","Message"]]
            for a in alert_rows[:10]:
                ah.append([a[1],a[2],a[3],a[4][:80]])
            ta=Table(ah,colWidths=[3.5*cm,2.5*cm,3*cm,8*cm])
            ta.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1f2333")),
                ("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#fb7185")),
                ("FONTSIZE",(0,0),(-1,-1),7.5),
                ("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#334155")),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),
                 [colors.HexColor("#161b22"),colors.HexColor("#0e1117")]),
                ("TEXTCOLOR",(0,1),(-1,-1),colors.HexColor("#e2e8f0"))]))
            story.append(ta)

        doc.build(story)
        return buf.getvalue()
    except Exception as e:
        st.warning(f"PDF generation failed: {e}. Install reportlab: pip install reportlab")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# ██████████████████  MAIN UI  ████████████████████████████████████████████████
# ─────────────────────────────────────────────────────────────────────────────

# ── Sidebar ──
with st.sidebar:
    st.title("☀️ FusionSolar APM")
    year_input = int(st.number_input("Analysis Year",
                    min_value=PLANT_START_YEAR, max_value=2030, value=2025))
    st.caption(f"Capacity: {PLANT_PEAK_KW:.0f} kWp | COD: {PLANT_START_YEAR}")
    st.caption("📍 Thessaloniki (PVGIS-SARAH3)")
    st.divider()

    st.subheader("Finance Inputs")
    annual_debt  = st.number_input("Annual Debt Service (€)", value=ANNUAL_DEBT_SVC,
                                   step=5000.0, format="%.0f")
    fixed_opex   = st.number_input("Fixed OPEX/year (€)", value=FIXED_OPEX,
                                   step=1000.0, format="%.0f")
    var_opex     = st.number_input("Variable OPEX (€/MWh)", value=VAR_OPEX_PER_MWH,
                                   step=0.1, format="%.2f")
    ref_pr       = st.slider("Reference PR (irradiance model)", 0.60, 0.95, 0.78, 0.01)
    st.divider()

    if st.button("🔄 Reset Login"):
        st.session_state.pop("hw_client",None)
        st.session_state.pop("hw_stations",None)
        st.rerun()

# ── Tabs ──
(t_score, t_monthly, t_intraday, t_hist,
 t_loss, t_financial, t_forecast,
 t_health, t_failure, t_opex, t_alerts, t_ipto) = st.tabs([
    "🎯 Scorecard",
    "📊 Monthly",
    "📈 Intraday",
    "📉 Historical",
    "🔻 Loss Cascade",
    "💰 Financial",
    "🔮 Forecast",
    "🛠️ Health",
    "🩺 Failure Analytics",
    "🧾 OPEX",
    "🔔 Alerts",
    "🔌 IPTO Diagnostics",
])

# ─────────────────────────────────────────────────────────────────────────────
# TAB: SCORECARD  (Tier 1 — Executive KPI Strip)
# ─────────────────────────────────────────────────────────────────────────────
with t_score:
    st.header("Executive Performance Scorecard")
    st.caption("Live plant status — refresh to update all KPIs from the API.")

    if st.button("🔄 Load Scorecard", key="btn_score"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")

            with st.spinner("Fetching data…"):
                df_yr  = api_monthly(client.base_url, sid, year_input,
                                     client.xsrf, client.verify_ssl)
                df_py  = api_monthly(client.base_url, sid, year_input-1,
                                     client.xsrf, client.verify_ssl)

            if not df_yr.empty:
                tc, ec = _resolve(df_yr)
                if tc and ec:
                    df_yr["dt"]  = pd.to_datetime(df_yr[tc],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
                    df_yr["kWh"] = pd.to_numeric(df_yr[ec],errors="coerce")
                    df_yr["m"]   = df_yr["dt"].dt.month

                    ytd_kwh  = df_yr["kWh"].sum(skipna=True)
                    bdf      = get_budget(year_input)
                    ytd_budg = bdf["Budget_kWh"].sum()
                    ach_pct  = ytd_kwh/ytd_budg*100 if ytd_budg else 0

                    # PR from PVGIS
                    gti_ytd  = sum(PVGIS_GTI[m] for m in df_yr["m"])
                    pr_ytd   = ytd_kwh/(gti_ytd*PLANT_PEAK_KW) if gti_ytd else np.nan

                    # WCPR
                    wcpr_vals=[]
                    for _,row in df_yr.iterrows():
                        g = PVGIS_GTI.get(int(row["m"]),0)
                        ta= T_AMB.get(int(row["m"]),20)
                        if g>0: wcpr_vals.append(wcpr(row["kWh"],g,ta))
                    wcpr_avg = float(np.nanmean(wcpr_vals)) if wcpr_vals else np.nan

                    # YoY delta
                    py_kwh = np.nan
                    if not df_py.empty:
                        tc2,ec2 = _resolve(df_py)
                        if tc2 and ec2:
                            py_kwh = pd.to_numeric(df_py[ec2],errors="coerce").sum()

                    # Alerts
                    conn = _get_db()
                    n_alerts = conn.execute(
                        "SELECT COUNT(*) FROM alerts WHERE status='Open'").fetchone()[0]
                    conn.close()

                    # RAG colour for PR
                    def _rag(v):
                        if np.isnan(v): return "⚫"
                        if v>=PR_ALERT_AMBER: return "🟢"
                        if v>=PR_ALERT_RED:   return "🟡"
                        return "🔴"

                    # Scorecard row
                    c1,c2,c3,c4,c5,c6 = st.columns(6)
                    c1.metric("YTD Production",f"{ytd_kwh/1e6:.3f} GWh",
                              delta=f"{(ytd_kwh-py_kwh)/1e3:.0f} MWh vs LY" if not np.isnan(py_kwh) else None)
                    c2.metric("vs Budget",f"{ach_pct:.1f}%",
                              delta=f"{ytd_kwh-ytd_budg:+,.0f} kWh")
                    c3.metric(f"PR {_rag(pr_ytd)}",f"{pr_ytd:.3f}" if not np.isnan(pr_ytd) else "—")
                    c4.metric("WCPR ⭐",f"{wcpr_avg:.3f}" if not np.isnan(wcpr_avg) else "—",
                              help="Weather-corrected PR — IEC 61724")
                    c5.metric("Specific Yield",
                              f"{ytd_kwh/PLANT_PEAK_KW:.0f} kWh/kWp")
                    c6.metric("Open Alerts",str(n_alerts),
                              delta="⚠️" if n_alerts>0 else None,
                              delta_color="inverse" if n_alerts>0 else "normal")

                    st.divider()

                    # Monthly bar chart with PR overlay
                    fig = make_subplots(specs=[[{"secondary_y":True}]])
                    bdf2 = get_budget(year_input)
                    fig.add_bar(x=MONTH_LABELS, y=bdf2["Budget_kWh"],
                                name="Budget", marker_color="#334155",
                                secondary_y=False)
                    fig.add_bar(x=[MONTH_LABELS[r["m"]-1] for _,r in df_yr.iterrows()],
                                y=df_yr["kWh"],
                                name="Actual", marker_color="#f0b429",
                                secondary_y=False)
                    # PR per month
                    pr_vals = [wcpr(row["kWh"],PVGIS_GTI.get(int(row["m"]),1),
                                    T_AMB.get(int(row["m"]),20))
                               for _,row in df_yr.iterrows()]
                    fig.add_scatter(
                        x=[MONTH_LABELS[r["m"]-1] for _,r in df_yr.iterrows()],
                        y=pr_vals, mode="lines+markers", name="WCPR",
                        line=dict(color="#3ecfcf",width=2),
                        marker=dict(size=7), secondary_y=True)
                    fig.add_hline(y=PR_ALERT_AMBER, line_dash="dash",
                                  line_color="#4ade80",
                                  annotation_text=f"Target {PR_ALERT_AMBER}",
                                  secondary_y=True)
                    fig.add_hline(y=PR_ALERT_RED, line_dash="dot",
                                  line_color="#ff5f5f",
                                  annotation_text=f"Alert {PR_ALERT_RED}",
                                  secondary_y=True)
                    _dual_layout(fig,
                        title=f"{year_input} Monthly Production & WCPR",
                        left_title="Energy (kWh)", right_title="WCPR",
                        left_color="#f0b429", right_color="#3ecfcf")
                    fig.update_layout(barmode="group")
                    st.plotly_chart(fig, use_container_width=True)

                    st.session_state["scorecard_merged"] = bdf2.merge(
                        df_yr[["m","kWh"]].rename(columns={"m":"MonthNum","kWh":"Energy_kWh"}),
                        on="MonthNum", how="left")
                    st.session_state["scorecard_year"] = year_input

# ─────────────────────────────────────────────────────────────────────────────
# TAB: MONTHLY  (production vs budget + revenue)
# ─────────────────────────────────────────────────────────────────────────────
with t_monthly:
    st.header("Monthly Energy vs Budget")

    if st.button("Refresh Monthly", key="btn_monthly"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")

            with st.spinner("Fetching…"):
                df_raw = api_monthly(client.base_url, sid, year_input,
                                     client.xsrf, client.verify_ssl)
                df_prev= api_monthly(client.base_url, sid, year_input-1,
                                     client.xsrf, client.verify_ssl)

            if df_raw.empty:
                st.warning("No monthly data."); st.stop()
            tc,ec = _resolve(df_raw)
            if not tc or not ec:
                st.error(f"Columns: {list(df_raw.columns)}"); st.stop()

            df_raw["dt"]  = pd.to_datetime(df_raw[tc],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
            df_raw["m"]   = df_raw["dt"].dt.month
            df_raw["kWh"] = pd.to_numeric(df_raw[ec],errors="coerce")
            bdf = get_budget(year_input)
            merged = bdf.merge(df_raw[["m","kWh"]].rename(
                columns={"m":"MonthNum","kWh":"Energy_kWh"}),
                on="MonthNum", how="left")
            merged["Delta_kWh"]     = merged["Energy_kWh"]-merged["Budget_kWh"]
            merged["Achievement_%"] = (
                merged["Energy_kWh"].astype(float)
                / merged["Budget_kWh"].astype(float).where(merged["Budget_kWh"] != 0)
                * 100
            ).round(1)

            # DAM prices
            today = date.today()
            dam_map={}
            comp_m=[m for m in range(1,13) if date(year_input,m,1)<today]
            if comp_m:
                with st.spinner("Fetching ADMIE monthly DAM prices…"):
                    for m in comp_m:
                        dam_map[m]=dam_monthly_avg(year_input,m)
            merged["Avg_DAM"]=merged["MonthNum"].map(dam_map)
            merged["Revenue_EUR"]=merged.apply(
                lambda r: r["Energy_kWh"]*r["Avg_DAM"]/1000
                if pd.notna(r["Avg_DAM"]) and r["Avg_DAM"]>0
                and pd.notna(r["Energy_kWh"]) else pd.NA, axis=1)

            # Rolling 12-mo
            frames=[]
            for df_y,yr_l in [(df_prev,year_input-1),(df_raw,year_input)]:
                if df_y.empty: continue
                t2,e2=_resolve(df_y)
                if t2 and e2:
                    tmp=df_y[[t2,e2]].copy(); tmp.columns=["ts","kWh"]
                    tmp["dt"]=pd.to_datetime(tmp["ts"],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
                    tmp["m"]=tmp["dt"].dt.month; tmp["yr"]=yr_l
                    tmp["kWh"]=pd.to_numeric(tmp["kWh"],errors="coerce")
                    frames.append(tmp)

            # Chart
            fig=go.Figure()
            fig.add_bar(x=merged["Month"],y=merged["Budget_kWh"],
                        name="Budget",marker_color="#334155")
            fig.add_bar(x=merged["Month"],y=merged["Energy_kWh"],
                        name="Actual",marker_color="#f0b429")
            if frames:
                dc=pd.concat(frames).sort_values("dt").reset_index(drop=True)
                dc["Roll12"]=dc["kWh"].rolling(12,min_periods=3).mean()
                dy=dc[dc["yr"]==year_input]
                if not dy.empty:
                    fig.add_scatter(x=[MONTH_LABELS[m-1] for m in dy["m"]],
                                    y=dy["Roll12"].values,
                                    mode="lines+markers",name="12-mo Rolling",
                                    line=dict(color="#f472b6",width=2.5,dash="dot"))
            _base_layout(fig,f"{year_input} Monthly Energy vs Budget",
                         "Month","kWh",barmode="group")
            st.plotly_chart(fig,use_container_width=True)

            # Delta chart
            dc2=[("🟢" if v>=0 else "🔴","#4ade80" if v>=0 else "#ff5f5f")
                 for v in merged["Delta_kWh"].fillna(0)]
            fig2=go.Figure(go.Bar(x=merged["Month"],y=merged["Delta_kWh"],
                marker_color=[c[1] for c in dc2],name="Δ vs Budget"))
            _base_layout(fig2,"Monthly Delta (Actual − Budget)","Month","kWh")
            st.plotly_chart(fig2,use_container_width=True)

            # Summary table
            has_dam=merged["Avg_DAM"].notna().any()
            dcols=["Month","Budget_kWh","Energy_kWh","Delta_kWh","Achievement_%"]
            fmt={"Budget_kWh":"{:,.0f}","Energy_kWh":"{:,.0f}",
                 "Delta_kWh":"{:+,.0f}","Achievement_%":"{:.1f}%"}
            if has_dam:
                dcols+=["Avg_DAM","Revenue_EUR"]
                fmt["Avg_DAM"]="{:.2f}"; fmt["Revenue_EUR"]="{:,.0f}"
                merged=merged.rename(columns={"Avg_DAM":"Avg DAM (€/MWh)",
                                              "Revenue_EUR":"Revenue (€)"})
                dcols=[c if c not in("Avg_DAM","Revenue_EUR")
                       else("Avg DAM (€/MWh)" if c=="Avg_DAM" else "Revenue (€)")
                       for c in dcols]
                ytd_rev=merged["Revenue (€)"].sum(skipna=True)
                st.metric("YTD Estimated Revenue",f"€ {ytd_rev:,.0f}")

            st.subheader("Summary Table")
            st.dataframe(merged[dcols].style.format(fmt,na_rep="—"),
                         use_container_width=True)

            # PDF download
            conn=_get_db()
            alert_rows=conn.execute(
                "SELECT * FROM alerts ORDER BY ts DESC LIMIT 20").fetchall()
            conn.close()
            pvg=pvgis_df([year_input],ref_pr)
            merged2=merged.rename(columns={"Energy_kWh_x":"Energy_kWh"}) \
                if "Energy_kWh_x" in merged.columns else merged
            pdf_bytes=build_pdf_report(merged2, pvg, alert_rows, year_input)
            if pdf_bytes:
                st.download_button("📥 Download PDF Report",pdf_bytes,
                    file_name=f"APM_Report_{year_input}.pdf",
                    mime="application/pdf")

# ─────────────────────────────────────────────────────────────────────────────
# TAB: INTRADAY  (15-min + DAM price dual-axis)
# ─────────────────────────────────────────────────────────────────────────────
with t_intraday:
    st.header("15-min Production vs Day-Ahead Price")
    c1,c2 = st.columns([2,1])
    with c1:
        tgt_date=st.date_input("Day",value=date.today()-timedelta(days=1),
                               key="id_date")
    with c2:
        show_rev=st.checkbox("Show revenue curve",value=True)

    if st.button("Generate",key="btn_id"):
        client, stations = ensure_client()
        if client and stations:
            sid=stations[0].get("stationCode") or stations[0].get("plantCode")
            with st.spinner("Fetching 15-min data…"):
                jq=api_15min(client.base_url,sid,tgt_date,client.xsrf,client.verify_ssl)
            with st.spinner("Fetching DAM prices…"):
                df_dam=dam_daily(tgt_date)

            raw=jq.get("data",[])
            src=jq.get("_src","hour")
            if raw and isinstance(raw,list) and "kpiList" in (raw[0] if isinstance(raw[0],dict) else {}):
                raw=raw[0]["kpiList"]
            df_p=_norm(raw)
            if df_p.empty:
                st.warning("No production data."); st.stop()

            tc=next((c for c in df_p.columns if "time" in c.lower() or "collect" in c.lower()),None)
            yc=next((c for c in df_p.columns if "inverterYield" in c or "activePower" in c
                     or "day_cap" in c or "power" in c.lower()),None)
            if not tc or not yc:
                st.error(f"Columns: {list(df_p.columns)}"); st.stop()

            df_p["dt"]=pd.to_datetime(df_p[tc],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
            df_p[yc]=pd.to_numeric(df_p[yc],errors="coerce")
            df_p=df_p.sort_values("dt").reset_index(drop=True)

            if src=="hour" and len(df_p)<=25:
                df_p=(df_p.set_index("dt")[[yc]].resample("15min").asfreq()
                      [yc].interpolate("linear").reset_index())
                df_p.columns=["dt",yc]
                st.caption("⚠️ Upsampled from hourly (15-min endpoint unavailable).")
            else:
                st.caption(f"✅ Native 15-min resolution ({len(df_p)} intervals).")

            dam_ok=not df_dam.empty and "price" in df_dam.columns
            fig=make_subplots(specs=[[{"secondary_y":True}]])
            fig.add_trace(go.Scatter(x=df_p["dt"],y=df_p[yc],fill="tozeroy",
                line=dict(color="#f0b429",width=2),
                fillcolor="rgba(240,180,41,0.12)",
                name="Production (kWh)"),secondary_y=False)

            if dam_ok:
                src_lbl="SMP €/MWh" if "ISP" in str(df_dam.get("_src",["DAM"])) else "DAM MCP €/MWh"
                fig.add_trace(go.Scatter(x=df_dam["dt"],y=df_dam["price"],
                    mode="lines",line=dict(color="#3ecfcf",width=2,shape="hv"),
                    name=src_lbl),secondary_y=True)

                if show_rev:
                    dr=pd.merge_asof(df_p[["dt",yc]].sort_values("dt"),
                                     df_dam[["dt","price"]].sort_values("dt"),
                                     on="dt",direction="nearest",
                                     tolerance=pd.Timedelta("8min"))
                    dr["rev"]=dr[yc]*dr["price"]/1000
                    fig.add_trace(go.Scatter(x=dr["dt"],y=dr["rev"],mode="lines",
                        line=dict(color="#a78bfa",width=1.5,dash="dot"),
                        name="Revenue (€/interval)"),secondary_y=True)

                    tot_rev=dr["rev"].sum(); tot_kwh=dr[yc].sum()
                    avg_p=df_dam["price"].mean(); pk_p=df_dam["price"].max()
                    hi_thr=df_dam["price"].quantile(0.75)
                    hi_pct=(dr.loc[dr["price"]>=hi_thr,yc].sum()/tot_kwh*100
                            if tot_kwh>0 else 0)
                    m1,m2,m3,m4=st.columns(4)
                    m1.metric("Total Production",f"{tot_kwh:,.0f} kWh")
                    m2.metric("Est. Revenue",f"€ {tot_rev:,.1f}")
                    m3.metric("Avg DAM Price",f"{avg_p:.1f} €/MWh")
                    m4.metric("Peak DAM Price",f"{pk_p:.1f} €/MWh")
                    if tot_kwh>0:
                        st.caption(f"📊 {hi_pct:.1f}% of production in top-quartile "
                                   f"price window (≥{hi_thr:.1f} €/MWh).")

                    # Capture rate
                    cap_price=(dr[yc]*dr["price"]).sum()/dr[yc].sum() if dr[yc].sum()>0 else np.nan
                    cap_rate=cap_price/avg_p if avg_p>0 else np.nan
                    st.info(f"💡 **Capture Price:** {cap_price:.2f} €/MWh  |  "
                            f"**Capture Rate:** {cap_rate:.1%}  "
                            f"(1.0 = perfectly aligned with market)")
            else:
                st.info("DAM prices unavailable — production curve only.")

            _dual_layout(fig,f"15-min Production & DAM Price — {tgt_date}",
                "⚡ Production (kWh)","💰 Price (€/MWh) · Revenue (€)")
            st.plotly_chart(fig,use_container_width=True)

            with st.expander("📋 15-min Data Table"):
                if dam_ok:
                    ds2=pd.merge_asof(df_p[["dt",yc]].sort_values("dt"),
                                      df_dam[["dt","price"]].sort_values("dt"),
                                      on="dt",direction="nearest",
                                      tolerance=pd.Timedelta("8min"))
                    ds2["rev"]=ds2[yc]*ds2["price"]/1000
                    ds2["Time"]=ds2["dt"].dt.strftime("%H:%M")
                    st.dataframe(ds2[["Time",yc,"price","rev"]].rename(columns={
                        yc:"Production",
                        "price":"DAM (€/MWh)","rev":"Revenue (€)"}
                    ).style.format({"Production":"{:,.2f}","DAM (€/MWh)":"{:.2f}",
                                    "Revenue (€)":"{:.3f}"}),
                        use_container_width=True,hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# TAB: HISTORICAL  (multi-year, PR, WCPR, degradation, irradiance)
# ─────────────────────────────────────────────────────────────────────────────
with t_hist:
    st.header("Historical Performance Deep-Dive")
    all_years=list(range(PLANT_START_YEAR,date.today().year+1))
    hist_years=st.multiselect("Years",options=all_years,default=all_years)

    if not hist_years:
        st.info("Select years above.")
    elif st.button("Load Historical Data",key="btn_hist"):
        client, stations = ensure_client()
        if client and stations:
            sid=stations[0].get("stationCode") or stations[0].get("plantCode")
            frames=[]
            prog=st.progress(0,"Fetching…")
            for i,yr in enumerate(sorted(hist_years)):
                df_y=api_monthly(client.base_url,sid,yr,client.xsrf,client.verify_ssl)
                if not df_y.empty:
                    df_y=df_y.copy(); df_y["_yr"]=yr; frames.append(df_y)
                prog.progress((i+1)/len(hist_years),f"Fetched {yr}")
            prog.empty()
            if not frames: st.warning("No data."); st.stop()

            df_all=pd.concat(frames,ignore_index=True)
            tc,ec=_resolve(df_all)
            if not tc or not ec: st.error(f"Cols: {list(df_all.columns)}"); st.stop()

            temp_col=next((c for c in df_all.columns if "temperature" in c.lower()),None)
            df_all["dt"]=pd.to_datetime(df_all[tc],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
            df_all["YM"]=df_all["dt"].dt.to_period("M").astype(str)
            df_all["m"]=df_all["dt"].dt.month
            df_all["yr"]=df_all["_yr"]
            df_all["kWh"]=pd.to_numeric(df_all[ec],errors="coerce")
            df_all=df_all.sort_values("dt").reset_index(drop=True)
            if temp_col:
                df_all["Temp"]=pd.to_numeric(df_all[temp_col],errors="coerce")

            pvg=pvgis_df(hist_years,ref_pr)
            df_mo=(df_all.groupby("YM")["kWh"].sum()
                   .reset_index().sort_values("YM").reset_index(drop=True))
            df_mo["m"]=df_mo["YM"].apply(lambda x:int(x.split("-")[1]))
            df_mo["yr"]=df_mo["YM"].apply(lambda x:int(x.split("-")[0]))
            df_mo["Year"]=df_mo["yr"]
            df_mo.rename(columns={"kWh":"Energy_kWh"},inplace=True)

            df_b=df_mo.merge(pvg[["YearMonth","GTI","T_amb","Expected_kWh"]].rename(
                columns={"YearMonth":"YM"}),on="YM",how="left")
            df_b["PR"]=df_b["Energy_kWh"]/(df_b["GTI"]*PLANT_PEAK_KW)
            df_b["WCPR"]=df_b.apply(lambda r:wcpr(r["Energy_kWh"],r["GTI"],r["T_amb"]),axis=1)
            df_b["Ratio_%"] = (
                df_b["Energy_kWh"].astype(float)
                / df_b["Expected_kWh"].astype(float).where(df_b["Expected_kWh"] != 0)
                * 100
            ).round(1)
            df_b["Roll12"]=df_b["Energy_kWh"].rolling(12,min_periods=3).mean()
            df_b["PR_R12"]=df_b["PR"].rolling(12,min_periods=3).mean()
            df_b["WCPR_R12"]=df_b["WCPR"].rolling(12,min_periods=3).mean()

            # Run alert checks
            run_alert_checks(df_b)

            # ① YoY chart
            st.subheader("① Year-over-Year Monthly Energy")
            fig1=go.Figure()
            for i,yr in enumerate(sorted(hist_years)):
                dy=df_mo[df_mo["yr"]==yr].sort_values("m")
                fig1.add_scatter(x=dy["m"],y=dy["Energy_kWh"],mode="lines+markers",
                    name=str(yr),line=dict(color=PALETTE[i%len(PALETTE)],width=2))
            bdf_r=get_budget(sorted(hist_years)[-1])
            fig1.add_scatter(x=list(range(1,13)),y=bdf_r["Budget_kWh"],
                mode="lines",name="Budget",line=dict(color="#475569",dash="dash"))
            fig1.add_scatter(x=df_b["m"],y=df_b["Roll12"],mode="lines",
                name="12-mo Rolling",line=dict(color="#f472b6",width=2,dash="dot"))
            _base_layout(fig1,"Monthly Energy per Year","Month","kWh")
            fig1.update_xaxes(tickmode="array",tickvals=list(range(1,13)),
                              ticktext=MONTH_LABELS)
            st.plotly_chart(fig1,use_container_width=True)

            # ② Cumulative
            st.subheader("② Cumulative Energy Since COD")
            df_b["Cum_MWh"]=df_b["Energy_kWh"].cumsum()/1000
            fig2=go.Figure(go.Scatter(x=df_b["YM"],y=df_b["Cum_MWh"],
                fill="tozeroy",line_color="#3ecfcf",
                fillcolor="rgba(62,207,207,0.12)",name="Cumulative MWh"))
            _base_layout(fig2,"Cumulative Energy (MWh)","","MWh")
            st.plotly_chart(fig2,use_container_width=True)
            st.metric("Total Production to Date",
                      f"{df_b['Cum_MWh'].iloc[-1]:,.1f} MWh")

            # ③ PR + WCPR trend
            st.subheader("③ Performance Ratio (PR) & Weather-Corrected PR (WCPR)")
            fig3=go.Figure()
            fig3.add_scatter(x=df_b["YM"],y=df_b["PR"],mode="lines+markers",
                line=dict(color="#60a5fa",width=1.5),name="Monthly PR",marker=dict(size=4))
            fig3.add_scatter(x=df_b["YM"],y=df_b["WCPR"],mode="lines+markers",
                line=dict(color="#f0b429",width=1.5,dash="dot"),
                name="Monthly WCPR",marker=dict(size=4))
            fig3.add_scatter(x=df_b["YM"],y=df_b["PR_R12"],mode="lines",
                name="PR 12-mo avg",line=dict(color="#a78bfa",width=2,dash="dot"))
            fig3.add_scatter(x=df_b["YM"],y=df_b["WCPR_R12"],mode="lines",
                name="WCPR 12-mo avg",line=dict(color="#fb923c",width=2,dash="dot"))
            fig3.add_hline(y=0.75,line_dash="dash",line_color="#4ade80",
                           annotation_text="Target 0.75")
            fig3.add_hline(y=0.65,line_dash="dot",line_color="#ff5f5f",
                           annotation_text="Alert 0.65")
            _base_layout(fig3,"PR & WCPR Trend","","PR / WCPR")
            fig3.update_yaxes(tickformat=".0%")
            st.plotly_chart(fig3,use_container_width=True)

            # ④ Irradiance benchmark
            st.subheader("④ Irradiance Benchmark — Actual vs PVGIS Expected")
            fig4=make_subplots(rows=2,cols=1,shared_xaxes=True,
                vertical_spacing=0.1,
                subplot_titles=["kWh: Actual vs Expected","Actual / Expected %"])
            fig4.add_bar(x=df_b["YM"],y=df_b["Expected_kWh"],
                name="PVGIS Expected",marker_color="#334155",row=1,col=1)
            fig4.add_bar(x=df_b["YM"],y=df_b["Energy_kWh"],
                name="Actual",marker_color="#f0b429",row=1,col=1)
            fig4.add_scatter(x=df_b["YM"],y=df_b["Roll12"],mode="lines",
                name="12-mo Actual",line=dict(color="#f472b6",width=2,dash="dot"),row=1,col=1)
            rc=["#4ade80" if v>=100 else "#fb923c" if v>=85 else "#ff5f5f"
                for v in df_b["Ratio_%"].fillna(0)]
            fig4.add_bar(x=df_b["YM"],y=df_b["Ratio_%"],
                name="Actual/Expected %",marker_color=rc,row=2,col=1)
            fig4.add_hline(y=100,line_dash="dash",line_color="#4ade80",row=2,col=1)
            fig4.add_hline(y=85,line_dash="dot",line_color="#ff5f5f",
                           annotation_text="85% alert",row=2,col=1)
            fig4.update_layout(barmode="group",height=500,
                paper_bgcolor=_BG,plot_bgcolor=_BG,font=dict(color=_TXT),
                legend=dict(bgcolor="rgba(0,0,0,0)"),margin=dict(t=55,b=40,l=10,r=10))
            fig4.update_xaxes(gridcolor=_GRD); fig4.update_yaxes(gridcolor=_GRD)
            st.plotly_chart(fig4,use_container_width=True)

            # ⑤ Degradation tracker
            st.subheader("⑤ Degradation Rate Analysis")
            deg=fit_degradation(df_mo)
            if deg:
                c1,c2,c3=st.columns(3)
                pct=deg["pct_per_year"]
                c1.metric("Degradation Rate",f"{pct:.2f}%/year",
                          delta="⚠️ Above typical" if abs(pct)>0.8 else "✅ Within range",
                          delta_color="inverse" if abs(pct)>0.8 else "normal",
                          help="Typical crystalline-Si: 0.5–0.7%/year")
                c2.metric("R²",f"{deg['r2']:.3f}")
                projected_20yr=(deg["intercept"]+deg["slope"]*
                                (sorted(hist_years)[-1]+20))
                c3.metric("Projected Yield in 20yr",
                          f"{projected_20yr:,.0f} kWh/kWp")

                da=deg["data"]
                x_fit=np.linspace(da["Year"].min(),da["Year"].max()+1,50)
                y_fit=deg["intercept"]+deg["slope"]*x_fit
                figD=go.Figure()
                figD.add_scatter(x=da["Year"],y=da["Specific_Yield"],
                    mode="markers+lines",name="Annual Specific Yield",
                    line=dict(color="#f0b429",width=2))
                figD.add_scatter(x=x_fit,y=y_fit,mode="lines",
                    name=f"Trend ({pct:.2f}%/yr)",
                    line=dict(color="#ff5f5f",dash="dash",width=2))
                # 90% CI band
                se=np.std(da["Specific_Yield"].values-
                          (deg["intercept"]+deg["slope"]*da["Year"].values))
                figD.add_scatter(x=np.concatenate([x_fit,x_fit[::-1]]),
                    y=np.concatenate([y_fit+1.645*se,(y_fit-1.645*se)[::-1]]),
                    fill="toself",fillcolor="rgba(255,95,95,0.08)",
                    line=dict(color="rgba(0,0,0,0)"),name="90% CI",showlegend=True)
                _base_layout(figD,"Annual Specific Yield & Degradation Trend",
                             "Year","kWh/kWp")
                st.plotly_chart(figD,use_container_width=True)
            else:
                st.info("Need ≥2 years of data for degradation analysis.")

            # ⑥ ML Anomaly detection
            st.subheader("⑥ Anomaly Detection (Isolation Forest)")
            df_an=run_anomaly_detection(df_all)
            if not df_an.empty:
                n_anom=(df_an["anomaly"]==-1).sum()
                st.metric("Anomalous months detected",str(n_anom))
                figA=go.Figure()
                normal=df_an[df_an["anomaly"]==1]
                anom  =df_an[df_an["anomaly"]==-1]
                figA.add_scatter(x=normal.index,y=normal["score"],
                    mode="markers",name="Normal",
                    marker=dict(color="#4ade80",size=7))
                figA.add_scatter(x=anom.index,y=anom["score"],
                    mode="markers",name="Anomaly",
                    marker=dict(color="#ff5f5f",size=10,symbol="x"))
                _base_layout(figA,"Anomaly Score by Month (lower = more anomalous)",
                             "Month index","Score")
                st.plotly_chart(figA,use_container_width=True)
                if n_anom>0:
                    add_alert("Warning","Anomaly",
                        f"Isolation Forest flagged {n_anom} anomalous months")
            else:
                st.info("Insufficient telemetry columns for ML anomaly detection. "
                        "Need efficiency, power_factor, or temperature data.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB: LOSS CASCADE  (IEC 61724 energy waterfall)
# ─────────────────────────────────────────────────────────────────────────────
with t_loss:
    st.header("Loss Cascade — IEC 61724 Energy Waterfall")
    st.caption("Decompose losses from theoretical yield to AC output. "
               "Configure soiling and wiring loss estimates below.")

    lc1,lc2,lc3=st.columns(3)
    soiling_pct = lc1.number_input("Soiling loss (%)",value=2.5,min_value=0.0,
                                    max_value=20.0,step=0.1,
                                    help="Annual average soiling loss estimate")/100
    wiring_pct  = lc2.number_input("DC wiring loss (%)",value=1.5,min_value=0.0,
                                    max_value=10.0,step=0.1)/100
    inv_eff     = lc3.number_input("Inverter efficiency (%)",value=97.5,
                                    min_value=80.0,max_value=100.0,step=0.1)/100

    cascade_year=st.selectbox("Year",options=list(range(PLANT_START_YEAR,
                                                        date.today().year+1)),
                               index=0,key="lc_year")

    if st.button("Compute Loss Cascade",key="btn_loss"):
        client, stations = ensure_client()
        if client and stations:
            sid=stations[0].get("stationCode") or stations[0].get("plantCode")
            with st.spinner("Fetching…"):
                df_yr=api_monthly(client.base_url,sid,cascade_year,
                                  client.xsrf,client.verify_ssl)
            if df_yr.empty: st.warning("No data."); st.stop()
            tc,ec=_resolve(df_yr)
            if not tc or not ec: st.error("Column detection failed."); st.stop()

            df_yr["dt"]=pd.to_datetime(df_yr[tc],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
            df_yr["m"]=df_yr["dt"].dt.month
            df_yr["kWh"]=pd.to_numeric(df_yr[ec],errors="coerce")
            actual_kwh=df_yr["kWh"].sum(skipna=True)

            # Annual GTI and T_amb
            gti_ann  =sum(PVGIS_GTI[m] for m in range(1,13))
            tamb_ann =sum(T_AMB[m] for m in range(1,13))/12

            # Ref yield = GTI × capacity (no losses)
            ref_yield      = gti_ann * PLANT_PEAK_KW

            # Temperature loss
            t_cell_avg = tamb_ann + (NOCT-20)/800 * (gti_ann*1000/8760)
            temp_loss_frac = abs(GAMMA * (t_cell_avg - 25))
            temp_loss_kwh  = ref_yield * temp_loss_frac

            # After temp
            after_temp = ref_yield - temp_loss_kwh

            # Soiling
            soil_loss  = after_temp * soiling_pct
            after_soil = after_temp - soil_loss

            # Inverter
            inv_loss   = after_soil * (1-inv_eff)
            after_inv  = after_soil * inv_eff

            # DC wiring
            wire_loss  = after_inv * wiring_pct
            after_wire = after_inv - wire_loss

            # Unexplained loss (residual between modelled and actual)
            unexplained = after_wire - actual_kwh
            unexplained = max(unexplained, 0)

            measures=["relative","relative","relative","relative","relative","total"]
            vals=[ref_yield,
                  -temp_loss_kwh, -soil_loss, -inv_loss, -wire_loss,
                  actual_kwh]
            labels=["Reference Yield",
                    f"Temperature Loss ({temp_loss_frac:.1%})",
                    f"Soiling Loss ({soiling_pct:.1%})",
                    f"Inverter Loss ({(1-inv_eff):.1%})",
                    f"DC Wiring Loss ({wiring_pct:.1%})",
                    "AC Output (Actual)"]
            colors_wf=(["#60a5fa"]+["#ff5f5f"]*4+["#4ade80"])

            fig_wf=go.Figure(go.Waterfall(
                orientation="v",measure=measures,
                x=labels,y=vals,
                connector=dict(line=dict(color="#334155",width=1)),
                increasing=dict(marker_color="#4ade80"),
                decreasing=dict(marker_color="#ff5f5f"),
                totals=dict(marker_color="#f0b429"),
                text=[f"{v:,.0f}" for v in vals],
                textposition="outside",
            ))
            _base_layout(fig_wf,f"IEC 61724 Loss Cascade — {cascade_year}","","kWh")
            fig_wf.update_layout(height=480)
            st.plotly_chart(fig_wf,use_container_width=True)

            pr_eff=actual_kwh/ref_yield if ref_yield>0 else 0
            c1,c2,c3,c4=st.columns(4)
            c1.metric("Reference Yield",f"{ref_yield:,.0f} kWh")
            c2.metric("AC Output",f"{actual_kwh:,.0f} kWh")
            c3.metric("System PR",f"{pr_eff:.3f}")
            c4.metric("Unexplained Residual",
                      f"{unexplained:,.0f} kWh",
                      help="Gap between bottom-up model and actual — "
                           "investigate shading, clipping, or curtailment.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB: FINANCIAL  (Revenue waterfall, Capture Rate, DSCR)
# ─────────────────────────────────────────────────────────────────────────────
with t_financial:
    st.header("Financial Performance & Covenant Monitor")

    if st.button("Load Financial Data",key="btn_fin"):
        client, stations = ensure_client()
        if client and stations:
            sid=stations[0].get("stationCode") or stations[0].get("plantCode")
            all_years=list(range(PLANT_START_YEAR,date.today().year+1))
            frames=[]
            with st.spinner("Fetching all years…"):
                for yr in all_years:
                    df_y=api_monthly(client.base_url,sid,yr,
                                     client.xsrf,client.verify_ssl)
                    if not df_y.empty:
                        df_y=df_y.copy(); df_y["_yr"]=yr; frames.append(df_y)
            if not frames: st.warning("No data."); st.stop()

            df_all=pd.concat(frames,ignore_index=True)
            tc,ec=_resolve(df_all)
            df_all["dt"]=pd.to_datetime(df_all[tc],unit="ms",utc=True).dt.tz_convert(PLANT_TZ)
            df_all["m"]=df_all["dt"].dt.month; df_all["yr"]=df_all["_yr"]
            df_all["kWh"]=pd.to_numeric(df_all[ec],errors="coerce")
            df_mo=(df_all.groupby(["yr","m"])["kWh"].sum()
                   .reset_index().sort_values(["yr","m"]))
            df_mo["YM"]=df_mo.apply(lambda r:f"{int(r['yr'])}-{int(r['m']):02d}",axis=1)

            # Fetch monthly DAM prices
            with st.spinner("Fetching monthly DAM prices (takes ~30s for all years)…"):
                prices={}
                today=date.today()
                for _,row in df_mo.iterrows():
                    k=(int(row["yr"]),int(row["m"]))
                    if k not in prices and date(k[0],k[1],1)<today:
                        prices[k]=dam_monthly_avg(k[0],k[1])
            df_mo["DAM"]=df_mo.apply(lambda r:prices.get((int(r["yr"]),int(r["m"]))),axis=1)
            df_mo["Revenue"]=df_mo.apply(
                lambda r:r["kWh"]*r["DAM"]/1000
                if pd.notna(r["DAM"]) and r["DAM"]>0 else pd.NA, axis=1)
            df_mo["OPEX_EUR"]=(fixed_opex/12
                               + df_mo["kWh"]/1000*var_opex)
            df_mo["EBITDA"]=df_mo["Revenue"].fillna(0)-df_mo["OPEX_EUR"]

            # Rolling 12-month DSCR
            df_mo["Rev12"]  =df_mo["Revenue"].fillna(0).rolling(12,min_periods=6).sum()
            df_mo["OPEX12"] =df_mo["OPEX_EUR"].rolling(12,min_periods=6).sum()
            df_mo["DS12"]   =annual_debt
            df_mo["DSCR"]   =(df_mo["Rev12"]-df_mo["OPEX12"])/df_mo["DS12"]

            # ── Revenue Waterfall (annual) ──
            st.subheader("① Annual Revenue Waterfall")
            wa_year=st.selectbox("Waterfall year",options=all_years,
                                  index=len(all_years)-1,key="wa_yr")
            dy=df_mo[df_mo["yr"]==wa_year]

            pvg_yr=pvgis_df([wa_year],ref_pr)
            exp_rev_base=(pvg_yr["Expected_kWh"].sum()
                          *prices.get((wa_year,6),80)/1000
                          if prices.get((wa_year,6)) else None)

            act_rev =dy["Revenue"].sum(skipna=True)
            act_kwh =dy["kWh"].sum(skipna=True)
            exp_kwh =pvg_yr["Expected_kWh"].sum()
            avg_dam =dy["DAM"].mean(skipna=True)

            if avg_dam and not np.isnan(avg_dam):
                irr_shortfall =(exp_kwh-act_kwh)*avg_dam/1000
                irr_shortfall = max(0, irr_shortfall)
                wf_measures=["absolute","relative","relative","total"]
                wf_x=["PVGIS Expected Revenue",
                       "Irradiance / PR Shortfall",
                       "Price Variance",
                       "Actual Revenue"]
                wf_y=[exp_kwh*avg_dam/1000,
                      -irr_shortfall,
                      0,
                      act_rev]
                fig_rev=go.Figure(go.Waterfall(
                    orientation="v",measure=wf_measures,
                    x=wf_x,y=wf_y,
                    increasing=dict(marker_color="#4ade80"),
                    decreasing=dict(marker_color="#ff5f5f"),
                    totals=dict(marker_color="#f0b429"),
                    text=[f"€{v:,.0f}" for v in wf_y],
                    textposition="outside"))
                _base_layout(fig_rev,f"Revenue Waterfall — {wa_year}","","€")
                st.plotly_chart(fig_rev,use_container_width=True)

            # ── Capture Rate trend ──
            st.subheader("② Monthly Capture Rate")
            df_mo["Capture_Price"]=df_mo["DAM"]  # proxy — improve with 15-min data
            df_mo["Capture_Rate"]=df_mo["Capture_Price"]/df_mo["DAM"]
            figC=go.Figure()
            figC.add_scatter(x=df_mo["YM"],y=df_mo["Revenue"].fillna(0),
                mode="lines+markers",name="Monthly Revenue (€)",
                line=dict(color="#f0b429",width=2))
            figC.add_scatter(x=df_mo["YM"],y=df_mo["EBITDA"],
                mode="lines",name="EBITDA (€)",
                line=dict(color="#4ade80",width=1.5,dash="dot"))
            _base_layout(figC,"Monthly Revenue & EBITDA","","€")
            st.plotly_chart(figC,use_container_width=True)

            # ── DSCR Chart ──
            st.subheader("③ Rolling 12-Month DSCR")
            df_dscr=df_mo.dropna(subset=["DSCR"])
            figDSCR=go.Figure()
            figDSCR.add_scatter(x=df_dscr["YM"],y=df_dscr["DSCR"],
                mode="lines+markers",name="DSCR (trailing 12mo)",
                line=dict(color="#60a5fa",width=2),marker=dict(size=5))
            figDSCR.add_hline(y=1.20,line_dash="dash",line_color="#fb923c",
                              annotation_text="1.20x Lock-up trigger")
            figDSCR.add_hline(y=1.10,line_dash="dot",line_color="#ff5f5f",
                              annotation_text="1.10x Default trigger")
            figDSCR.add_hrect(y0=0,y1=1.10,fillcolor="rgba(255,95,95,0.05)",
                              line_width=0)
            _base_layout(figDSCR,"Debt Service Coverage Ratio (DSCR)","","DSCR")
            st.plotly_chart(figDSCR,use_container_width=True)

            last_dscr=df_dscr["DSCR"].iloc[-1] if not df_dscr.empty else np.nan
            if not np.isnan(last_dscr):
                col_a,col_b=st.columns(2)
                col_a.metric("Latest DSCR (trailing 12mo)",f"{last_dscr:.2f}x",
                    delta="⚠️ Below lock-up" if last_dscr<1.20 else "✅ Above lock-up",
                    delta_color="inverse" if last_dscr<1.20 else "normal")
                # Stress test
                rev_needed_for_lockup=(1.20*annual_debt+df_mo["OPEX12"].iloc[-1])
                rev_gap=rev_needed_for_lockup-df_mo["Rev12"].iloc[-1]
                col_b.metric("Revenue gap to lock-up trigger",
                    f"€ {abs(rev_gap):,.0f}",
                    delta="Surplus" if rev_gap<0 else "Deficit",
                    delta_color="normal" if rev_gap<0 else "inverse")

            if last_dscr<1.20 and not np.isnan(last_dscr):
                add_alert("Critical","DSCR",
                    f"Trailing 12-month DSCR = {last_dscr:.2f}x — below 1.20x lock-up threshold")

# ─────────────────────────────────────────────────────────────────────────────
# TAB: FORECAST  (Open-Meteo 7-day production + revenue)
# ─────────────────────────────────────────────────────────────────────────────
with t_forecast:
    st.header("7-Day Production & Revenue Forecast")
    st.caption("Production forecast: Open-Meteo GHI → GTI (×1.15 proxy) → yield model. "
               "Revenue forecast uses next-day ADMIE DAM if available, else trailing avg.")

    if st.button("Load Forecast",key="btn_fc"):
        with st.spinner("Fetching Open-Meteo forecast…"):
            df_fc=fetch_forecast()
        if df_fc.empty:
            st.warning("Could not fetch forecast (network may be blocked)."); st.stop()

        # Aggregate to daily
        df_fc["Date"]=df_fc["dt"].dt.date
        df_day=(df_fc.groupby("Date")
                .agg(Yield_kWh=("Yield_kWh","sum"),
                     Peak_GHI=("GHI_Wm2","max"),
                     Avg_Temp=("T_amb","mean"))
                .reset_index())
        df_day["Date"]=pd.to_datetime(df_day["Date"])

        # Fetch next-day DAM (D+1 published by ADMIE ~13:00)
        fc_dam={}
        with st.spinner("Checking ADMIE for next-day prices…"):
            for d in df_day["Date"].dt.date.tolist():
                p=dam_daily(d)
                if not p.empty and "price" in p.columns:
                    fc_dam[d]=p["price"].mean()
        if fc_dam:
            df_day["DAM"]=df_day["Date"].dt.date.map(fc_dam)
        else:
            # Fall back to trailing monthly average
            trail=dam_monthly_avg(date.today().year, date.today().month)
            df_day["DAM"]=trail

        df_day["Rev_EUR"]=df_day.apply(
            lambda r: r["Yield_kWh"]*r["DAM"]/1000
            if pd.notna(r["DAM"]) and r["DAM"]>0 else pd.NA, axis=1)

        # Chart
        figF=make_subplots(specs=[[{"secondary_y":True}]])
        figF.add_bar(x=df_day["Date"],y=df_day["Yield_kWh"],
            name="Forecast Yield (kWh)",marker_color="#f0b429",
            secondary_y=False)
        if df_day["Rev_EUR"].notna().any():
            figF.add_scatter(x=df_day["Date"],y=df_day["Rev_EUR"],
                mode="lines+markers",name="Est. Revenue (€)",
                line=dict(color="#3ecfcf",width=2),secondary_y=True)
        _dual_layout(figF,"7-Day Production & Revenue Forecast",
            "⚡ Yield (kWh)","💰 Est. Revenue (€)")
        st.plotly_chart(figF,use_container_width=True)

        # Hourly profile chart
        st.subheader("Hourly Yield Profile (7 days)")
        figH=go.Figure()
        for i,d in enumerate(df_fc["dt"].dt.date.unique()[:7]):
            dh=df_fc[df_fc["dt"].dt.date==d]
            figH.add_scatter(x=dh["dt"].dt.hour,y=dh["Yield_kWh"],
                mode="lines",name=str(d),
                line=dict(color=PALETTE[i%len(PALETTE)],width=1.5))
        _base_layout(figH,"Hourly Yield Profile by Day","Hour of Day","kWh")
        st.plotly_chart(figH,use_container_width=True)

        # Summary table
        st.dataframe(df_day.rename(columns={
            "Date":"Date","Yield_kWh":"Forecast kWh",
            "Peak_GHI":"Peak GHI (W/m²)","Avg_Temp":"Avg Temp (°C)",
            "DAM":"DAM (€/MWh)","Rev_EUR":"Est. Revenue (€)"})
            .style.format({"Forecast kWh":"{:,.0f}","Peak GHI (W/m²)":"{:.0f}",
                           "Avg Temp (°C)":"{:.1f}","DAM (€/MWh)":"{:.2f}",
                           "Est. Revenue (€)":"{:,.0f}"},na_rep="—"),
            use_container_width=True,hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# TAB: HEALTH  (Live diagnostics + strings + availability + soiling)
# ─────────────────────────────────────────────────────────────────────────────
with t_health:
    st.header("Equipment Health & Diagnostics")

    h_tab1, h_tab2, h_tab3 = st.tabs(["🔌 Live Diagnostics",
                                       "📅 Availability Log",
                                       "🌿 Soiling Optimiser"])

    with h_tab1:
        if st.button("Run Live Diagnostics",key="btn_diag"):
            client, stations = ensure_client()
            if client and stations:
                sid=stations[0].get("stationCode") or stations[0].get("plantCode")
                with st.spinner("Fetching device list…"):
                    j_devs=_post(client.s,
                                 f"{client.base_url}/thirdData/getDevList",
                                 {"stationCodes":sid})
                inv_ids=[d["id"] for d in j_devs.get("data",[])
                         if d.get("devTypeId") in INV_DEV_TYPE_IDS]
                if not inv_ids:
                    st.warning("No inverters found.")
                else:
                    df_kpi=api_realtime(client.base_url,1,tuple(inv_ids[:3]),
                                        client.xsrf,client.verify_ssl)
                    if df_kpi.empty:
                        st.warning("No real-time KPI data.")
                    else:
                        tech_map={"dataItemMap.active_power":"AC Power (kW)",
                                  "dataItemMap.temperature":"Temp (°C)",
                                  "dataItemMap.elec_freq":"Freq (Hz)",
                                  "dataItemMap.power_factor":"PF",
                                  "dataItemMap.efficiency":"Eff %"}
                        df_disp=df_kpi.rename(columns=tech_map)
                        avail=[v for v in tech_map.values() if v in df_disp.columns]
                        st.subheader("Inverter Telemetry")
                        st.dataframe(df_disp[avail],use_container_width=True)
                        # Persist snapshot for failure trend analysis
                        _save_telemetry_snapshot(df_kpi)

                        # Temp alert
                        if "Temp (°C)" in df_disp.columns:
                            max_temp=pd.to_numeric(df_disp["Temp (°C)"],errors="coerce").max()
                            if max_temp>INV_TEMP_WARN:
                                add_alert("Warning","Temperature",
                                    f"Inverter temperature {max_temp:.1f}°C > "
                                    f"{INV_TEMP_WARN}°C threshold")
                                st.warning(f"⚠️ Inverter temperature {max_temp:.1f}°C "
                                           f"exceeds {INV_TEMP_WARN}°C threshold!")

                        st.divider()
                        st.subheader("DC String Analysis")
                        string_cols=[c for c in df_kpi.columns if "pv" in c and "_i" in c]
                        if not string_cols:
                            st.info("No DC string columns found (pattern: pv*_i).")
                        else:
                            s_vals=df_kpi[string_cols].iloc[0].astype(float).sort_values(ascending=False)
                            active=s_vals[s_vals>0.1]
                            mean_a=active.mean() if len(active)>0 else 0
                            bar_c=["#ff5f5f" if v<=0.1
                                   else "#f0b429" if v<mean_a*0.88
                                   else "#4ade80" for v in s_vals.values]
                            figS=go.Figure(go.Bar(x=s_vals.index,y=s_vals.values,
                                                  marker_color=bar_c))
                            if mean_a>0:
                                figS.add_hline(y=mean_a,line_dash="dash",
                                    line_color="#60a5fa",
                                    annotation_text=f"Mean: {mean_a:.2f}A")
                            _base_layout(figS,"DC String Currents","String","Amperes")
                            st.plotly_chart(figS,use_container_width=True)

                            if len(active)>1:
                                cv=active.std()/active.mean()
                                low=active[active<active.mean()*0.88]
                                c1,c2,c3=st.columns(3)
                                c1.metric("Active Strings",len(active))
                                c2.metric("Mean Current",f"{active.mean():.2f} A")
                                c3.metric("CV (variation)",f"{cv:.1%}")
                                if cv>STRING_CV_WARN:
                                    add_alert("Warning","Strings",
                                        f"String CV {cv:.1%} > {STRING_CV_WARN:.0%} — "
                                        f"{len(low)} strings underperforming")
                                    st.error(f"⚠️ High variation (CV {cv:.1%}) — "
                                             f"{len(low)} string(s) >12% below mean: "
                                             f"{list(low.index)}")
                                else:
                                    st.success(f"✅ Strings balanced (CV {cv:.1%}).")

    with h_tab2:
        st.subheader("Downtime Log & Availability")
        with st.expander("➕ Log Downtime Event"):
            dn_c1,dn_c2=st.columns(2)
            dn_start=dn_c1.date_input("Start date",key="dn_start")
            dn_end  =dn_c2.date_input("End date",  key="dn_end")
            dn_inv  =st.text_input("Inverter / component","INV-01",key="dn_inv")
            dn_kwh  =st.number_input("Estimated lost kWh",0.0,step=10.0,key="dn_kwh")
            dn_cause=st.text_input("Cause","Grid fault",key="dn_cause")
            if st.button("Save Downtime",key="dn_save"):
                conn=_get_db()
                conn.execute("INSERT INTO downtime VALUES(NULL,?,?,?,?,?)",
                    (dn_start.isoformat(),dn_end.isoformat(),
                     dn_inv,dn_kwh,dn_cause))
                conn.commit(); conn.close()
                st.success("Logged.")

        conn=_get_db()
        rows=conn.execute("SELECT * FROM downtime ORDER BY start_dt DESC").fetchall()
        conn.close()
        if rows:
            df_dn=pd.DataFrame(rows,columns=["id","Start","End","Inverter",
                                              "Lost kWh","Cause"])
            st.dataframe(df_dn.drop(columns="id"),use_container_width=True,
                         hide_index=True)
            total_lost=df_dn["Lost kWh"].sum()
            st.metric("Total Logged Lost Production",f"{total_lost:,.0f} kWh")
        else:
            st.info("No downtime events logged yet.")

    with h_tab3:
        st.subheader("Soiling Loss & Cleaning Optimisation")
        st.caption("Model the financial break-even point for a cleaning campaign.")

        so_c1,so_c2,so_c3=st.columns(3)
        clean_cost =so_c1.number_input("Cleaning cost (€)",value=500.0,step=50.0)
        yield_rec  =so_c2.number_input("Yield recovery (%)",value=2.5,step=0.1)/100
        dam_ref    =so_c3.number_input("DAM reference price (€/MWh)",value=80.0,step=1.0)

        # Revenue recovered per cleaning
        daily_kwh_est=PLANT_PEAK_KW*4.5  # rough 4.5 peak-sun-hours
        monthly_kwh  =daily_kwh_est*30
        rec_kwh_mo   =monthly_kwh*yield_rec
        rec_rev_mo   =rec_kwh_mo*dam_ref/1000

        # Payback in months
        payback_mo = clean_cost/rec_rev_mo if rec_rev_mo>0 else float("inf")

        c1,c2,c3=st.columns(3)
        c1.metric("Monthly recovered energy",f"{rec_kwh_mo:,.0f} kWh")
        c2.metric("Monthly recovered revenue",f"€ {rec_rev_mo:,.0f}")
        c3.metric("Payback period",
                  f"{payback_mo:.1f} months" if payback_mo<99 else "Not viable",
                  delta="✅ Viable" if payback_mo<3 else "⚠️ Check frequency")

        # Soiling accumulation curve
        days=np.arange(0,180)
        # Exponential soiling model: loss = A(1 - exp(-k·d))
        A_soil=0.05; k_soil=0.02
        soil_curve=A_soil*(1-np.exp(-k_soil*days))
        rev_loss_curve=soil_curve*monthly_kwh/30*dam_ref/1000

        figSo=go.Figure()
        figSo.add_scatter(x=days,y=soil_curve*100,name="Soiling loss (%)",
            line=dict(color="#f0b429",width=2))
        figSo.add_scatter(x=days,y=rev_loss_curve,name="Daily Revenue Loss (€)",
            line=dict(color="#ff5f5f",width=2),yaxis="y2")
        figSo.add_vline(x=payback_mo*30 if payback_mo<6 else 90,
                        line_dash="dash",line_color="#4ade80",
                        annotation_text="Recommended cleaning trigger")
        figSo.update_layout(paper_bgcolor=_BG,plot_bgcolor=_BG,
            font=dict(color=_TXT),hovermode="x unified",
            yaxis=dict(title="Soiling Loss (%)",gridcolor=_GRD,
                       title_font=dict(color="#f0b429"),tickfont=dict(color="#f0b429")),
            yaxis2=dict(title="Daily Revenue Loss (€)",overlaying="y",side="right",
                        gridcolor="rgba(0,0,0,0)",
                        title_font=dict(color="#ff5f5f"),tickfont=dict(color="#ff5f5f")),
            title=dict(text="Soiling Accumulation & Revenue Impact",
                       font=dict(color=_TXT,size=15)),
            legend=dict(bgcolor="rgba(0,0,0,0)"),margin=dict(t=55,b=40,l=70,r=70))
        figSo.update_xaxes(title_text="Days Since Last Cleaning",gridcolor=_GRD)
        st.plotly_chart(figSo,use_container_width=True)

        # Log cleaning event
        with st.expander("➕ Log Cleaning Event"):
            cl_dt=st.date_input("Cleaning date",key="cl_dt")
            cl_cost=st.number_input("Cost (€)",value=clean_cost,step=50.0,key="cl_cost")
            cl_rec=st.number_input("Measured yield recovery (%)",value=2.5,
                                    step=0.1,key="cl_rec")
            cl_notes=st.text_input("Notes","Full array cleaning",key="cl_notes")
            if st.button("Save Cleaning Event",key="cl_save"):
                conn=_get_db()
                conn.execute("INSERT INTO cleaning VALUES(NULL,?,?,?,?)",
                    (cl_dt.isoformat(),cl_cost,cl_rec,cl_notes))
                conn.commit(); conn.close()
                st.success("Logged.")

        conn=_get_db()
        cl_rows=conn.execute("SELECT * FROM cleaning ORDER BY dt DESC").fetchall()
        conn.close()
        if cl_rows:
            df_cl=pd.DataFrame(cl_rows,columns=["id","Date","Cost (€)",
                                                  "Yield Recovery (%)","Notes"])
            st.dataframe(df_cl.drop(columns="id"),use_container_width=True,
                         hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# TAB: OPEX  (cost tracker + OPEX/MWh trend)
# ─────────────────────────────────────────────────────────────────────────────
with t_opex:
    st.header("OPEX Tracker")
    st.caption("Log O&M costs and track cost-per-MWh over time.")

    OPEX_CATS=["Routine O&M","Corrective Maintenance","Insurance",
               "Land Lease","Cleaning","Other"]

    with st.expander("➕ Add OPEX Entry"):
        op_c1,op_c2,op_c3=st.columns(3)
        op_dt  =op_c1.date_input("Date",key="op_dt")
        op_cat =op_c2.selectbox("Category",OPEX_CATS,key="op_cat")
        op_amt =op_c3.number_input("Amount (€)",0.0,step=100.0,key="op_amt")
        op_desc=st.text_input("Description","",key="op_desc")
        if st.button("Save OPEX",key="op_save"):
            conn=_get_db()
            conn.execute("INSERT INTO opex VALUES(NULL,?,?,?,?)",
                (op_dt.isoformat(),op_cat,op_desc,op_amt))
            conn.commit(); conn.close()
            st.success("OPEX entry saved.")

    conn=_get_db()
    opex_rows=conn.execute(
        "SELECT * FROM opex ORDER BY dt DESC").fetchall()
    conn.close()

    if opex_rows:
        df_op=pd.DataFrame(opex_rows,
                           columns=["id","Date","Category","Description","Amount (€)"])
        df_op["Date"]=pd.to_datetime(df_op["Date"])

        # Summary metrics
        total_opex=df_op["Amount (€)"].sum()
        ytd_opex=df_op[df_op["Date"].dt.year==date.today().year]["Amount (€)"].sum()
        c1,c2=st.columns(2)
        c1.metric("Total OPEX Logged",f"€ {total_opex:,.0f}")
        c2.metric("YTD OPEX",f"€ {ytd_opex:,.0f}")

        # Monthly trend
        df_op["YM"]=df_op["Date"].dt.to_period("M").astype(str)
        df_op_m=df_op.groupby(["YM","Category"])["Amount (€)"].sum().reset_index()
        figOP=go.Figure()
        for cat in OPEX_CATS:
            sub=df_op_m[df_op_m["Category"]==cat]
            if not sub.empty:
                figOP.add_bar(x=sub["YM"],y=sub["Amount (€)"],name=cat)
        _base_layout(figOP,"Monthly OPEX by Category","Month","€",barmode="stack")
        st.plotly_chart(figOP,use_container_width=True)

        # OPEX/MWh (need production data)
        if st.button("Calculate OPEX/MWh",key="opex_mwh"):
            client, stations = ensure_client()
            if client and stations:
                sid=stations[0].get("stationCode") or stations[0].get("plantCode")
                frames=[]
                for yr in list(range(PLANT_START_YEAR,date.today().year+1)):
                    df_y=api_monthly(client.base_url,sid,yr,
                                     client.xsrf,client.verify_ssl)
                    if not df_y.empty:
                        df_y=df_y.copy(); df_y["_yr"]=yr; frames.append(df_y)
                if frames:
                    df_all=pd.concat(frames,ignore_index=True)
                    tc,ec=_resolve(df_all)
                    if tc and ec:
                        df_all["dt"]=pd.to_datetime(df_all[tc],unit="ms",
                            utc=True).dt.tz_convert(PLANT_TZ)
                        df_all["YM"]=df_all["dt"].dt.to_period("M").astype(str)
                        df_all["kWh"]=pd.to_numeric(df_all[ec],errors="coerce")
                        df_prod_m=df_all.groupby("YM")["kWh"].sum().reset_index()
                        df_op_tot=df_op.groupby("YM")["Amount (€)"].sum().reset_index()
                        df_ratio=df_prod_m.merge(df_op_tot,on="YM",how="inner")
                        df_ratio["OPEX_MWh"]=(df_ratio["Amount (€)"]
                                              /df_ratio["kWh"]*1000)
                        figOM=go.Figure(go.Bar(x=df_ratio["YM"],
                            y=df_ratio["OPEX_MWh"],
                            marker_color=["#ff5f5f" if v>20 else "#4ade80"
                                          for v in df_ratio["OPEX_MWh"]]))
                        figOM.add_hline(y=15,line_dash="dash",line_color="#fb923c",
                            annotation_text="15 €/MWh guideline")
                        _base_layout(figOM,"OPEX per MWh","Month","€/MWh")
                        st.plotly_chart(figOM,use_container_width=True)

        st.subheader("OPEX Ledger")
        st.dataframe(df_op.drop(columns="id").style.format({"Amount (€)":"{:,.2f}"}),
                     use_container_width=True,hide_index=True)
    else:
        st.info("No OPEX entries yet. Add entries above to start tracking.")

# ─────────────────────────────────────────────────────────────────────────────
# TAB: ALERTS  (alert feed + acknowledge / resolve)
# ─────────────────────────────────────────────────────────────────────────────
with t_alerts:
    st.header("Alert Feed")
    st.caption("Rule-based and ML-generated alerts. Acknowledge or resolve below.")

    conn=_get_db()
    all_alerts=conn.execute(
        "SELECT id,ts,severity,category,message,status FROM alerts "
        "ORDER BY ts DESC").fetchall()
    conn.close()

    if not all_alerts:
        st.success("✅ No alerts — all clear.")
    else:
        df_al=pd.DataFrame(all_alerts,
            columns=["id","Timestamp","Severity","Category","Message","Status"])

        # Metrics
        n_open =(df_al["Status"]=="Open").sum()
        n_crit =(df_al["Severity"]=="Critical").sum()
        n_warn =(df_al["Severity"]=="Warning").sum()
        c1,c2,c3=st.columns(3)
        c1.metric("Open Alerts",str(n_open))
        c2.metric("Critical",str(n_crit))
        c3.metric("Warnings",str(n_warn))

        # Filter
        filt=st.selectbox("Filter by status",["All","Open","Acknowledged","Resolved"],
                          key="al_filt")
        df_show=df_al if filt=="All" else df_al[df_al["Status"]==filt]

        # Colour severity
        def _sev_icon(s):
            return {"Critical":"🔴","Warning":"🟡","Info":"🔵"}.get(s,"⚪")
        df_show=df_show.copy()
        df_show["Sev"]=df_show["Severity"].apply(_sev_icon)+" "+df_show["Severity"]

        st.dataframe(df_show[["Timestamp","Sev","Category","Message","Status"]]
                     .rename(columns={"Sev":"Severity"}),
                     use_container_width=True,hide_index=True)

        # Acknowledge / Resolve
        st.divider()
        c1,c2=st.columns(2)
        alert_ids=[str(r[0]) for r in all_alerts if r[5]=="Open"]
        if alert_ids:
            sel_id=c1.selectbox("Select alert ID",alert_ids,key="al_sel")
            new_st=c2.selectbox("New status",["Acknowledged","Resolved"],key="al_st")
            if st.button("Update Alert",key="al_upd"):
                conn=_get_db()
                conn.execute("UPDATE alerts SET status=? WHERE id=?",
                             (new_st,int(sel_id)))
                conn.commit(); conn.close()
                st.success(f"Alert #{sel_id} → {new_st}")
                st.rerun()

        # Clear all resolved
        if st.button("🗑️ Clear all Resolved alerts",key="al_clr"):
            conn=_get_db()
            conn.execute("DELETE FROM alerts WHERE status='Resolved'")
            conn.commit(); conn.close()
            st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# TAB: FAILURE ANALYTICS  (historical telemetry trends → predictive signals)
# ─────────────────────────────────────────────────────────────────────────────
with t_failure:
    st.header("🩺 Predictive Failure Analytics")
    st.caption(
        "Historical trends across inverter temperature, power factor, efficiency, "
        "grid frequency and AC power — the early-warning signals for inverter and "
        "component failures. Data is built up each time you run **Live Diagnostics**."
    )

    conn = _get_db()
    telem_rows = conn.execute(
        "SELECT ts, inverter_id, signal, value FROM telemetry_history ORDER BY ts"
    ).fetchall()
    conn.close()

    if not telem_rows:
        st.info(
            "No telemetry history yet. Go to **🛠️ Health → Live Diagnostics** and click "
            "**Run Live Diagnostics** — each run saves a snapshot. After a few runs "
            "(ideally over days/weeks) the trends here will be populated."
        )
        st.divider()
        st.subheader("What to look for — failure predictor guide")
        guide = [
            ("🌡️ Inverter Temperature rising", "Gradual °C rise above 60°C over weeks = fan failure, vent blockage, or internal component degradation. Sudden spike = cooling system fault. Lead time before failure: days to weeks."),
            ("⚡ Power Factor declining", "PF drop below 0.92 indicates grid interaction problems, capacitor bank degradation, or reactive power setpoint drift. Often precedes inverter trip events."),
            ("📉 Efficiency trend downward", "Efficiency dropping > 1% over 3 months (weather-corrected) suggests IGBT degradation, DC-side resistance increase (corroded connectors), or transformer issues."),
            ("🔌 Grid Frequency deviation", "Sustained deviation from 50 Hz (even 0.1 Hz) indicates grid instability that stresses inverter internals. Repeated excursions correlate with premature capacitor failure."),
            ("🔄 AC Power clipping", "Power plateau at rated capacity during low-irradiance periods = measurement anomaly or inverter de-rating. During high irradiance it's normal clipping."),
            ("📊 Multi-signal correlation", "The most reliable failure signal: temperature rising AND efficiency falling AND power factor dipping simultaneously = systemic degradation, not noise."),
        ]
        for title, detail in guide:
            with st.expander(title):
                st.write(detail)

    else:
        df_tel = pd.DataFrame(telem_rows, columns=["ts","inverter_id","signal","value"])
        df_tel["ts"] = pd.to_datetime(df_tel["ts"])
        df_tel["value"] = pd.to_numeric(df_tel["value"], errors="coerce")

        all_signals = sorted(df_tel["signal"].unique())
        all_invs    = sorted(df_tel["inverter_id"].unique())

        fa1, fa2 = st.columns([3, 1])
        with fa2:
            sel_inv = st.multiselect("Inverter(s)", all_invs,
                                     default=all_invs[:3] if len(all_invs)>=3 else all_invs,
                                     key="fa_inv")
        with fa1:
            date_range = st.date_input("Date range",
                value=(df_tel["ts"].dt.date.min(), df_tel["ts"].dt.date.max()),
                key="fa_dates")

        if not sel_inv:
            st.warning("Select at least one inverter.")
            st.stop()

        df_f = df_tel[df_tel["inverter_id"].isin(sel_inv)].copy()
        if len(date_range) == 2:
            df_f = df_f[(df_f["ts"].dt.date >= date_range[0]) &
                        (df_f["ts"].dt.date <= date_range[1])]

        if df_f.empty:
            st.warning("No data in selected range."); st.stop()

        # ── Summary health score per inverter ──
        st.subheader("① Inverter Health Score")
        st.caption("Composite score 0–100 based on how far each signal is from its warning threshold. "
                   "Below 70 = amber; below 50 = red.")

        def _health_score(df_inv: pd.DataFrame) -> float:
            scores = []
            for sig_col, meta in FAILURE_SIGNALS.items():
                sig_label = meta["label"]
                sub = df_inv[df_inv["signal"] == sig_label]
                if sub.empty or meta["warn"] is None: continue
                recent_val = sub.sort_values("ts")["value"].iloc[-1]
                low_bad = meta.get("low_bad", False)
                warn, crit = meta["warn"], meta["crit"]
                if low_bad:
                    if recent_val >= warn:   s = 100.0
                    elif recent_val >= crit: s = 50.0
                    else:                    s = 10.0
                else:
                    if recent_val <= warn:   s = 100.0
                    elif recent_val <= crit: s = 50.0
                    else:                   s = 10.0
                scores.append(s)
            return float(np.mean(scores)) if scores else 100.0

        score_cols = st.columns(min(len(sel_inv), 5))
        for i, inv in enumerate(sel_inv[:5]):
            df_i = df_f[df_f["inverter_id"] == inv]
            sc = _health_score(df_i)
            col = score_cols[i % len(score_cols)]
            delta_c = "normal" if sc >= 70 else "inverse"
            col.metric(f"INV {inv[-6:]}", f"{sc:.0f}/100",
                       delta="🟢 Good" if sc>=70 else ("🟡 Watch" if sc>=50 else "🔴 Critical"),
                       delta_color=delta_c)

        st.divider()

        # ── Per-signal historical trend charts ──
        st.subheader("② Signal Trend Charts")
        st.caption("Each panel shows raw readings + 7-day rolling average + warning/critical bands.")

        n_sig = len([s for s in FAILURE_SIGNALS.values() if
                     df_f["signal"].eq(s["label"]).any()])
        if n_sig == 0:
            st.info("No matching telemetry signals found in history.")
        else:
            for sig_col, meta in FAILURE_SIGNALS.items():
                sig_label = meta["label"]
                sub = df_f[df_f["signal"] == sig_label].copy()
                if sub.empty: continue

                st.markdown(f"**{sig_label}**")
                fig = go.Figure()

                # One trace per inverter
                for j, inv in enumerate(sel_inv):
                    dv = sub[sub["inverter_id"] == inv].sort_values("ts")
                    if dv.empty: continue
                    # Rolling mean (7-day window over irregular samples: use time-based)
                    dv = dv.set_index("ts")
                    dv["roll"] = dv["value"].rolling("7D", min_periods=1).mean()
                    dv = dv.reset_index()

                    fig.add_scatter(x=dv["ts"], y=dv["value"],
                        mode="markers", name=f"{inv} (raw)",
                        marker=dict(color=PALETTE[j % len(PALETTE)], size=5, opacity=0.5))
                    fig.add_scatter(x=dv["ts"], y=dv["roll"],
                        mode="lines", name=f"{inv} 7-day avg",
                        line=dict(color=PALETTE[j % len(PALETTE)], width=2))

                # Warning / critical bands
                warn_v = meta.get("warn")
                crit_v = meta.get("crit")
                low_bad = meta.get("low_bad", False)
                x_range = [df_f["ts"].min(), df_f["ts"].max()]
                if warn_v is not None:
                    fig.add_scatter(x=x_range, y=[warn_v, warn_v],
                        mode="lines", name="⚠️ Warning",
                        line=dict(color="#fb923c", dash="dash", width=1.5))
                if crit_v is not None:
                    fig.add_scatter(x=x_range, y=[crit_v, crit_v],
                        mode="lines", name="🔴 Critical",
                        line=dict(color="#ff5f5f", dash="dot", width=1.5))

                # Shaded danger zone
                if warn_v is not None and crit_v is not None:
                    if low_bad:
                        fig.add_hrect(y0=0, y1=crit_v,
                            fillcolor="rgba(255,95,95,0.06)", line_width=0)
                        fig.add_hrect(y0=crit_v, y1=warn_v,
                            fillcolor="rgba(251,146,60,0.04)", line_width=0)
                    else:
                        fig.add_hrect(y0=crit_v, y1=1e9,
                            fillcolor="rgba(255,95,95,0.06)", line_width=0)
                        fig.add_hrect(y0=warn_v, y1=crit_v,
                            fillcolor="rgba(251,146,60,0.04)", line_width=0)

                _base_layout(fig, "", "Date", f"{sig_label} ({meta['unit']})", height=260)
                fig.update_layout(margin=dict(t=10, b=30, l=10, r=10),
                                  legend=dict(orientation="h", y=1.02))
                st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # ── Multi-signal correlation heatmap ──
        st.subheader("③ Cross-Signal Correlation Heatmap")
        st.caption("Correlation between telemetry signals across all inverters and time. "
                   "Strong negative correlation between temperature and efficiency is a key failure precursor.")

        pivot = df_f.pivot_table(index="ts", columns="signal", values="value",
                                 aggfunc="mean").resample("1D").mean().dropna(how="all")
        if len(pivot.columns) >= 2 and len(pivot) >= 3:
            corr = pivot.corr()
            fig_corr = go.Figure(go.Heatmap(
                z=corr.values,
                x=corr.columns.tolist(),
                y=corr.index.tolist(),
                colorscale="RdBu",
                zmid=0,
                zmin=-1, zmax=1,
                text=np.round(corr.values, 2).astype(str),
                texttemplate="%{text}",
                hovertemplate="%{y} vs %{x}: %{z:.3f}<extra></extra>",
            ))
            _base_layout(fig_corr, "Signal Correlation Matrix", height=380)
            fig_corr.update_layout(margin=dict(t=30, b=80, l=120, r=10))
            st.plotly_chart(fig_corr, use_container_width=True)

            # Highlight concerning correlations
            concerning = []
            for i in range(len(corr.columns)):
                for j in range(i+1, len(corr.columns)):
                    c = corr.iloc[i, j]
                    pair = f"{corr.columns[i]} ↔ {corr.index[j]}"
                    if c < -0.5:
                        concerning.append((pair, c, "🔴 Strong negative — investigate"))
                    elif c > 0.85 and i != j:
                        concerning.append((pair, c, "🟡 Redundant sensors or collinearity"))
            if concerning:
                st.markdown("**Notable correlations:**")
                for pair, val, note in concerning:
                    st.markdown(f"- `{pair}`: **{val:.2f}** — {note}")
        else:
            st.info("Need ≥2 signals and ≥3 days of data for correlation analysis.")

        st.divider()

        # ── Exceedance count over time ──
        st.subheader("④ Threshold Exceedance Log")
        st.caption("Count of readings that breached warning or critical thresholds per day.")

        exceedances = []
        for sig_col, meta in FAILURE_SIGNALS.items():
            sig_label = meta["label"]
            warn_v = meta.get("warn")
            crit_v = meta.get("crit")
            low_bad = meta.get("low_bad", False)
            sub = df_f[df_f["signal"] == sig_label].copy()
            if sub.empty or warn_v is None: continue
            sub["Date"] = sub["ts"].dt.date
            for _, row in sub.iterrows():
                v = row["value"]
                if low_bad:
                    level = ("Critical" if v < crit_v else
                             "Warning"  if v < warn_v else None)
                else:
                    level = ("Critical" if v > crit_v else
                             "Warning"  if v > warn_v else None)
                if level:
                    exceedances.append({
                        "Date": row["ts"],
                        "Inverter": row["inverter_id"],
                        "Signal": sig_label,
                        "Value": round(v, 3),
                        "Level": level,
                        "Threshold": warn_v if level=="Warning" else crit_v,
                    })

        if exceedances:
            df_exc = pd.DataFrame(exceedances).sort_values("Date", ascending=False)

            n_crit = (df_exc["Level"] == "Critical").sum()
            n_warn = (df_exc["Level"] == "Warning").sum()
            ec1, ec2 = st.columns(2)
            ec1.metric("Critical exceedances", str(n_crit))
            ec2.metric("Warning exceedances",  str(n_warn))

            # Time series of exceedance count
            df_exc["DateOnly"] = df_exc["Date"].dt.date
            daily_exc = df_exc.groupby(["DateOnly","Level"]).size().reset_index(name="Count")
            fig_exc = go.Figure()
            for level, col in [("Critical","#ff5f5f"),("Warning","#fb923c")]:
                sub = daily_exc[daily_exc["Level"]==level]
                if not sub.empty:
                    fig_exc.add_bar(x=sub["DateOnly"], y=sub["Count"],
                                    name=level, marker_color=col)
            _base_layout(fig_exc, "Daily Threshold Exceedances",
                         "Date", "Count", barmode="stack", height=240)
            st.plotly_chart(fig_exc, use_container_width=True)

            st.dataframe(
                df_exc[["Date","Inverter","Signal","Value","Threshold","Level"]]
                .style.apply(lambda row: [
                    "color:#ff5f5f" if row["Level"]=="Critical"
                    else "color:#fb923c" if row["Level"]=="Warning"
                    else "" for _ in row], axis=1),
                use_container_width=True, hide_index=True)

            # Auto-alert for critical exceedances
            if n_crit > 0:
                sigs = df_exc[df_exc["Level"]=="Critical"]["Signal"].unique()
                add_alert("Critical","FailureSignal",
                    f"{n_crit} critical threshold exceedance(s) detected in: "
                    f"{', '.join(sigs)}")
        else:
            st.success("✅ No threshold exceedances in selected range.")

        st.divider()

        # ── Failure probability estimate ──
        st.subheader("⑤ Failure Risk Estimate")
        st.caption("Simple heuristic model based on signal trend velocity. "
                   "Not a certified maintenance tool — use as a discussion aid.")

        risk_items = []
        for sig_col, meta in FAILURE_SIGNALS.items():
            sig_label = meta["label"]
            warn_v = meta.get("warn")
            if warn_v is None: continue
            sub = df_f[df_f["signal"] == sig_label].sort_values("ts")
            if len(sub) < 4: continue
            # Fit linear trend to last 30 readings
            recent = sub.tail(30)
            x_num = (recent["ts"] - recent["ts"].min()).dt.total_seconds().values
            y_val = recent["value"].values
            if np.std(x_num) == 0: continue
            try:
                from scipy.stats import linregress
                sl, ic, r2, _, _ = linregress(x_num, y_val)
            except Exception:
                continue
            # Days until threshold breach at current velocity
            low_bad = meta.get("low_bad", False)
            current = y_val[-1]
            crit_v = meta.get("crit", warn_v)
            if sl == 0: continue
            secs_to_breach = (crit_v - current) / sl if not low_bad else (current - crit_v) / (-sl)
            days_to_breach = secs_to_breach / 86400
            risk_items.append({
                "Signal": sig_label,
                "Current": round(current, 2),
                "Trend": f"{'↑' if sl>0 else '↓'} {abs(sl*86400):.3f}/day",
                "Days to Critical": round(days_to_breach, 0) if 0 < days_to_breach < 365 else None,
                "R²": round(r2 if hasattr(r2,'__float__') else r2**2, 3),
            })

        if risk_items:
            df_risk = pd.DataFrame(risk_items)
            df_risk["Risk"] = df_risk["Days to Critical"].apply(
                lambda d: "🔴 HIGH (<30d)" if d is not None and d < 30
                else ("🟡 MEDIUM (30–90d)" if d is not None and d < 90
                      else "🟢 Low / stable"))
            st.dataframe(
                df_risk.style.apply(lambda row: [
                    "color:#ff5f5f" if "HIGH" in str(row["Risk"])
                    else "color:#fb923c" if "MEDIUM" in str(row["Risk"])
                    else "color:#4ade80" for _ in row], axis=1),
                use_container_width=True, hide_index=True)

            high_risk = df_risk[df_risk["Risk"].str.contains("HIGH")]
            if not high_risk.empty:
                add_alert("Warning","FailureRisk",
                    f"Trend model predicts critical breach in <30 days for: "
                    f"{', '.join(high_risk['Signal'].tolist())}")
        else:
            st.info("Need ≥4 readings per signal to compute trend velocity.")

        st.divider()

        # ── Raw telemetry table + export ──
        with st.expander("📋 Raw Telemetry History"):
            st.dataframe(df_f[["ts","inverter_id","signal","value"]].rename(columns={
                "ts":"Timestamp","inverter_id":"Inverter",
                "signal":"Signal","value":"Value"}),
                use_container_width=True, hide_index=True)
            csv = df_f[["ts","inverter_id","signal","value"]].to_csv(index=False)
            st.download_button("⬇️ Export CSV", csv,
                file_name="telemetry_history.csv", mime="text/csv")

        if st.button("🗑️ Clear telemetry history", key="clr_tel"):
            conn = _get_db()
            conn.execute("DELETE FROM telemetry_history")
            conn.commit(); conn.close()
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB: IPTO DIAGNOSTICS  (live connectivity probe + troubleshooting guide)
# ─────────────────────────────────────────────────────────────────────────────
with t_ipto:
    st.header("🔌 IPTO / ADMIE Market Data Diagnostics")
    st.caption(
        "The DAM price data powering financial tabs comes from the Greek TSO (ADMIE/IPTO) "
        "public API at `admie.gr`. This panel diagnoses why prices may not be loading "
        "and shows what's cached locally."
    )

    # Show last known error from session state
    last_err = st.session_state.get("_admie_last_error")
    if last_err:
        st.error(f"**Last ADMIE error recorded this session:**\n\n`{last_err}`")
    else:
        st.success("No ADMIE errors recorded this session.")

    st.divider()

    # ── Live probe ──
    st.subheader("Live Connectivity Probe")
    st.write(
        "Runs a full end-to-end test: DNS → HTTPS → API response → file download → "
        "price parsing. Takes 5–15 seconds."
    )

    probe_col1, probe_col2 = st.columns([1, 2])
    probe_timeout = probe_col1.slider("Timeout (s)", 5, 30, 10, key="probe_timeout")

    if probe_col2.button("▶️ Run ADMIE Connectivity Probe", key="btn_probe"):
        with st.spinner("Probing admie.gr …"):
            result = probe_admie(timeout=probe_timeout)

        # Display result
        if result.get("user_message"):
            if result.get("price_parseable"):
                st.success(result["user_message"])
            elif result.get("reachable"):
                st.warning(result["user_message"])
            else:
                st.error(result["user_message"])

        # Detail table
        detail_rows = [
            ("DNS / Network reachable", "✅ Yes" if result["reachable"] else "❌ No"),
            ("HTTP status code",        str(result.get("status_code") or "N/A")),
            ("Error type",              result.get("error_type") or "None"),
            ("File URL found",          "✅ Yes" if result["file_url_found"] else "❌ No"),
            ("Price column parseable",  "✅ Yes" if result["price_parseable"] else "❌ No"),
            ("Sample avg price",        f"{result['sample_price']:.2f} €/MWh"
                                        if result["sample_price"] else "N/A"),
            ("API JSON keys",           str(result.get("json_keys") or "N/A")),
            ("Files found for test date", str(result.get("n_files", 0))),
        ]
        df_det = pd.DataFrame(detail_rows, columns=["Check","Result"])
        st.dataframe(df_det, use_container_width=True, hide_index=True)

    st.divider()

    # ── Troubleshooting decision tree ──
    st.subheader("Troubleshooting Guide")

    with st.expander("❌ DNS failure / NameResolutionError", expanded=False):
        st.markdown("""
**Symptom:** `Failed to resolve 'www.admie.gr'` or `NameResolutionError`

**Cause:** Your server cannot reach external DNS for `admie.gr`.

**Fixes (in order of likelihood):**
1. **Streamlit Cloud / PaaS** — these platforms allow all outbound HTTPS by default. Re-deploy and test.
2. **Self-hosted with corporate firewall** — ask your network team to whitelist `www.admie.gr:443` in the egress firewall and DNS resolver.
3. **Docker container** — ensure the container has DNS configured (`--dns 8.8.8.8`) and outbound port 443 is not blocked by the host firewall.
4. **VPN** — if your server is behind a split-tunnel VPN, `admie.gr` (a Greek TSO) may be routed through the tunnel and blocked. Add a split-tunnel exception.
5. **Development machine** — test from terminal: `curl -I https://www.admie.gr/` — if that works, the issue is environment-specific.
""")

    with st.expander("🚫 Egress proxy blocked (x-deny-reason header)", expanded=False):
        st.markdown("""
**Symptom:** API responds but includes `x-deny-reason` header

**Cause:** A corporate or PaaS egress proxy is intercepting and blocking the request based on domain policy.

**Fixes:**
1. Add `admie.gr` and `www.admie.gr` to your proxy allowlist.
2. If using Streamlit Community Cloud, egress is unrestricted — this error won't occur there.
3. Check if the proxy also blocks the file CDN that ADMIE uses to serve Excel files (often a different domain).
""")

    with st.expander("⏱️ Timeout", expanded=False):
        st.markdown("""
**Symptom:** `ReadTimeout` or `ConnectTimeout`

**Cause:** ADMIE server is slow to respond (common during business hours) or network latency is high.

**Fixes:**
1. Increase the timeout slider above and re-run the probe.
2. The `dam_daily()` and `dam_monthly_avg()` functions use 15s and 20s timeouts — acceptable for most deployments.
3. ADMIE sometimes publishes files late (DAM results for D+1 appear around 13:00 EET). Avoid fetching before 14:00 EET.
4. Consider adding a retry queue that polls every 30 minutes rather than on-demand.
""")

    with st.expander("⚠️ API returns data but no price column found", expanded=False):
        st.markdown("""
**Symptom:** Probe succeeds, file downloads, but price parsing fails

**Cause:** ADMIE occasionally changes the Excel column names or sheet structure.

**Fixes:**
1. Run the probe and note the actual column names shown in the diagnostic table.
2. Add the new column name keyword to the `["mcp","smp","price","τιμή"]` list in `dam_daily()`.
3. ADMIE publishes both English and Greek column headers depending on the file version — both are handled.
4. Try `ISP1ISPResults` instead of `DAM_ResultsSummary` — it uses a slightly different schema.
""")

    with st.expander("📅 No files for a specific date", expanded=False):
        st.markdown("""
**Symptom:** API returns empty list `[]` for the requested date

**Causes:**
- The date is in the future (DAM results are published D-1 by ~13:00 EET)
- The date is a weekend or holiday — ADMIE still publishes but file names may differ
- The date predates the DAM_ResultsSummary file format (pre-2018 use different endpoints)

**Fixes:**
1. Always use `date.today() - timedelta(days=1)` as the default date
2. For historical data, try the `ISP1ISPResults` fallback
3. Check https://www.admie.gr/en/market/market-statistics manually for that date
""")

    st.divider()

    # ── ISP Raw Sheet Inspector ───────────────────────────────────────────────
    st.subheader("ISP Raw Sheet Inspector")
    st.caption(
        "Downloads the latest ISP1ISPResults file and shows every row label "
        "(col 0) in each sheet — useful if the SMP row matcher fails because "
        "ADMIE renamed a row. Matched rows are marked ✅."
    )
    inspect_date = st.date_input(
        "Date to inspect", value=date.today() - timedelta(days=3),
        key="isp_inspect_date")

    if st.button("Inspect ISP File", key="btn_inspect"):
        ds_i = inspect_date.strftime("%Y-%m-%d")
        with st.spinner(f"Downloading ISP1ISPResults for {ds_i}…"):
            content = _fetch_admie_file("ISP1ISPResults", ds_i, ds_i, timeout=25)
        if not content:
            st.error(f"No ISP file found for {ds_i}. "
                     "Check network connectivity or try a different date.")
        else:
            xl = pd.read_excel(io.BytesIO(content), sheet_name=None)
            st.success(f"File downloaded — {len(xl)} sheet(s): {list(xl.keys())}")
            for sname, df_s in xl.items():
                with st.expander(
                        f"Sheet: **{sname}**  "
                        f"({df_s.shape[0]} rows × {df_s.shape[1]} cols)"):
                    labels = df_s.iloc[:, 0].astype(str).tolist()
                    st.markdown("**Row labels in col 0** — SMP matcher searches these:")
                    label_df = pd.DataFrame({
                        "Row": range(len(labels)),
                        "Label": labels,
                        "Matched": [
                            "✅" if any(kw in lbl.lower()
                                       for kw in _SMP_ROW_KEYWORDS) else ""
                            for lbl in labels
                        ]
                    })
                    st.dataframe(label_df[label_df["Label"] != "nan"],
                                 use_container_width=True, hide_index=True)

                    # Try to parse this single sheet
                    r_df = _parse_excel_isp_format(
                        {sname: df_s}, inspect_date, "ISP1ISPResults")
                    if r_df is not None and not r_df.empty:
                        prices_f = pd.to_numeric(r_df["price"], errors="coerce").dropna()
                        st.success(
                            f"✅ SMP parsed from this sheet — "
                            f"{len(prices_f)} periods, "
                            f"avg = **{prices_f.mean():.2f} €/MWh**, "
                            f"min = {prices_f.min():.2f}, "
                            f"max = {prices_f.max():.2f}")
                        st.dataframe(r_df.head(10).style.format({"price": "{:.2f}"}),
                                     use_container_width=True, hide_index=True)
                    else:
                        st.warning(
                            "⚠️ No SMP row matched in this sheet. "
                            "Find the correct label above and add it to "
                            "`_SMP_ROW_KEYWORDS` in `apm_app.py`.")

    st.divider()

    # ── Local price cache ──
    st.subheader("Local Price Cache (SQLite)")
    st.caption(
        "When ADMIE is reachable, prices are cached locally. If the API goes down, "
        "cached prices are used as fallback in financial calculations."
    )

    conn = _get_db()
    cache_rows = conn.execute(
        "SELECT ym, avg_price_eur_mwh, fetched_ts FROM price_cache ORDER BY ym DESC"
    ).fetchall()
    conn.close()

    if cache_rows:
        df_cache = pd.DataFrame(cache_rows,
                                columns=["Month","Avg Price (€/MWh)","Fetched At"])
        st.dataframe(df_cache.style.format({"Avg Price (€/MWh)":"{:.2f}"}),
                     use_container_width=True, hide_index=True)
        st.metric("Cached months", len(cache_rows))

        if st.button("🗑️ Clear price cache", key="clr_cache"):
            conn = _get_db()
            conn.execute("DELETE FROM price_cache")
            conn.commit(); conn.close()
            st.rerun()
    else:
        st.info(
            "No prices cached yet. Once ADMIE is reachable from your deployment server, "
            "prices will be automatically cached here as you use the Monthly and Financial tabs."
        )

    st.divider()

    # ── API reference ──
    with st.expander("📖 ADMIE API Reference"):
        st.markdown("""
**Base endpoint:**
```
GET https://www.admie.gr/getOperationMarketFile
```

**Parameters:**

| Parameter | Values | Description |
|-----------|--------|-------------|
| `dateStart` | `YYYY-MM-DD` | Start date |
| `dateEnd` | `YYYY-MM-DD` | End date |
| `FileCategory` | `DAM_ResultsSummary` | Day-Ahead Market clearing prices (primary) |
| | `ISP1ISPResults` | Intra-day System Marginal Price (fallback) |

**Response:** JSON array of file metadata objects. Each contains a URL to an Excel file.

**Excel file structure:** One sheet with columns including period/time and MCP (Market Clearing Price) in €/MWh.

**Notes:**
- DAM results are published by ~13:00 EET for the following day (D+1)
- 96 rows = 15-min resolution; 24 rows = hourly
- Greek column headers: `Τιμή` = Price, `Περίοδος` = Period
- SSL certificate may require `verify=False` on some server configurations
- No authentication required — fully public API
""")