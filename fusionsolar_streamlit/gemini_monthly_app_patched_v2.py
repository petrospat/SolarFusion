
# gemini_monthly_app_rate_limited.py
# Streamlit app to fetch monthly KPIs from Huawei FusionSolar Northbound API
# Rate-limit patch applied:
# - Cooldown on Refresh button to avoid spam (default 60s)
# - Backoff & retry wrapper for API POSTs when 407 (ACCESS_FREQUENCY_IS_TOO_HIGH)
# - Caching monthly call for 30 minutes to reduce hits
# - Budget indicator (based on device/plant counts); site has 3 inverters --> 1 call/5min
# - Hardened normalization (from v2): safely handles unexpected JSON shapes

import json
import time
import urllib3
from datetime import datetime, timezone
from typing import Tuple, List, Optional, Union

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

# ----------------------- Page setup -----------------------
st.set_page_config(page_title="Solar Tracker (Northbound API)", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------- Site-specific counts -----------------------
# User informed: site has 3 inverters. Budget per 5 min = ceil(3/100) = 1 call.
DEVICE_COUNTS = {"inverter": 3}
PLANT_COUNT = 1  # assuming single plant queried

# ----------------------- Cooldown settings -----------------------
COOLDOWN_SECONDS = 60

# ----------------------- Backoff helper -----------------------
def post_with_backoff(session: requests.Session, url: str, json_payload: dict,
                      timeout: int = 25, max_retries: int = 3, base_sleep: int = 30):
    """POST with exponential backoff on Huawei 407 (frequency too high).
    Returns JSON on success; raises on persistent failure.
    """
    for attempt in range(max_retries):
        resp = session.post(url, json=json_payload, timeout=timeout)
        # If HTTP OK, inspect body
        if resp.status_code == 200:
            try:
                j = resp.json()
            except Exception:
                resp.raise_for_status()
                return resp.text
            if j.get("failCode") == 407:
                sleep_for = base_sleep * (attempt + 1)
                st.warning(f"API frequency too high (407). Waiting {sleep_for}s before retry...")
                time.sleep(sleep_for)
                continue
            return j
        # Non-200: brief wait then retry
        sleep_for = base_sleep * (attempt + 1)
        time.sleep(sleep_for)
    # Give up after retries
    resp.raise_for_status()
    return resp.json()

# ----------------------- Client -----------------------
class HuaweiClient:
    def __init__(self, secrets: dict):
        self.base_url = secrets["base_url"].rstrip('/')
        self.username = secrets["username"]
        self.system_code = secrets["system_code"]
        self.verify_ssl = secrets.get("verify_ssl", True)
        self.s = requests.Session()
        self.s.verify = self.verify_ssl
        self.s.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Streamlit-App"
        })

    def login(self) -> Tuple[bool, str]:
        url = f"{self.base_url}/thirdData/login"
        payload = {"userName": self.username, "systemCode": self.system_code}
        try:
            resp = self.s.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            token = (
                resp.cookies.get("XSRF-TOKEN")
                or resp.headers.get("xsrf-token")
                or resp.headers.get("xsrftoken")
                or resp.headers.get("XSRF-TOKEN")
            )
            try:
                j = resp.json()
                if not token and isinstance(j, dict):
                    token = j.get("data")
            except Exception:
                pass
            if not token:
                return False, "Login succeeded, but no XSRF token returned"
            self.s.headers.update({"XSRF-TOKEN": token})
            return True, "Login Successful"
        except Exception as e:
            return False, f"Login error: {e}"

    def get_stations(self) -> Tuple[Optional[List[dict]], Optional[str]]:
        try:
            r = self.s.post(f"{self.base_url}/thirdData/stations", json={"pageNo": 1}, timeout=20)
            if r.status_code == 200:
                j = r.json()
                lst = j.get("data", {}).get("list")
                if lst:
                    return lst, None
        except Exception:
            pass
        try:
            r = self.s.post(f"{self.base_url}/thirdData/getStationList", json={}, timeout=20)
            r.raise_for_status()
            j = r.json()
            lst = j.get("data", {}).get("list", j.get("data", []))
            if lst:
                return lst, None
            return None, "Empty station list"
        except Exception as e:
            return None, str(e)

