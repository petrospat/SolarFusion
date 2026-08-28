import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timezone
from typing import Optional

st.set_page_config(page_title="Huawei FusionSolar – EU Dashboard", layout="wide")
st.title("Huawei FusionSolar – Europe Region")

# ---- Load secrets safely (fallback to UI if missing) ----
cfg = st.secrets.get("fusion", {})

if not cfg:
    st.warning("⚠️ No [fusion] section in .streamlit/secrets.toml. Enter connection details.")
    with st.form("manual_config"):
        base_url = st.text_input("Base URL", "https://eu5.fusionsolar.huawei.com")
        username = st.text_input("Northbound username")
        system_code = st.text_input("Northbound system code (password)", type="password")
        station_code = st.text_input("Station code (optional, e.g., NE-XXXXXX)")
        verify_ssl = st.checkbox("Verify SSL certificates", value=False)
        timeout_seconds = st.number_input("Timeout (seconds)", min_value=5, max_value=120, value=30, step=1)
        use_port_27200 = st.checkbox("Use port 27200 for /stations", value=False)
        prefer_stations_endpoint = st.checkbox("Prefer /stations over legacy list", value=True)
        submitted = st.form_submit_button("Use these settings")
    if not submitted or not username or not system_code:
        st.stop()
    cfg = {
        "base_url": base_url, "username": username, "system_code": system_code,
        "station_code": station_code or None, "verify_ssl": verify_ssl,
        "timeout_seconds": int(timeout_seconds),
        "use_port_27200": bool(use_port_27200),
        "prefer_stations_endpoint": bool(prefer_stations_endpoint),
    }

# ---- Normalize config values ----
BASE: str = str(cfg.get("base_url", "https://eu5.fusionsolar.huawei.com")).rstrip("/")
USER: str = str(cfg.get("username", ""))
SYS_CODE: str = str(cfg.get("system_code", ""))
DEFAULT_STATION: Optional[str] = cfg.get("station_code") or None
VERIFY_SSL: bool = bool(cfg.get("verify_ssl", False))  # default False (bypass certs)
REQUEST_TIMEOUT: int = int(cfg.get("timeout_seconds", 30))
USE_PORT_27200: bool = bool(cfg.get("use_port_27200", False))
PREFER_STATIONS: bool = bool(cfg.get("prefer_stations_endpoint", True))
PROXIES = cfg.get("proxies") or None

# ---- Optional: suppress warnings when VERIFY_SSL is False ----
if not VERIFY_SSL:
    try:
        from urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
    except Exception:
        pass

def make_session() -> requests.Session:
    s = requests.Session()
    s.verify = VERIFY_SSL
    if PROXIES:
        s.proxies.update(PROXIES)
    s.headers.update({"Content-Type": "application/json", "Connection": "keep-alive"})
    return s

