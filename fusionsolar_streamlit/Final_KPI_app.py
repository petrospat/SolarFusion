# Final_KPI_app.py
# Streamlit app for Huawei FusionSolar Northbound API
# Features: 2025 Zero Budget Policy, +1 Month Shift, Technical KPIs, and Health Monitoring

import json
import time
import random
import math
import urllib3
import calendar
from datetime import datetime, timezone
from typing import Tuple, List, Optional, Union

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st
import plotly.graph_objects as go

# ----------------------- Page setup -----------------------
st.set_page_config(page_title="FusionSolar Performance Dashboard", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------- Config: timezone & shift -----------------------
PLANT_TZ = "Europe/Athens" 
SHIFT_MONTHS = 1            

# ----------------------- Site-specific counts -----------------------
DEVICE_COUNTS_HINT = {"inverter": 3}
PLANT_COUNT_HINT = 1
COOLDOWN_SECONDS = 60

# ----------------------- Backoff helper -----------------------
def post_with_backoff(session: requests.Session, url: str, json_payload: dict,
                      timeout: int = 25, max_retries: int = 3, base_sleep: float = 2.0,
                      max_sleep: float = 30.0, jitter: float = 1.0):
    last_resp = None
    for attempt in range(max_retries):
        resp = session.post(url, json=json_payload, timeout=timeout)
        last_resp = resp
        if resp.status_code == 200:
            try:
                j = resp.json()
            except Exception:
                resp.raise_for_status()
                return resp.text
            if j.get("failCode") == 407:
                sleep_for = min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter)
                time.sleep(sleep_for)
                continue
            return j
        sleep_for = min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter)
        time.sleep(sleep_for)

    if last_resp is not None:
        try:
            last_resp.raise_for_status()
        except Exception:
            try: return last_resp.json()
            except Exception: raise
    raise RuntimeError("POST failed: no response")

# ----------------------- Client -----------------------
class HuaweiClient:
    def __init__(self, secrets: dict):
        self.base_url = secrets["base_url"].rstrip('/')
        self.username = secrets["username"]
        self.system_code = secrets["system_code"]
        self.verify_ssl = secrets.get("verify_ssl", True)
        self.s = requests.Session()
        self.s.verify = self.verify_ssl
        # Mount an HTTPAdapter with retries to handle transient network issues
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
            backoff_factor=0.5,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "Streamlit-App"})

    def login(self) -> Tuple[bool, str]:
        payload = {"userName": self.username, "systemCode": self.system_code}
        try:
            resp = self.s.post(f"{self.base_url}/thirdData/login", json=payload, timeout=15)
            resp.raise_for_status()
            token = (resp.cookies.get("XSRF-TOKEN") or resp.headers.get("xsrf-token") or
                     resp.headers.get("xsrftoken") or resp.headers.get("XSRF-TOKEN"))
            try:
                j = resp.json()
                if not token and isinstance(j, dict): token = j.get("data")
            except Exception: pass
            if not token: return False, "No XSRF token returned"
            self.s.headers.update({"XSRF-TOKEN": token})
            return True, "Login Successful"
        except Exception as e: return False, f"Login error: {e}"

    def get_stations(self) -> Tuple[Optional[List[dict]], Optional[str]]:
        try:
            r = self.s.post(f"{self.base_url}/thirdData/getStationList", json={}, timeout=20)
            r.raise_for_status()
            j = r.json()
            
            # SAFE EXTRACTION: Handle cases where 'data' is a list OR a dict containing a 'list' key
            rows = []
            if isinstance(j, dict):
                data = j.get("data")
                if isinstance(data, dict):
                    rows = data.get("list", [])
                elif isinstance(data, list):
                    rows = data
            
            if rows: return rows, None
            return None, "Empty station list"
        except Exception as e: return None, str(e)

def get_logged_in_client(secrets: dict, ttl_seconds: int = 600) -> Tuple[Optional[HuaweiClient], str]:
    now_ts = time.time()
    cached = st.session_state.get("huawei_client")
    ts = st.session_state.get("huawei_client_ts")
    if cached and ts and (now_ts - ts) < ttl_seconds:
        return cached, "Reused cached HuaweiClient"
    client = HuaweiClient(secrets)
    ok, msg = client.login()
    if not ok: return None, msg
    st.session_state["huawei_client"] = client
    st.session_state["huawei_client_ts"] = now_ts
    return client, msg

# ----------------------- Data Helpers -----------------------