# ----------------------- Helpers -----------------------
def get_budget_df():
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    targets = [117077, 89742, 140573, 172775, 177950, 186287, 197265, 190014, 168524, 132649, 86079, 82732]
    return pd.DataFrame({'Month': months, 'Budget_kWh': targets})

def _coerce_rows(rows: Union[List[dict], dict, None]) -> List[dict]:
    if rows is None:
        return []
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    if isinstance(rows, dict):
        return [rows]
    return []

def normalize_safe(rows_in) -> pd.DataFrame:
    rows = _coerce_rows(rows_in)
    if not rows:
        return pd.DataFrame()
    try:
        df = pd.json_normalize(rows, sep=".")
    except NotImplementedError:
        try:
            df = pd.DataFrame(rows)
        except Exception:
            return pd.DataFrame()
    except Exception:
        try:
            df = pd.DataFrame(rows)
        except Exception:
            return pd.DataFrame()
    if df.empty:
        return df
    return df.loc[:, ~df.columns.duplicated()].copy()

def extract_monthly_rows(j: dict):
    data = j.get("data", [])
    if isinstance(data, dict):
        if 'kpiList' in data:
            return data['kpiList']
        for k in ('list', 'rows', 'items'):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and 'kpiList' in first:
            return first['kpiList']
        return data
    return []

def pick_metric(row: pd.Series) -> float:
    for k in ["dataItemMap.PVYield", "dataItemMap.inverterYield", "dataItemMap.inverter_power", "dataItemMap.ongrid_power"]:
        if k in row.index:
            v = pd.to_numeric(row[k], errors='coerce')
            if pd.notnull(v):
                return float(v)
    return 0.0

# ----------------------- Cached monthly call -----------------------
@st.cache_data(ttl=30 * 60, show_spinner=False)
def get_kpi_station_month_cached(base_url: str, station_code: str, year: int,
                                 xsrf_token: str, verify_ssl: bool) -> dict:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "User-Agent": "Streamlit-App", "XSRF-TOKEN": xsrf_token})
    dt = datetime(year, 1, 1, tzinfo=timezone.utc)
    payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
    url = f"{base_url}/thirdData/getKpiStationMonth"
    return post_with_backoff(s, url, payload, timeout=25, max_retries=3, base_sleep=30)

# ----------------------- UI -----------------------
try:
    fusion = st.secrets["fusion"]
except KeyError:
    st.error("🚨 Missing [fusion] section in .streamlit/secrets.toml")
    st.stop()

st.title(f"☀️ Solar Performance ({fusion.get('system_code', 'Unknown System')})")
col_top1, col_top2, col_top3 = st.columns(3)
show_raw = col_top1.checkbox("Show raw JSON (debug)", value=False)
year = col_top2.number_input("Year", min_value=2000, max_value=datetime.now().year+1, value=datetime.now().year, step=1)

# Budget indicator (per 5 minutes)
import math
device_budget_5min = math.ceil(DEVICE_COUNTS.get("inverter", 0) / 100)  # 3 -> 1
plant_budget_5min = math.ceil(PLANT_COUNT / 100)                           # 1 -> 1
col_top3.info(f"Rate-limit budget (per 5 min):\n"
f"• Devices: {device_budget_5min} call\n"
f"• Plants: {plant_budget_5min} call")

# Cooldown guard
last_run = st.session_state.get("last_run_ts")
can_run = True
remaining = 0
if last_run:
    elapsed = time.time() - last_run
    if elapsed < COOLDOWN_SECONDS:
        can_run = False
        remaining = int(COOLDOWN_SECONDS - elapsed)
if not can_run:
    st.warning(f"Please wait {remaining}s before refreshing again.")