def login_session() -> requests.Session:
    s = make_session()
    url = f"{BASE}/thirdData/login"
    resp = s.post(url, json={"userName": USER, "systemCode": SYS_CODE}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    token = resp.cookies.get("XSRF-TOKEN") or resp.headers.get("xsrf-token") or resp.headers.get("xsrftoken")
    if not token:
        raise RuntimeError("XSRF-TOKEN not found in login response.")
    s.headers.update({"XSRF-TOKEN": token})
    return s

@st.cache_data(ttl=25 * 60, show_spinner=False)
def list_stations() -> pd.DataFrame:
    """
    Robust station discovery:
      1) /thirdData/stations on port 27200 (if enabled)
      2) /thirdData/stations on default port 443
      3) /thirdData/getStationList (legacy) on 443
    """
    s = login_session()

    # Try 1: stations via port 27200 (if configured)
    if PREFER_STATIONS and USE_PORT_27200:
        try:
            r = s.post(f"{BASE}:27200/thirdData/stations", json={"pageNo": "1"}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            j = r.json()
            lst = j.get("data", {}).get("list", [])
            if lst:
                return pd.json_normalize(lst)
        except Exception as e:
            # Report but continue to next approach
            st.info(f"Stations via 27200 unavailable ({e}); trying port 443.")

    # Try 2: stations on port 443 (some deployments host it on 443)
    if PREFER_STATIONS:
        try:
            r2 = s.post(f"{BASE}/thirdData/stations", json={"pageNo": "1"}, timeout=REQUEST_TIMEOUT)
            if r2.status_code == 200:
                j2 = r2.json()
                lst2 = j2.get("data", {}).get("list", [])
                if lst2:
                    return pd.json_normalize(lst2)
        except Exception as e:
            st.info(f"Stations via 443 unavailable ({e}); falling back to legacy.")

    # Try 3: legacy getStationList (documented in Huawei guides & many clients)
    r3 = s.post(f"{BASE}/thirdData/getStationList", json={}, timeout=REQUEST_TIMEOUT)
    r3.raise_for_status()
    j3 = r3.json()
    lst3 = j3.get("data", {}).get("list", j3.get("data", []))
    return pd.json_normalize(lst3)

@st.cache_data(ttl=5 * 60, show_spinner=False)
def station_realtime_kpi(station_code: str) -> dict:
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getStationRealKpi",
               json={"stationCodes": station_code}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=5 * 60, show_spinner=False)
def station_hour_kpi(station_code: str, ts_ms: int) -> dict:
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getStationHourKpi",
               json={"stationCodes": station_code, "collectTime": ts_ms}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# ---- Sidebar connection info ----
with st.sidebar:
    st.markdown("### Connection")
    st.write(f"Base URL: `{BASE}`")
    st.write(f"SSL verify: **{'ON' if VERIFY_SSL else 'OFF'}**")
    st.write(f"Prefer /stations: **{PREFER_STATIONS}**")
    st.write(f"Use port 27200: **{USE_PORT_27200}**")
    if PROXIES:
        st.write("Proxy: ✅")
    else:
        st.write("Proxy: ❌")

# ---- UI flow ----
try:
    df_st = list_stations()
except requests.exceptions.ConnectTimeout as e:
    st.error(f"Timeout reaching station endpoint: {e}")
    st.stop()
except requests.exceptions.SSLError as e:
    st.error(f"SSL error: {e}")
    st.stop()
except Exception as e:
    st.error(f"Station list error: {e}")
    st.stop()

if df_st.empty:
    st.warning("No stations returned. Check Northbound API user permissions (company/plants).")
else:
    cols = [c for c in ["plantName", "plantCode", "capacity", "gridConnectionDate"] if c in df_st.columns]
    st.subheader("Stations")
    st.dataframe(df_st[cols] if cols else df_st, use_container_width=True)

    options = df_st.get("plantCode", pd.Series(dtype=str)).tolist() or df_st.get("stationCode", pd.Series(dtype=str)).tolist()
    if not options:
        st.error("No station codes found in response.")
        st.stop()

    idx = 0
    if DEFAULT_STATION and DEFAULT_STATION in options:
        idx = options.index(DEFAULT_STATION)

    station_code = st.selectbox("Select station", options=options, index=idx)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Real‑time KPIs")
        try:
            j_rt = station_realtime_kpi(station_code)
            st.dataframe(pd.json_normalize(j_rt.get("data", [])), use_container_width=True)
        except Exception as e:
            st.error(f"Real‑time KPI error: {e}")

    with col2:
        st.subheader("Hourly KPIs (today)")
        try:
            midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            j_hr = station_hour_kpi(station_code, int(midnight_utc.timestamp() * 1000))
            df_hr = pd.json_normalize(j_hr.get("data", []))
            st.dataframe(df_hr, use_container_width=True)

            # Quick chart if time + numeric fields exist
            if not df_hr.empty:
                time_col = next((c for c in df_hr.columns if "collectTime" in c or "time" in c.lower()), None)
                num_cols = df_hr.select_dtypes(include="number").columns.tolist()
                if time_col and num_cols:
                    df_plot = df_hr[[time_col] + num_cols].copy()
                    df_plot["time"] = pd.to_datetime(df_plot[time_col], unit="ms", utc=True).dt.tz_convert(None)
                    st.line_chart(df_plot.set_index("time")[num_cols], use_container_width=True)
                else:
                    st.info("No numeric hourly fields detected to plot.")
        except Exception as e:
            st.error(f"Hourly KPI error: {e}")