def get_budget_df(year: int) -> pd.DataFrame:
    if year < 2025: year = 2025
    months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    targets = [117077, 89742, 140573, 172775, 177950, 186287, 197265, 190014, 168524, 132649, 86079, 82732]
    if year == 2025:
        for i in range(0, 9): targets[i] = 0
    df = pd.DataFrame({'Month': months, 'Budget_kWh': targets})
    df['Year'] = year
    return df

def normalize_safe(rows_in) -> pd.DataFrame:
    if not rows_in: return pd.DataFrame()
    if isinstance(rows_in, dict): rows_in = [rows_in]
    try:
        df = pd.json_normalize(rows_in, sep='.')
    except Exception:
        df = pd.DataFrame(rows_in)
    if df.empty: return df
    return df.loc[:, ~df.columns.duplicated()].copy()

@st.cache_data(ttl=30 * 60, show_spinner=False)
def get_kpi_station_month_cached(base_url, station_code, year, xsrf_token, verify_ssl) -> dict:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    dt = datetime(year, 1, 1, tzinfo=timezone.utc)
    payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
    return post_with_backoff(s, f"{base_url}/thirdData/getKpiStationMonth", payload)


@st.cache_data(ttl=30 * 60, show_spinner=False)
def get_kpi_station_hour_cached(base_url, station_code, year, xsrf_token, verify_ssl) -> dict:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    dt = datetime(year, 1, 1, tzinfo=timezone.utc)
    payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
    return post_with_backoff(s, f"{base_url}/thirdData/getKpiStationHour", payload)

