import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import requests
import urllib3
from datetime import datetime

# --- CONFIGURATION & SETUP ---
st.set_page_config(page_title="Solar Tracker (Northbound API)", layout="wide")

# Suppress SSL warnings if verify_ssl is false in secrets
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CUSTOM API CLIENT ---
class HuaweiClient:
    """
    Custom client to handle the specific Northbound API requirements 
    (System Code, SSL toggles, etc.)
    """
    def __init__(self, secrets):
        # Read essential configuration from the [fusion] section
        self.base_url = secrets["base_url"].rstrip('/')
        self.username = secrets["username"]
        self.system_code = secrets["system_code"]  # This value is used for both systemCode and password
        self.verify_ssl = secrets.get("verify_ssl", True)
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Streamlit-App"
        }
        self.xsrf_token = None

    def login(self):
        """Authenticates using Username and the dual-purpose System Code."""
        url = f"{self.base_url}/thirdData/login"
        payload = {
            "userName": self.username,
            "systemCode": self.system_code,
            "password": self.system_code  # CRITICAL FIX: Using system_code value as the password
        }
        
        try:
            response = requests.post(url, json=payload, headers=self.headers, verify=self.verify_ssl, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("success"):
                # Retrieve the XSRF token for subsequent requests
                self.xsrf_token = response.headers.get("xsrf-token")
                if not self.xsrf_token and "data" in data:
                     self.xsrf_token = data["data"] 
                
                if self.xsrf_token:
                    self.headers["xsrf-token"] = self.xsrf_token
                
                return True, "Login Successful"
            else:
                return False, f"Login Failed: {data.get('failCode', 'Unknown Error')}"
        except Exception as e:
            return False, str(e)

    def get_stations(self):
        """Fetches the list of stations."""
        url = f"{self.base_url}/thirdData/stations"
        try:
            response = requests.post(url, json={"pageNo": 1, "pageSize": 20}, headers=self.headers, verify=self.verify_ssl)
            response.raise_for_status()
            data = response.json()
            if data.get("success") and data.get("data"):
                return data["data"]["list"], None
            return None, data.get("message", "Failed to fetch stations")
        except Exception as e:
            return None, str(e)

    # --- FUNCTION FOR DAILY DATA RETRIEVAL (One Month at a time) ---
    def get_kpi_month(self, station_code, month_dt):
        """Fetches the daily KPI list for the given month using /getKpiStationMonth."""
        url = f"{self.base_url}/thirdData/getKpiStationMonth"
        payload = {
            "stationCodes": station_code,
            "collectTime": int(month_dt.timestamp() * 1000) 
        }
        try:
            response = requests.post(url, json=payload, headers=self.headers, verify=self.verify_ssl)
            response.raise_for_status()
            data = response.json()
            
            if data.get("success") and data.get("data"):
                # Returns the kpiList which contains daily records
                return data["data"][0].get('kpiList', []), None 
            return [], "No data returned"
        except Exception as e:
            return [], str(e)
    # --- END FUNCTION ---

# --- HELPER: BUDGET DATA ---
def get_budget_df():
    # TODO: EDIT THESE TARGETS with your actual monthly budget targets (kWh)
    targets = [200, 250, 350, 450, 550, 600, 620, 580, 480, 350, 220, 180]
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    return pd.DataFrame({'Month': months, 'Budget_kWh': targets})


# --- MAIN APP LOGIC ---

# 1. Load Secrets
try:
    fusion_config = st.secrets["fusion"]
except KeyError:
    st.error("🚨 Configuration Error: Missing [fusion] section in secrets.toml.")
    st.stop()

st.title(f"☀️ Solar Performance ({fusion_config.get('system_code', 'Unknown System')})")

# 2. Fetch Data Execution
if st.button("Refresh Data", help="Fetch the latest data from the FusionSolar API."):
    with st.spinner("Connecting to Northbound API and aggregating daily data..."):
        client = HuaweiClient(fusion_config)
        
        # Step A: Login
        success, msg = client.login()
        if not success:
            st.error(f"Login Failed: {msg}")
        else:
            # Step B: Get Station Code
            stations, err = client.get_stations()
            if not stations:
                st.error(f"Could not find stations: {err}")
            else:
                my_station = stations[0]
                station_name = my_station.get('plantName', 'N/A')
                capacity = my_station.get('capacity', 'N/A')
                station_code = my_station.get('plantCode')
                
                if not station_code:
                    st.error("Station ID not found (Key 'plantCode' missing). Cannot fetch KPI data.")
                    st.stop()
                
                st.success(f"Connected to: **{station_name}** (Capacity: {capacity} kW)")
                
                
                # --- STEP C: FETCH DAILY DATA AND AGGREGATE ---
                df = get_budget_df()
                df['Actual_kWh'] = 0.0
                year_to_check = datetime.now().year
                
                data_found = False

                for month_num in range(1, 13):
                    # Use the 1st of the month for the collectTime parameter
                    current_month_date = datetime(year_to_check, month_num, 1)
                    
                    # Fetch daily data for this specific month
                    daily_kpis, err = client.get_kpi_month(station_code, current_month_date)
                    
                    if daily_kpis:
                        data_found = True
                        monthly_total_kWh = 0.0
                        
                        for daily_item in daily_kpis:
                            data_map = daily_item.get('dataItemMap', {})
                            
                            # CRITICAL FIX: Use the confirmed working key: 'inverterYield' or 'PVYield'
                            val = data_map.get('inverterYield', 0.0)
                            
                            monthly_total_kWh += val

                        month_idx = month_num - 1
                        df.at[month_idx, 'Actual_kWh'] = monthly_total_kWh

                if data_found:
                    # --- METRICS AND VISUALIZATION ---
                    current_month_idx = datetime.now().month
                    total_actual = df['Actual_kWh'].sum()
                    total_budget_ytd = df.iloc[:current_month_idx]['Budget_kWh'].sum()
                    ytd_diff = total_actual - total_budget_ytd
                    
                    col1, col2 = st.columns(2)
                    col1.metric("Year Total (Actual)", f"{total_actual:.1f} kWh")
                    col2.metric("Variance (YTD)", f"{ytd_diff:.1f} kWh", 
                                delta_color="normal", delta=f"{ytd_diff:.1f} kWh")

                    # --- VISUALIZATION ---
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=df['Month'], y=df['Budget_kWh'], name='Budget', marker_color='lightgrey'))
                    fig.add_trace(go.Scatter(x=df['Month'], y=df['Actual_kWh'], name='Actual', mode='lines+markers', line=dict(color='orange', width=4)))
                    fig.update_layout(title="Monthly Performance vs Budget", hovermode="x unified")
                    
                    st.plotly_chart(fig, use_container_width=True)
                    
                    with st.expander("Raw Data Table"):
                        st.write(df)
                        
                else:
                    st.warning("Could not retrieve any daily data for aggregation. Please check Northbound API permissions and data scope on the FusionSolar web portal.")


# Provide status if data hasn't been fetched yet
if not st.session_state.get('data_fetched', False):
    st.info("Click 'Refresh Data' to connect and load the initial performance charts.")