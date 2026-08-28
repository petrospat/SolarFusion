
# gemini_monthly_app_fixed.py
# Streamlit app to fetch monthly KPIs from Huawei FusionSolar Northbound API
# Fixes:
# - Correct login token handling (cookie + header)
# - Robust /stations discovery with fallback
# - Correct parsing of getKpiStationMonth response (no kpiList in your sample)
# - Metric fallback (PVYield -> inverterYield -> inverter_power -> ongrid_power)
# - Month matching by collectTime; no accidental year sum
# - Optional raw JSON viewer

import json
import urllib3
from datetime import datetime, timezone
from typing import Optional, List, Tuple

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
            # Token can be in cookie or headers; accept common spellings
            token = (
                resp.cookies.get("XSRF-TOKEN")
                or resp.headers.get("xsrf-token")
                or resp.headers.get("xsrftoken")
                or resp.headers.get("XSRF-TOKEN")
            )
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            if not token and isinstance(data, dict):
                token = data.get("data")  # some older deployments
            if not token:
                return False, "Login succeeded but no XSRF token returned"
            # Use header name expected by API
            self.s.headers.update({"XSRF-TOKEN": token})
            return True, "Login Successful"
        except Exception as e:
            return False, f"Login error: {e}"

    def get_stations(self) -> Tuple[Optional[list], Optional[str]]:
        # Preferred endpoint on 443
        url = f"{self.base_url}/thirdData/stations"
        try:
            r = self.s.post(url, json={"pageNo": 1}, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if j.get("data", {}).get("list"):
                    return j["data"]["list"], None
        except Exception:
            pass
        # Fallback to legacy list
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

    def get_kpi_month(self, station_code: str, month_dt: datetime) -> Tuple[list, Optional[str]]:
        """Return the *list of monthly rows* for the year of month_dt.
        In your tenant, /thirdData/getKpiStationMonth returns a list under data[],
        each with collectTime (ms) and dataItemMap {...}.
        """
        url = f"{self.base_url}/thirdData/getKpiStationMonth"
        payload = {"stationCodes": station_code, "collectTime": int(month_dt.replace(day=1, tzinfo=timezone.utc).timestamp() * 1000)}
        try:
            r = self.s.post(url, json=payload, timeout=25)
            r.raise_for_status()
            j = r.json()
            # Two possible shapes depending on version; support both
            data = j.get("data", [])
            if isinstance(data, list) and data and isinstance(data[0], dict):
                if "kpiList" in data[0]:
                    return data[0]["kpiList"], None  # older doc shape
                else:
                    return data, None  # your sample shape
            return [], "No data returned"
        except Exception as e:
            return [], str(e)

# ----------------------- Budget helper -----------------------
def get_budget_df():
    targets = [200, 250, 350, 450, 550, 600, 620, 580, 480, 350, 220, 180]
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    return pd.DataFrame({'Month': months, 'Budget_kWh': targets})

# ----------------------- UI -----------------------
try:
    fusion_config = st.secrets["fusion"]
except KeyError:
    st.error("🚨 Configuration Error: Missing [fusion] section in secrets.toml.")
    st.stop()

st.title(f"☀️ Solar Performance ({fusion_config.get('system_code', 'Unknown System')})")
show_raw = st.checkbox("Show raw JSON (debug)", value=False)

if st.button("Refresh Data", help="Fetch the latest data from the FusionSolar API."):
    with st.spinner("Connecting to Northbound API and loading monthly KPIs..."):
        client = HuaweiClient(fusion_config)
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
            st.error("Station ID not found (plantCode/stationCode missing).")
            st.stop()
        st.success(f"Connected to **{stn_name}** (Capacity: {capacity} kW)")

        # Prepare dataframe
        df = get_budget_df()
        df['Actual_kWh'] = 0.0
        year = datetime.now().year

        # Metric preference order based on your sample
        metric_keys = ["PVYield", "inverterYield", "inverter_power", "ongrid_power"]

        any_data = False
        for month_num in range(1, 13):
            query_dt = datetime(year, month_num, 1)
            rows, err = client.get_kpi_month(station_code, query_dt)
            if show_raw and month_num == 1:
                with st.expander("Raw JSON (first monthly response)"):
                    st.json({"data": rows})
                    st.download_button("Download JSON", json.dumps({"data": rows}, indent=2).encode("utf-8"), "month_api_sample.json", "application/json")

            if not rows:
                continue

            any_data = True
            # Normalize to DataFrame to map months easily
            tmp = pd.json_normalize(rows, sep=".")
            tmp = tmp.loc[:, ~tmp.columns.duplicated()].copy()

            # Determine collectTime column
            tcol = "collectTime" if "collectTime" in tmp.columns else None
            if not tcol:
                # Older shape: sometimes under params.collectTime (unlikely here)
                cand = [c for c in tmp.columns if c.endswith(".collectTime") or "time" in c.lower()]
                tcol = cand[0] if cand else None

            if not tcol:
                continue

            tmp['time'] = pd.to_datetime(tmp[tcol], unit='ms', utc=True).dt.tz_convert(None)
            tmp['month'] = tmp['time'].dt.month

            # Find the row for the current month
            row_m = tmp.loc[tmp['month'] == month_num]
            if row_m.empty:
                continue

            # Pick the first available metric in preference order
            val = None
            for k in metric_keys:
                col = f"dataItemMap.{k}"
                if col in row_m.columns:
                    v = pd.to_numeric(row_m.iloc[0][col], errors='coerce')
                    if pd.notnull(v):
                        val = float(v)
                        break
            if val is None:
                val = 0.0

            df.at[month_num - 1, 'Actual_kWh'] = val

        if any_data:
            current_month_idx = datetime.now().month
            total_actual = float(df['Actual_kWh'].sum())
            total_budget_ytd = float(df.iloc[:current_month_idx]['Budget_kWh'].sum())
            ytd_diff = total_actual - total_budget_ytd

            col1, col2 = st.columns(2)
            col1.metric("Year Total (Actual)", f"{total_actual:.1f} kWh")
            col2.metric("Variance (YTD)", f"{ytd_diff:.1f} kWh", delta=f"{ytd_diff:.1f} kWh", delta_color="normal")

            # Chart
            fig = go.Figure()
            fig.add_trace(go.Bar(x=df['Month'], y=df['Budget_kWh'], name='Budget', marker_color='lightgrey'))
            fig.add_trace(go.Scatter(x=df['Month'], y=df['Actual_kWh'], name='Actual', mode='lines+markers', line=dict(color='orange', width=4)))
            fig.update_layout(title=f"Monthly Performance vs Budget ({year})", hovermode="x unified")
            st.plotly_chart(fig, use_container_width=True)

            with st.expander("Raw Data Table"):
                st.dataframe(df, use_container_width=True)
        else:
            st.warning("No monthly KPI data returned. Check API permissions and data availability for this year.")

if not st.session_state.get('data_fetched', False):
    st.info("Click 'Refresh Data' to connect and load the initial performance charts.")