@st.cache_data(ttl=25 * 60, show_spinner=False)
def get_device_list_cached(base_url, station_code, xsrf_token, verify_ssl) -> List[dict]:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    j = post_with_backoff(s, f"{base_url}/thirdData/getDevList", {"stationCodes": station_code})
    rows = []
    if isinstance(j, dict):
        data = j.get("data")
        rows = data.get("list", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    return rows if isinstance(rows, list) else []

@st.cache_data(ttl=5 * 60, show_spinner=False)
def get_dev_real_kpi_cached(base_url, dev_type_id, dev_ids_tuple, xsrf_token, verify_ssl) -> pd.DataFrame:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    ids = list(map(str, dev_ids_tuple))
    payload = {"devTypeId": dev_type_id, "devIds": ",".join(ids)}
    j = post_with_backoff(s, f"{base_url}/thirdData/getDevRealKpi", payload)
    return normalize_safe(j.get("data", []))

# ----------------------- Main UI -----------------------
try:
    fusion = st.secrets["fusion"]
except KeyError:
    st.error("Missing [fusion] section in secrets.")
    st.stop()

st.title("☀️ FusionSolar – Advanced Performance & Technical Dashboard")

col_top1, col_top2, col_top3 = st.columns(3)
show_raw = col_top1.checkbox("Debug: Show raw JSON", value=False)
year = col_top2.number_input("Select Year", min_value=2025, value=2025)

tab1, tab2 = st.tabs(["📊 Performance & Budget", "🔧 Technical & Diagnostics"])

with tab1:
    if st.button("Refresh Plant KPIs"):
        st.session_state["last_run_ts"] = time.time()
        with st.spinner("Analyzing performance data..."):
            client, msg = get_logged_in_client(fusion)
            if client is None:
                st.error(msg)
                st.stop()
            stations, err = client.get_stations()
            if not stations:
                st.error(f"Could not find stations: {err}")
                st.stop()
            stn = stations[0]
            # Prioritize unique IDs over plant names
            station_code = stn.get('stationCode') or stn.get('plantCode')
            
            xsrf = client.s.headers.get("XSRF-TOKEN", "")
            j_mon = get_kpi_station_month_cached(client.base_url, station_code, int(year), xsrf, client.verify_ssl)
            
            # Extraction & Shift logic
            raw_rows = j_mon.get("data", [])
            if raw_rows and isinstance(raw_rows, list) and isinstance(raw_rows[0], dict) and 'kpiList' in raw_rows[0]:
                raw_rows = raw_rows[0]['kpiList']
            
            tmp = normalize_safe(raw_rows)
            tcol = next((c for c in tmp.columns if "time" in c.lower()), None)
            
            if tcol:
                tmp['dt'] = pd.to_datetime(tmp[tcol], unit='ms', utc=True).dt.tz_convert(PLANT_TZ)
                tmp['month'] = ((tmp['dt'].dt.month - 1 + SHIFT_MONTHS) % 12) + 1
            
            # Build Metrics
            df = get_budget_df(int(year))
            df['Actual_kWh'] = 0.0
            
            # Map metrics (prioritize inverterYield) — robust approach with fallback
            def _select_and_aggregate(tmp_df):
                # Try preferred keys first
                prefs = ["dataItemMap.inverterYield", "dataItemMap.PVYield"]
                for k in prefs:
                    if k in tmp_df.columns:
                        col = k
                        vals = pd.to_numeric(tmp_df[col], errors='coerce')
                        if vals.notna().sum() > 0 and vals.sum() > 0:
                            return col, vals

                # Otherwise pick best numeric candidate excluding timestamp/month cols
                exclude_kw = ('time', 'collect', 'dt', 'month')
                candidates = [c for c in tmp_df.columns if not any(kw in c.lower() for kw in exclude_kw)]
                # score candidates by token matches
                priority_tokens = ['yield', 'pv', 'inverter', 'energy', 'power', 'value']
                scored = []
                for c in candidates:
                    low = c.lower()
                    score = sum(1 for t in priority_tokens if t in low)
                    scored.append((score, c))
                scored.sort(reverse=True)
                for _score, c in scored:
                    vals = pd.to_numeric(tmp_df[c], errors='coerce')
                    if vals.notna().sum() > 0 and vals.sum() > 0:
                        return c, vals
                return None, None

            metric_key, metric_vals = _select_and_aggregate(tmp)
            # If we found a metric in monthly payload, aggregate per (shifted) month
            if metric_key is not None:
                tmp['metric'] = metric_vals
                metrics_by_month = tmp.dropna(subset=['metric']).groupby('month')['metric'].sum()
                for m, val in metrics_by_month.items():
                    try:
                        mi = int(m)
                        if 1 <= mi <= 12:
                            idx = (mi - 2) % 12
                            df.at[idx, 'Actual_kWh'] = float(val)
                    except Exception:
                        continue
            else:
                # Fallback: try hourly endpoint, aggregate hourly -> monthly totals
                try:
                    j_hr = get_kpi_station_hour_cached(client.base_url, station_code, int(year), xsrf, client.verify_ssl)
                    raw_hr = j_hr.get('data', [])
                    if raw_hr and isinstance(raw_hr, list) and isinstance(raw_hr[0], dict) and 'kpiList' in raw_hr[0]:
                        raw_hr = raw_hr[0]['kpiList']
                    tmp_hr = normalize_safe(raw_hr)
                    tcol_hr = next((c for c in tmp_hr.columns if 'time' in c.lower() or 'collect' in c.lower()), None)
                    if tcol_hr:
                        tmp_hr['dt'] = pd.to_datetime(tmp_hr[tcol_hr], unit='ms', utc=True).dt.tz_convert(PLANT_TZ)
                        tmp_hr['month'] = ((tmp_hr['dt'].dt.month - 1 + SHIFT_MONTHS) % 12) + 1

                    # reuse selection logic on hourly data
                    metric_key_hr, metric_vals_hr = _select_and_aggregate(tmp_hr)
                    if metric_key_hr is not None:
                        tmp_hr['metric'] = metric_vals_hr
                        metrics_by_month = tmp_hr.dropna(subset=['metric']).groupby('month')['metric'].sum()
                        for m, val in metrics_by_month.items():
                            try:
                                mi = int(m)
                                if 1 <= mi <= 12:
                                    idx = (mi - 2) % 12
                                    df.at[idx, 'Actual_kWh'] = float(val)
                            except Exception:
                                continue
                        if show_raw:
                            st.info(f"Used hourly endpoint with metric '{metric_key_hr}' to compute Actual_kWh")
                except Exception:
                    # silent fallback, leave Actual_kWh as zeros
                    pass

            # Proration for Current Month
            now = pd.Timestamp.now(tz=PLANT_TZ)
            if int(year) == now.year:
                m_idx = now.month - 1
                days_in_month = calendar.monthrange(now.year, now.month)[1]
                factor = now.day / days_in_month
                df.at[m_idx, 'Budget_kWh'] *= factor

            # --- PERFORMANCE METRICS ---
            current_month_idx = now.month if int(year) == now.year else 12
            ytd_actual = df['Actual_kWh'].sum()
            ytd_budget = df.iloc[:current_month_idx]['Budget_kWh'].sum()
            achievement = (ytd_actual / ytd_budget * 100) if ytd_budget > 0 else 0
            
            m1, m2, m3 = st.columns(3)
            m1.metric("YTD Actual", f"{ytd_actual:,.0f} kWh")
            m2.metric("YTD Variance", f"{ytd_actual - ytd_budget:,.0f} kWh", delta=f"{ytd_actual - ytd_budget:,.0f} kWh")
            m3.metric("Budget Achievement", f"{achievement:.1f}%")

            # --- CUMULATIVE CHART ---
            df['Cum_Budget'] = df['Budget_kWh'].cumsum()
            df['Cum_Actual'] = df['Actual_kWh'].cumsum()

            fig_cum = go.Figure()
            fig_cum.add_trace(go.Scatter(x=df['Month'], y=df['Cum_Budget'], name='Target Path', line=dict(dash='dash', color='grey')))
            fig_cum.add_trace(go.Scatter(x=df['Month'], y=df['Cum_Actual'], name='Actual Path', line=dict(width=4, color='orange')))
            fig_cum.update_layout(title="YTD Cumulative Performance Tracking", xaxis_title="Month", yaxis_title="Cumulative kWh")
            st.plotly_chart(fig_cum, use_container_width=True)

            with st.expander("Detailed Monthly Table"):
                temp = df.copy()
                # Ensure numeric columns
                temp['Budget_kWh'] = pd.to_numeric(temp.get('Budget_kWh', 0.0), errors='coerce').fillna(0.0)
                temp['Actual_kWh'] = pd.to_numeric(temp.get('Actual_kWh', 0.0), errors='coerce').fillna(0.0)
                temp['Delta'] = temp['Budget_kWh'] - temp['Actual_kWh']

                desired = ['Year', 'Month', 'Budget_kWh', 'Actual_kWh', 'Delta']
                cols = [c for c in desired if c in temp.columns]
                display_df = temp.loc[:, cols].copy()

                # Convert numeric columns to integers for display
                for num_col in ['Budget_kWh', 'Actual_kWh', 'Delta']:
                    if num_col in display_df.columns:
                        display_df[num_col] = display_df[num_col].round().astype(int)

                # Friendly labels
                display_df.rename(columns={'Budget_kWh': 'Budget kWh', 'Actual_kWh': 'Actual kWh'}, inplace=True)

                # Color Delta: negative red, positive green
                if 'Delta' in display_df.columns:
                    styled = display_df.style.applymap(lambda x: 'color: red' if isinstance(x, (int, float)) and x < 0 else ('color: green' if isinstance(x, (int, float)) and x > 0 else ''), subset=['Delta'])
                    st.dataframe(styled, use_container_width=True)
                else:
                    st.dataframe(display_df, use_container_width=True)

with tab2:
    if st.button("Analyze Device Health"):
        with st.spinner("Retrieving technical telemetry..."):
            client, msg = get_logged_in_client(fusion)
            if client is None:
                st.error(msg)
                st.stop()
            stations, err = client.get_stations()
            if not stations:
                st.error(f"Could not find stations: {err}")
                st.stop()
            stn = stations[0]
            station_code = stn.get('stationCode') or stn.get('plantCode')
            xsrf = client.s.headers.get("XSRF-TOKEN", "")
            
            dev_rows = get_device_list_cached(client.base_url, station_code, xsrf, client.verify_ssl)
            df_devs = normalize_safe(dev_rows)
            
            # Select Inverters (Type 1 or 38)
            inv_ids = df_devs.loc[df_devs['devTypeId'].isin([1, 38, 39]), 'id'].astype(str).tolist()
            if inv_ids:
                df_kpi = get_dev_real_kpi_cached(client.base_url, 1, tuple(inv_ids), xsrf, client.verify_ssl)
                
                # --- TECHNICAL KPI FILTERING ---
                tech_map = {
                    'devId': 'ID',
                    'dataItemMap.active_power': 'AC Power (kW)',
                    'dataItemMap.elec_cap': 'Current (A)',
                    'dataItemMap.u_phase_a': 'Voltage (V)',
                    'dataItemMap.temperature': 'Internal Temp (°C)'
                }
                health_df = df_kpi.rename(columns=tech_map)[[v for v in tech_map.values() if v in df_kpi.rename(columns=tech_map).columns]]
                
                st.subheader("Inverter Health Summary")
                st.dataframe(health_df, use_container_width=True)

                # --- DEVIATION & DEGRADATION CHECK ---
                if 'AC Power (kW)' in health_df.columns:
                    avg_p = health_df['AC Power (kW)'].mean()
                    std_p = health_df['AC Power (kW)'].std()
                    cv = (std_p / avg_p) if avg_p > 0 else 0
                    
                    st.subheader("Automated Diagnostics")
                    if cv > 0.15:
                        st.error(f"⚠️ High Deviation Detected ({cv:.1%}). One or more inverters are underperforming relative to peers.")
                    elif cv > 0.05:
                        st.warning(f"🟡 Moderate Deviation ({cv:.1%}). Check for partial shading or equipment aging.")
                    else:
                        st.success(f"✅ String Uniformity is High ({cv:.1%}). System is balanced.")

st.info("Configuration: Europe/Athens TZ | +1 Month Shift | 2025 Jan-Sep Zero-Policy")