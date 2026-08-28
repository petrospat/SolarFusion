
# gemini_monthly_app_patched.py
# Streamlit app to fetch monthly KPIs from Huawei FusionSolar Northbound API (patched)
# Patches:
# - Correct login + token handling (cookie/header)
# - Station discovery with legacy fallback
# - Robust monthly parser (supports data[] and data[0].kpiList)
# - Single yearly request + per-month mapping
# - KPI fallback: PVYield -> inverterYield -> inverter_power -> ongrid_power
# - Diagnostics: show failCode/message/params when data is empty
# - Optional raw JSON viewer and Daily KPI quick test

import json
import urllib3
from datetime import datetime, timezone
from typing import Tuple, List, Optional

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

# ----------------------- Page setup -----------------------
st.set_page_config(page_title="Solar Tracker (Northbound API)", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
            # Some deployments put token in JSON data field
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
        # Preferred endpoint
        try:
            r = self.s.post(f"{self.base_url}/thirdData/stations", json={"pageNo": 1}, timeout=20)
            if r.status_code == 200:
                j = r.json()
                lst = j.get("data", {}).get("list")
                if lst:
                    return lst, None
        except Exception:
            pass
        # Legacy fallback
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

    def get_kpi_station_month(self, station_code: str, year: int) -> Tuple[dict, Optional[str]]:
        """Call monthly KPI once for the year; returns full JSON and error if any."""
        # Use the first day of the year; server returns monthly rows for that year
        dt = datetime(year, 1, 1, tzinfo=timezone.utc)
        payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
        try:
            r = self.s.post(f"{self.base_url}/thirdData/getKpiStationMonth", json=payload, timeout=25)
            r.raise_for_status()
            return r.json(), None
        except Exception as e:
            return {}, str(e)

    def get_kpi_station_day(self, station_code: str, dt: datetime) -> Tuple[dict, Optional[str]]:
        payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
        try:
            r = self.s.post(f"{self.base_url}/thirdData/getKpiStationDay", json=payload, timeout=25)
            r.raise_for_status()
            return r.json(), None
        except Exception as e:
            return {}, str(e)

# ----------------------- Helpers -----------------------
def get_budget_df():
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    targets = [200, 250, 350, 450, 550, 600, 620, 580, 480, 350, 220, 180]
    return pd.DataFrame({'Month': months, 'Budget_kWh': targets})

def normalize_safe(rows: List[dict]) -> pd.DataFrame:
    df = pd.json_normalize(rows, sep=".")
    if df.empty:
        return df
    return df.loc[:, ~df.columns.duplicated()].copy()

def extract_monthly_rows(j: dict) -> List[dict]:
    data = j.get("data", [])
    if isinstance(data, list) and data and isinstance(data[0], dict) and "kpiList" in data[0]:
        return data[0]["kpiList"]  # older shape
    return data  # newer shape (your sample)

def pick_metric(row: pd.Series) -> float:
    for k in ["dataItemMap.PVYield", "dataItemMap.inverterYield", "dataItemMap.inverter_power", "dataItemMap.ongrid_power"]:
        if k in row.index:
            v = pd.to_numeric(row[k], errors='coerce')
            if pd.notnull(v):
                return float(v)
    return 0.0

# ----------------------- UI -----------------------
try:
    fusion = st.secrets["fusion"]
except KeyError:
    st.error("🚨 Missing [fusion] section in .streamlit/secrets.toml")
    st.stop()

st.title(f"☀️ Solar Performance ({fusion.get('system_code', 'Unknown System')})")
col_top1, col_top2 = st.columns(2)
show_raw = col_top1.checkbox("Show raw JSON (debug)", value=False)
year = col_top2.number_input("Year", min_value=2000, max_value=datetime.now().year+1, value=datetime.now().year, step=1)

if st.button("Refresh Data", help="Fetch the latest monthly KPIs for the selected year"):
    with st.spinner("Connecting to Northbound API and loading monthly KPIs..."):
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

        # --- Request monthly once for the year ---
        j_mon, err = client.get_kpi_station_month(station_code, int(year))
        if err:
            st.error(f"Monthly API error: {err}")
            st.stop()

        # Diagnostics if empty
        if not j_mon.get("data"):
            st.warning("Monthly API returned empty data[]")
            st.write("failCode:", j_mon.get("failCode"))
            st.write("message:", j_mon.get("message"))
            st.write("params:", j_mon.get("params"))
            st.write("success:", j_mon.get("success"))
            if show_raw:
                with st.expander("Raw monthly JSON"):
                    st.json(j_mon)
            st.stop()

        rows = extract_monthly_rows(j_mon)
        tmp = normalize_safe(rows)
        # Map to months by collectTime
        tcol = "collectTime" if "collectTime" in tmp.columns else None
        if not tcol:
            cand = [c for c in tmp.columns if c.endswith(".collectTime") or "time" in c.lower()]
            tcol = cand[0] if cand else None
        if not tcol:
            st.error("No collectTime column in monthly response")
            if show_raw:
                with st.expander("Raw monthly JSON"):
                    st.json(j_mon)
            st.stop()

        tmp['time'] = pd.to_datetime(tmp[tcol], unit='ms', utc=True).dt.tz_convert(None)
        tmp['month'] = tmp['time'].dt.month

        # Build month -> metric mapping
        df = get_budget_df()
        df['Actual_kWh'] = 0.0
        for m in range(1, 13):
            rm = tmp.loc[tmp['month'] == m]
            if rm.empty:
                continue
            val = pick_metric(rm.iloc[0])
            df.at[m-1, 'Actual_kWh'] = val

        # KPIs
        current_month_idx = min(12, max(1, datetime.now().month))
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

        # Optional quick test of daily KPI for a chosen date
        with st.expander("Quick test: Daily KPI"):
            dt_sel = st.date_input("Pick a date", datetime.now().date())
            dt = datetime(dt_sel.year, dt_sel.month, dt_sel.day, tzinfo=timezone.utc)
            j_day, errd = client.get_kpi_station_day(station_code, dt)
            if errd:
                st.error(f"Daily API error: {errd}")
            else:
                st.json(j_day)

# Initial hint for users
st.info("Set the year and click 'Refresh Data' to load monthly KPIs. Enable 'Show raw JSON' for debugging.")
