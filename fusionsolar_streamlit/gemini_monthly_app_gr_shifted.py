
# gemini_monthly_app_gr_shifted.py
# Streamlit app for Huawei FusionSolar Northbound API
# Monthly plant KPIs (rate-limit aware) + Device real-time KPIs tab
# Greece plant: set PLANT_TZ = "Europe/Athens" and apply +1 month shift after tz conversion (Option A)

import json
import time
import math
import urllib3
from datetime import datetime, timezone
from typing import Tuple, List, Optional, Union

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

# ----------------------- Page setup -----------------------
st.set_page_config(page_title="FusionSolar – GR Plant (Shifted Month)", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------- Config: timezone & shift -----------------------
PLANT_TZ = "Europe/Athens"  # Greece
SHIFT_MONTHS = 1            # production appears one month early; shift data forward by 1 month

# ----------------------- Site-specific counts -----------------------
DEVICE_COUNTS_HINT = {"inverter": 3}
PLANT_COUNT_HINT = 1
COOLDOWN_SECONDS = 60

# ----------------------- Backoff helper -----------------------
def post_with_backoff(session: requests.Session, url: str, json_payload: dict,
                      timeout: int = 25, max_retries: int = 3, base_sleep: int = 30):
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
                sleep_for = base_sleep * (attempt + 1)
                st.warning(f"API frequency too high (407). Waiting {sleep_for}s before retry...")
                time.sleep(sleep_for)
                continue
            return j
        sleep_for = base_sleep * (attempt + 1)
        time.sleep(sleep_for)
    if last_resp is not None:
        last_resp.raise_for_status()
        return last_resp.json()
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
        df = pd.json_normalize(rows, sep='.')
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

# ----------------------- Monthly Plant API (cached) -----------------------
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

# ----------------------- Device APIs -----------------------
@st.cache_data(ttl=25 * 60, show_spinner=False)
def get_device_list_cached(base_url: str, station_code: str, xsrf_token: str, verify_ssl: bool) -> List[dict]:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "User-Agent": "Streamlit-App", "XSRF-TOKEN": xsrf_token})
    payload = {"stationCodes": station_code}
    url = f"{base_url}/thirdData/getDevList"
    j = post_with_backoff(s, url, payload, timeout=25, max_retries=3, base_sleep=30)
    rows = j.get("data", {}).get("list", j.get("data", []))
    return _coerce_rows(rows)

@st.cache_data(ttl=5 * 60, show_spinner=False)
def get_dev_real_kpi_cached(base_url: str, dev_type_id: int, dev_ids_tuple: tuple,
                            xsrf_token: str, verify_ssl: bool) -> pd.DataFrame:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "User-Agent": "Streamlit-App", "XSRF-TOKEN": xsrf_token})
    ids = list(map(str, dev_ids_tuple))
    chunks = [ids[i:i+100] for i in range(0, len(ids), 100)]
    frames = []
    for ch in chunks:
        payload = {"devTypeId": dev_type_id, "devIds": ",".join(ch)}
        url = f"{base_url}/thirdData/getDevRealKpi"
        j = post_with_backoff(s, url, payload, timeout=25, max_retries=3, base_sleep=30)
        frames.append(normalize_safe(j.get("data", [])))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def estimate_device_budget(df_devs: pd.DataFrame) -> int:
    if "devTypeId" not in df_devs.columns or df_devs.empty:
        return 0
    total = 0
    for t in sorted(df_devs["devTypeId"].dropna().unique().tolist()):
        cnt = int((df_devs["devTypeId"] == t).sum())
        total += math.ceil(cnt / 100)
    return total

# ----------------------- UI -----------------------
try:
    fusion = st.secrets["fusion"]
except KeyError:
    st.error("🚨 Missing [fusion] section in .streamlit/secrets.toml")
    st.stop()

st.title("☀️ FusionSolar – GR Plant (Shifted Month)")
col_top1, col_top2, col_top3 = st.columns(3)
show_raw = col_top1.checkbox("Show raw JSON (debug)", value=False)
year = col_top2.number_input("Year", min_value=2000, max_value=datetime.now().year+1, value=datetime.now().year, step=1)

device_budget_hint = math.ceil(DEVICE_COUNTS_HINT.get("inverter", 0) / 100)
plant_budget_hint = math.ceil(PLANT_COUNT_HINT / 100)
col_top3.info(f"Hinted budget (per 5 min):\n"
              f"• Devices: {device_budget_hint} call\n"
              f"• Plants: {plant_budget_hint} call")

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

# Tabs
tab1, tab2 = st.tabs(["Monthly Plant KPIs", "Device Real-time KPIs"])