if st.button("Refresh Data", help="Fetch the latest monthly KPIs for the selected year") and can_run:
    st.session_state["last_run_ts"] = time.time()
    with st.spinner("Connecting and loading monthly KPIs..."):
        client = HuaweiClient(fusion)
        ok, msg = client.login()
        if not ok:
            st.error(msg)
            st.stop()

        stations, err = client.get_stations()
        if not stations:
            st.error(f"Could not find stations: {err}")
            st.stop()

        stn = stations[0]
        stn_name = stn.get('plantName') or stn.get('stationName', 'N/A')
        capacity = stn.get('capacity', 'N/A')
        station_code = stn.get('plantCode') or stn.get('stationCode')
        if not station_code:
            st.error("Station ID missing (plantCode/stationCode)")
            st.stop()
        st.success(f"Connected to **{stn_name}** (Capacity: {capacity} kW)")

        # --- Monthly: cached + backoff ---
        xsrf_token = client.s.headers.get("XSRF-TOKEN", "")
        j_mon = get_kpi_station_month_cached(client.base_url, station_code, int(year), xsrf_token, client.verify_ssl)

        # Diagnostics if empty
        if not j_mon.get("data"):
            st.warning("Monthly API returned empty data[] or 407 rate-limit.")
            st.write("failCode:", j_mon.get("failCode"))
            st.write("message:", j_mon.get("message"))
            st.write("params:", j_mon.get("params"))
            st.write("success:", j_mon.get("success"))
            if show_raw:
                with st.expander("Raw monthly JSON"):
                    st.json(j_mon)
            st.stop()

        rows = extract_monthly_rows(j_mon)
        if show_raw:
            with st.expander("Rows before normalization (type & length)"):
                st.write(type(rows).__name__, len(rows) if hasattr(rows, '__len__') else 'n/a')
                st.json(rows[:2] if isinstance(rows, list) else rows)

        tmp = normalize_safe(rows)
        if tmp.empty:
            st.warning("Monthly rows could not be normalized (empty). See raw JSON above.")
            if show_raw:
                with st.expander("Raw monthly JSON"):
                    st.json(j_mon)
            st.stop()

        # Map to months by collectTime
        tcol = "collectTime" if "collectTime" in tmp.columns else None
        if not tcol:
            cand = [c for c in tmp.columns if c.endswith(".collectTime") or "time" in c.lower()]
            tcol = cand[0] if cand else None
        if not tcol:
            st.error("No collectTime column in monthly response")
            if show_raw:
                with st.expander("Normalized monthly DataFrame"):
                    st.dataframe(tmp)
            st.stop()

        tmp['time'] = pd.to_datetime(tmp[tcol], unit='ms', utc=True).dt.tz_convert(None)
        tmp['month'] = tmp['time'].dt.month

        # Build month -> metric mapping
        df = get_budget_df()
        df['Actual_kWh'] = 0.0
        for m in range(0, 12):
            rm = tmp.loc[tmp['month'] == m]
            if rm.empty:
                continue
            val = pick_metric(rm.iloc[0])
            df.at[m-1, 'Actual_kWh'] = val

        # KPIs
        current_month_idx = min(11, max(0, datetime.now().month))
        total_actual = float(df['Actual_kWh'].sum())
        total_budget_ytd = float(df.iloc[:current_month_idx]['Budget_kWh'].sum())
        ytd_diff = total_actual - total_budget_ytd
        c1, c2 = st.columns(2)
        c1.metric("Year Total (Actual)", f"{total_actual:.1f} kWh")
        c2.metric("Variance (YTD)", f"{ytd_diff:.1f} kWh", delta=f"{ytd_diff:.1f} kWh", delta_color="normal")

        # Chart
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df['Month'], y=df['Budget_kWh'], name='Budget', marker_color='lightgrey'))
        fig.add_trace(go.Scatter(x=df['Month'], y=df['Actual_kWh'], name='Actual', mode='lines+markers', line=dict(color='orange', width=4)))
        fig.update_layout(title=f"Monthly Performance vs Budget ({int(year)})", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)

        with st.expander("Raw Data Table"):
            st.dataframe(df, use_container_width=True)

# Initial hint for users
st.info("Set the year and click 'Refresh Data'. Cooldown and caching are enabled to respect rate limits.")