with tab1:
    if st.button("Refresh Monthly", help="Fetch monthly KPIs for the selected year") and can_run:
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

            xsrf_token = client.s.headers.get("XSRF-TOKEN", "")
            j_mon = get_kpi_station_month_cached(client.base_url, station_code, int(year), xsrf_token, client.verify_ssl)

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

            # --- Timezone conversion + +1 month shift (Option A) ---
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

            try:
                tmp_time = (pd.to_datetime(tmp[tcol], unit='ms', utc=True)
                            .dt.tz_convert(PLANT_TZ)
                            .dt.tz_convert(None))
            except Exception as e:
                st.info(f"Plant TZ conversion failed ({e}); using UTC.")
                tmp_time = pd.to_datetime(tmp[tcol], unit='ms', utc=True).dt.tz_convert(None)

            month0 = tmp_time.dt.month - 1
            month_shifted0 = (month0 + SHIFT_MONTHS) % 12
            tmp['month'] = month_shifted0 + 1

            df = get_budget_df()
            df['Actual_kWh'] = 0.0
            for m in range(1, 13):
                rm = tmp.loc[tmp['month'] == m]
                if rm.empty:
                    continue
                df.at[m-1, 'Actual_kWh'] = pick_metric(rm.iloc[0])

            current_month_idx = min(12, max(1, datetime.now().month))
            total_actual = float(df['Actual_kWh'].sum())
            total_budget_ytd = float(df.iloc[:current_month_idx]['Budget_KWh'].sum()) if 'Budget_KWh' in df.columns else float(df.iloc[:current_month_idx]['Budget_kWh'].sum())
            ytd_diff = total_actual - total_budget_ytd
            c1, c2 = st.columns(2)
            c1.metric("Year Total (Actual)", f"{total_actual:.1f} kWh")
            c2.metric("Variance (YTD)", f"{ytd_diff:.1f} kWh", delta=f"{ytd_diff:.1f} kWh", delta_color="normal")

            fig = go.Figure()
            fig.add_trace(go.Bar(x=df['Month'], y=df.get('Budget_KWh', df['Budget_kWh']), name='Budget', marker_color='lightgrey'))
            fig.add_trace(go.Scatter(x=df['Month'], y=df['Actual_kWh'], name='Actual', mode='lines+markers', line=dict(color='orange', width=4)))
            fig.update_layout(title=f"Monthly Performance vs Budget ({int(year)})", hovermode="x unified")
            st.plotly_chart(fig, use_container_width=True)
            with st.expander("Raw Data Table"):
                st.dataframe(df, use_container_width=True)

with tab2:
    if st.button("Refresh Devices", help="Fetch device inventory and real-time KPIs") and can_run:
        st.session_state["last_run_ts"] = time.time()
        with st.spinner("Connecting and loading device KPIs..."):
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
            station_code = stn.get('plantCode') or stn.get('stationCode')
            stn_name = stn.get('plantName') or stn.get('stationName', 'N/A')
            capacity = stn.get('capacity', 'N/A')
            st.success(f"Connected to **{stn_name}** (Capacity: {capacity} kW)")

            xsrf_token = client.s.headers.get("XSRF-TOKEN", "")
            dev_rows = get_device_list_cached(client.base_url, station_code, xsrf_token, client.verify_ssl)
            df_devs = normalize_safe(dev_rows)
            if df_devs.empty:
                st.info("No devices found for this plant.")
                st.stop()

            ren = {"id": "devId", "devName": "devName", "stationCode": "stationCode", "esnCode": "sn", "devTypeId": "devTypeId"}
            for k, v in ren.items():
                if k in df_devs.columns:
                    df_devs.rename(columns={k: v}, inplace=True)

            st.dataframe(df_devs, use_container_width=True)

            live_budget = estimate_device_budget(df_devs)
            st.info(f"Estimated device KPI budget (per 5 min): {live_budget} call(s)")

            type_choices = sorted(df_devs["devTypeId"].dropna().unique().tolist()) if "devTypeId" in df_devs.columns else []
            dev_type = st.selectbox("Device type", options=type_choices, format_func=lambda t: f"{int(t)}") if type_choices else None
            if dev_type is None:
                st.stop()

            id_list = df_devs.loc[df_devs["devTypeId"] == dev_type, "devId"].astype(str).tolist()
            count = len(id_list)
            st.write(f"Found {count} device(s) of type {int(dev_type)}")
            sample_n = st.slider("Number of devices to query now", min_value=1, max_value=max(1, count), value=min(3, count))
            id_sample = tuple(id_list[:sample_n])

            df_kpi = get_dev_real_kpi_cached(client.base_url, int(dev_type), id_sample, xsrf_token, client.verify_ssl)
            if df_kpi.empty:
                st.warning("No KPI rows returned for selected devices. Try fewer devices or wait for cooldown.")
            else:
                st.dataframe(df_kpi, use_container_width=True)

st.info("Plant timezone set to Europe/Athens and data months shifted by +1. Adjust SHIFT_MONTHS if needed.")
