
# fusion_solar_dashboard.py
# Single-file Streamlit app for Huawei FusionSolar SmartPVMS Northbound API
# - Plants & Devices dashboards
# - Real-time + Hour/Day/Month/Year reports
# - Robust JSON normalization (handles duplicate keys)
# - Raw JSON expanders + download
# - CSV/Excel export and rate-limit budget indicator
#
# Author: M365 Copilot

import json
from typing import Optional, List

import pandas as pd
import requests
import streamlit as st
from datetime import datetime, timezone

# ----------------------- Page setup -----------------------
st.set_page_config(page_title="FusionSolar – All-in-One Dashboard", layout="wide")
st.title("Huawei FusionSolar – All-in-One (Plants & Devices)")

# ----------------------- Secrets or form -----------------------
cfg = st.secrets.get("fusion", {})
if not cfg:
    st.warning("⚠️ No [fusion] section in .streamlit/secrets.toml. Enter connection details.")
    with st.form("manual_config"):
        base_url = st.text_input("Base URL", "https://eu5.fusionsolar.huawei.com")
        username = st.text_input("Northbound username")
        system_code = st.text_input("Northbound system code (password)", type="password")
        station_code = st.text_input("Default station code (optional)")
        verify_ssl = st.checkbox("Verify SSL certificates", value=False)
        timeout_seconds = st.number_input("Timeout (seconds)", min_value=5, max_value=120, value=30, step=1)
        use_port_27200 = st.checkbox("Use port 27200 for /stations", value=False)
        prefer_stations_endpoint = st.checkbox("Prefer /stations over legacy list", value=True)
        submitted = st.form_submit_button("Use these settings")
        if not submitted or not username or not system_code:
            st.stop()
        cfg = {
            "base_url": base_url,
            "username": username,
            "system_code": system_code,
            "station_code": station_code or None,
            "verify_ssl": verify_ssl,
            "timeout_seconds": int(timeout_seconds),
            "use_port_27200": bool(use_port_27200),
            "prefer_stations_endpoint": bool(prefer_stations_endpoint),
        }

# ----------------------- Normalize -----------------------
BASE: str = str(cfg.get("base_url", "https://eu5.fusionsolar.huawei.com")).rstrip("/")
USER: str = str(cfg.get("username", ""))
SYS_CODE: str = str(cfg.get("system_code", ""))
DEFAULT_STATION: Optional[str] = cfg.get("station_code") or None
VERIFY_SSL: bool = bool(cfg.get("verify_ssl", False))
REQUEST_TIMEOUT: int = int(cfg.get("timeout_seconds", 30))
USE_PORT_27200: bool = bool(cfg.get("use_port_27200", False))
PREFER_STATIONS: bool = bool(cfg.get("prefer_stations_endpoint", True))
PROXIES = cfg.get("proxies") or None

# ----------------------- Optional: suppress insecure warnings -----------------------
if not VERIFY_SSL:
    try:
        from urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
    except Exception:
        pass

# ----------------------- Helpers: safe JSON normalize & time column detection -----------------------
def normalize_safe(data, sep: str = ".") -> pd.DataFrame:
    """
    Flatten JSON data to a DataFrame, remove duplicate columns, and return a copy.
    Avoids 'cannot assemble with duplicate keys' errors.
    """
    try:
        df = pd.json_normalize(data, sep=sep)
    except Exception:
        # Fallback: treat dict as single-row list
        if isinstance(data, dict):
            df = pd.json_normalize([data], sep=sep)
        else:
            df = pd.DataFrame()
    if df.empty:
        return df
    # Deduplicate columns
    df = df.loc[:, ~df.columns.duplicated()].copy()
    return df


def detect_time_column(df: pd.DataFrame) -> Optional[str]:
    """
    Find a plausible time column for KPI rows.
    Priority: 'collectTime', any '*.collectTime', else anything containing 'time'.
    """
    candidates = [
        c for c in df.columns
        if c == "collectTime" or c.endswith(".collectTime") or ("time" in c.lower())
    ]
    return candidates[0] if candidates else None


def download_button_for_json(obj: dict, label: str, filename: str):
    raw_bytes = json.dumps(obj, indent=2).encode("utf-8")
    st.download_button(label=label, data=raw_bytes, file_name=filename, mime="application/json")


# ----------------------- HTTP session & login -----------------------
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

# ----------------------- Plant discovery (robust) -----------------------
@st.cache_data(ttl=25 * 60, show_spinner=False)
def list_stations() -> pd.DataFrame:
    s = login_session()
    # Try 1: stations via port 27200
    if PREFER_STATIONS and USE_PORT_27200:
        try:
            r = s.post(f"{BASE}:27200/thirdData/stations", json={"pageNo": "1"}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            lst = r.json().get("data", {}).get("list", [])
            if lst:
                return normalize_safe(lst)
        except Exception as e:
            st.info(f"Stations via 27200 unavailable ({e}); trying port 443.")
    # Try 2: stations on port 443
    if PREFER_STATIONS:
        try:
            r2 = s.post(f"{BASE}/thirdData/stations", json={"pageNo": "1"}, timeout=REQUEST_TIMEOUT)
            if r2.status_code == 200:
                lst2 = r2.json().get("data", {}).get("list", [])
                if lst2:
                    return normalize_safe(lst2)
        except Exception as e:
            st.info(f"Stations via 443 unavailable ({e}); falling back to legacy.")
    # Try 3: legacy getStationList
    r3 = s.post(f"{BASE}/thirdData/getStationList", json={}, timeout=REQUEST_TIMEOUT)
    r3.raise_for_status()
    j3 = r3.json()
    lst3 = j3.get("data", {}).get("list", j3.get("data", []))
    return normalize_safe(lst3)

# ----------------------- Plant KPI helpers -----------------------
@st.cache_data(ttl=5 * 60, show_spinner=False)
def station_realtime_kpi(station_codes: List[str]) -> dict:
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getStationRealKpi",
               json={"stationCodes": ",".join(station_codes)}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=5 * 60, show_spinner=False)
def station_hour_kpi(station_codes: List[str], ts_ms: int) -> dict:
    """
    Prefer /thirdData/getKpiStationHour; fall back to /thirdData/getStationHourKpi
    (some deployments use the older name).
    """
    s = login_session()
    payload = {"stationCodes": ",".join(station_codes), "collectTime": ts_ms}
    last_exc = None
    for ep in ("/thirdData/getKpiStationHour", "/thirdData/getStationHourKpi"):
        try:
            r = s.post(f"{BASE}{ep}", json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            continue
    # If both failed, raise the last exception
    raise last_exc if last_exc else RuntimeError("Hourly KPI endpoints not available")

@st.cache_data(ttl=10 * 60, show_spinner=False)
def station_day_kpi(station_codes: List[str], ts_ms: int) -> dict:
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getKpiStationDay",
               json={"stationCodes": ",".join(station_codes), "collectTime": ts_ms},
               timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=15 * 60, show_spinner=False)
def station_month_kpi(station_codes: List[str], ts_ms: int) -> dict:
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getKpiStationMonth",
               json={"stationCodes": ",".join(station_codes), "collectTime": ts_ms},
               timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=30 * 60, show_spinner=False)
def station_year_kpi(station_codes: List[str], ts_ms: int) -> dict:
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getKpiStationYear",
               json={"stationCodes": ",".join(station_codes), "collectTime": ts_ms},
               timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# ----------------------- Device list & KPIs -----------------------
DEVICE_TYPES = {
    1: "String inverter",
    17: "Grid meter",
    38: "Residential inverter",
    39: "Battery",
    41: "ESS",
    47: "Power sensor",
}

@st.cache_data(ttl=25 * 60, show_spinner=False)
def list_devices(station_codes: List[str]) -> pd.DataFrame:
    """Call /thirdData/getDevList for one or more plants, returns device catalog."""
    s = login_session()
    r = s.post(f"{BASE}/thirdData/getDevList",
               json={"stationCodes": ",".join(station_codes)},
               timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json().get("data", {}).get("list", r.json().get("data", []))
    df = normalize_safe(rows)
    # canonical names (when present)
    rename = {"id": "devId", "devName": "devName", "stationCode": "stationCode", "esnCode": "sn", "devTypeId": "devTypeId"}
    for k, v in rename.items():
        if k in df.columns:
            df.rename(columns={k: v}, inplace=True)
    return df

@st.cache_data(ttl=5 * 60, show_spinner=False)
def device_realtime_kpi(dev_type_id: int, dev_ids: List[str]) -> pd.DataFrame:
    """Batch <=100 IDs per call; concatenate results into a DataFrame."""
    s = login_session()
    chunks = [dev_ids[i:i+100] for i in range(0, len(dev_ids), 100)]
    frames = []
    for ch in chunks:
        r = s.post(f"{BASE}/thirdData/getDevRealKpi",
                   json={"devTypeId": dev_type_id, "devIds": ",".join(map(str, ch))},
                   timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        frames.append(normalize_safe(r.json().get("data", [])))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

# Optional device reports (availability can vary by tenant/version)
@st.cache_data(ttl=10 * 60, show_spinner=False)
def device_day_kpi(dev_type_id: int, dev_ids: List[str], ts_ms: int) -> pd.DataFrame:
    s = login_session()
    frames = []
    for ch in [dev_ids[i:i+100] for i in range(0, len(dev_ids), 100)]:
        r = s.post(f"{BASE}/thirdData/getKpiDevDay",
                   json={"devTypeId": dev_type_id, "devIds": ",".join(map(str, ch)), "collectTime": ts_ms},
                   timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        frames.append(normalize_safe(r.json().get("data", [])))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

@st.cache_data(ttl=15 * 60, show_spinner=False)
def device_month_kpi(dev_type_id: int, dev_ids: List[str], ts_ms: int) -> pd.DataFrame:
    s = login_session()
    frames = []
    for ch in [dev_ids[i:i+100] for i in range(0, len(dev_ids), 100)]:
        r = s.post(f"{BASE}/thirdData/getKpiDevMonth",
                   json={"devTypeId": dev_type_id, "devIds": ",".join(map(str, ch)), "collectTime": ts_ms},
                   timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        frames.append(normalize_safe(r.json().get("data", [])))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

@st.cache_data(ttl=30 * 60, show_spinner=False)
def device_year_kpi(dev_type_id: int, dev_ids: List[str], ts_ms: int) -> pd.DataFrame:
    s = login_session()
    frames = []
    for ch in [dev_ids[i:i+100] for i in range(0, len(dev_ids), 100)]:
        r = s.post(f"{BASE}/thirdData/getKpiDevYear",
                   json={"devTypeId": dev_type_id, "devIds": ",".join(map(str, ch)), "collectTime": ts_ms},
                   timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        frames.append(normalize_safe(r.json().get("data", [])))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

# ----------------------- Helper: API budget estimator -----------------------
def estimate_device_budget(df_devs: pd.DataFrame) -> int:
    """Estimate max device API calls per 5 minutes: sum ceil(count(type)/100)."""
    import math
    if "devTypeId" not in df_devs.columns or df_devs.empty:
        return 0
    total = 0
    for t in sorted(df_devs["devTypeId"].dropna().unique().tolist()):
        cnt = int((df_devs["devTypeId"] == t).sum())
        total += math.ceil(cnt/100) if cnt else 0
    return total

# ----------------------- Sidebar -----------------------
with st.sidebar:
    st.markdown("### Connection")
    st.write(f"Base URL: `{BASE}`")
    st.write(f"SSL verify: **{'ON' if VERIFY_SSL else 'OFF'}**")
    st.write(f"Prefer /stations: **{PREFER_STATIONS}**")
    st.write(f"Use port 27200: **{USE_PORT_27200}**")
    st.button("🔄 Refresh (clear cache)", on_click=st.cache_data.clear)
    show_raw = st.checkbox("Show raw JSON expanders", value=False)
    if PROXIES:
        st.write("Proxy: ✅")
    else:
        st.write("Proxy: ❌")

# ----------------------- Main UI -----------------------
try:
    df_plants = list_stations()
except requests.exceptions.ConnectTimeout as e:
    st.error(f"Timeout reaching station endpoint: {e}")
    st.stop()
except requests.exceptions.SSLError as e:
    st.error(f"SSL error: {e}")
    st.stop()
except Exception as e:
    st.error(f"Station list error: {e}")
    st.stop()

if df_plants.empty:
    st.warning("No stations returned. Check Northbound API user permissions (company/plants).")
    st.stop()

# Friendly view of plants
cols = [c for c in ["plantName", "plantCode", "capacity", "gridConnectionDate"] if c in df_plants.columns]
st.subheader("Stations")
st.dataframe(df_plants[cols] if cols else df_plants, use_container_width=True)

# Station selection
options = df_plants.get("plantCode", pd.Series(dtype=str)).tolist() or df_plants.get("stationCode", pd.Series(dtype=str)).tolist()
if not options:
    st.error("No station codes found in response.")
    st.stop()
idx = 0
if DEFAULT_STATION and DEFAULT_STATION in options:
    idx = options.index(DEFAULT_STATION)
station_code = st.selectbox("Select station", options=options, index=idx)

# KPI tabs
tab1, tab2, tab3, tab4 = st.tabs(["Plant KPIs", "Devices", "Reports", "Export"])

with tab1:
    colA, colB = st.columns(2)

    # --------- Real-time KPIs (plant) ----------
    with colA:
        st.caption("Real‑time KPIs (plant)")
        try:
            j_rt = station_realtime_kpi([station_code])
            df_rt = normalize_safe(j_rt.get("data", []))
            st.dataframe(df_rt, use_container_width=True)
            if show_raw:
                with st.expander("Raw JSON (Plant Real-time)"):
                    st.json(j_rt)
                    download_button_for_json(j_rt, "Download real-time JSON", "plant_realtime_kpi.json")
        except Exception as e:
            st.error(f"Real‑time KPI error: {e}")

    # --------- Hourly KPIs (today) ----------
    with colB:
        st.caption("Hourly KPIs (today)")
        try:
            midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            j_hr = station_hour_kpi([station_code], int(midnight_utc.timestamp() * 1000))
            df_hr = normalize_safe(j_hr.get("data", []), sep=".")
            st.dataframe(df_hr, use_container_width=True)

            if show_raw:
                with st.expander("Raw JSON (Plant Hourly)"):
                    st.json(j_hr)
                    download_button_for_json(j_hr, "Download hourly JSON", "plant_hourly_kpi.json")

            # Quick chart if time + numeric fields exist
            if not df_hr.empty:
                time_col = detect_time_column(df_hr)
                num_cols = list(dict.fromkeys(df_hr.select_dtypes(include="number").columns.tolist()))
                if time_col and num_cols:
                    df_plot = df_hr[[time_col] + num_cols].copy()
                    try:
                        df_plot["time"] = pd.to_datetime(df_plot[time_col], unit="ms", utc=True).dt.tz_convert(None)
                    except Exception:
                        df_plot["time"] = pd.to_datetime(df_plot[time_col], utc=True).tz_convert(None)
                    df_plot = df_plot.drop_duplicates(subset=["time"])
                    st.line_chart(df_plot.set_index("time")[num_cols], use_container_width=True)
                else:
                    st.info("No numeric hourly fields or time column detected to plot.")
        except Exception as e:
            st.error(f"Hourly KPI error: {e}")

with tab2:
    st.caption("Device inventory & real‑time KPIs")
    try:
        df_devs = list_devices([station_code])
        if df_devs.empty:
            st.info("No devices found for this plant.")
        else:
            st.dataframe(df_devs, use_container_width=True)
            budget = estimate_device_budget(df_devs)
            st.info(f"Estimated max device API calls per 5 minutes: **{budget}** (sum of ceil(count/type/100)).")

            type_choices = sorted(df_devs["devTypeId"].dropna().unique().tolist()) if "devTypeId" in df_devs.columns else []
            dev_type = st.selectbox(
                "Select device type for KPIs",
                options=type_choices,
                format_func=lambda t: f"{t} – {DEVICE_TYPES.get(int(t), 'Unknown')}" if pd.notnull(t) else str(t)
            ) if type_choices else None

            if dev_type is not None:
                dev_ids = df_devs.loc[df_devs["devTypeId"] == dev_type, "devId"].astype(str).tolist()
                df_kpi = device_realtime_kpi(int(dev_type), dev_ids)
                st.dataframe(df_kpi, use_container_width=True)
                if show_raw:
                    # We don't have the raw JSON blob here (we normalized each batch),
                    # but you can always re-call a small subset to show the raw:
                    s_ids = dev_ids[: min(5, len(dev_ids))]  # small sample
                    s = login_session()
                    r = s.post(f"{BASE}/thirdData/getDevRealKpi",
                               json={"devTypeId": int(dev_type), "devIds": ",".join(s_ids)},
                               timeout=REQUEST_TIMEOUT)
                    r.raise_for_status()
                    j_dev_rt = r.json()
                    with st.expander("Raw JSON (Device Real-time sample)"):
                        st.json(j_dev_rt)
                        download_button_for_json(j_dev_rt, "Download device real-time JSON (sample)", "device_realtime_kpi_sample.json")

    except Exception as e:
        st.error(f"Device list/KPI error: {e}")

with tab3:
    st.caption("Plant & Device reports (daily/monthly/yearly)")

    today = datetime.now(timezone.utc).date()
    date_sel = st.date_input("Select a date for daily report", today)
    dt_ms = int(datetime(date_sel.year, date_sel.month, date_sel.day, tzinfo=timezone.utc).timestamp() * 1000)

    # Plant Daily/Monthly/Yearly
    colPD, colPM, colPY = st.columns(3)
    with colPD:
        st.subheader("Plant – Daily")
        try:
            j_day = station_day_kpi([station_code], dt_ms)
            df_day = normalize_safe(j_day.get("data", []))
            st.dataframe(df_day, use_container_width=True)
            if show_raw:
                with st.expander("Raw JSON (Plant Daily)"):
                    st.json(j_day)
                    download_button_for_json(j_day, "Download plant daily JSON", "plant_daily_kpi.json")
        except Exception as e:
            st.error(f"Plant daily error: {e}")
    with colPM:
        st.subheader("Plant – Monthly")
        try:
            j_mon = station_month_kpi([station_code], dt_ms)
            df_mon = normalize_safe(j_mon.get("data", []))
            st.dataframe(df_mon, use_container_width=True)
            if show_raw:
                with st.expander("Raw JSON (Plant Monthly)"):
                    st.json(j_mon)
                    download_button_for_json(j_mon, "Download plant monthly JSON", "plant_monthly_kpi.json")
        except Exception as e:
            st.error(f"Plant monthly error: {e}")
    with colPY:
        st.subheader("Plant – Yearly")
        try:
            j_yr = station_year_kpi([station_code], dt_ms)
            df_yr = normalize_safe(j_yr.get("data", []))
            st.dataframe(df_yr, use_container_width=True)
            if show_raw:
                with st.expander("Raw JSON (Plant Yearly)"):
                    st.json(j_yr)
                    download_button_for_json(j_yr, "Download plant yearly JSON", "plant_yearly_kpi.json")
        except Exception as e:
            st.error(f"Plant yearly error: {e}")

    st.markdown("---")
    st.caption("Device reports (select type)")
    try:
        df_devs2 = list_devices([station_code])
        type_choices = sorted(df_devs2["devTypeId"].dropna().unique().tolist()) if not df_devs2.empty else []
        dev_type2 = st.selectbox("Device type", options=type_choices,
                                 format_func=lambda t: f"{t} – {DEVICE_TYPES.get(int(t), 'Unknown')}") if type_choices else None
        if dev_type2 is not None:
            ids2 = df_devs2.loc[df_devs2["devTypeId"] == dev_type2, "devId"].astype(str).tolist()
            colDD, colDM, colDY = st.columns(3)
            with colDD:
                st.subheader("Devices – Daily")
                try:
                    dfd = device_day_kpi(int(dev_type2), ids2, dt_ms)
                    st.dataframe(dfd, use_container_width=True)
                except Exception as e:
                    st.error(f"Device daily error: {e}")
            with colDM:
                st.subheader("Devices – Monthly")
                try:
                    dfm = device_month_kpi(int(dev_type2), ids2, dt_ms)
                    st.dataframe(dfm, use_container_width=True)
                except Exception as e:
                    st.error(f"Device monthly error: {e}")
            with colDY:
                st.subheader("Devices – Yearly")
                try:
                    dfy = device_year_kpi(int(dev_type2), ids2, dt_ms)
                    st.dataframe(dfy, use_container_width=True)
                except Exception as e:
                    st.error(f"Device yearly error: {e}")
    except Exception as e:
        st.error(f"Device report error: {e}")

with tab4:
    st.caption("Export tables to CSV/Excel")
    st.write("Upload any API JSON (or copy/paste from expanders) to export:")
    up = st.file_uploader("Upload a JSON file (API response)", type=["json"], accept_multiple_files=False)
    if up is not None:
        try:
            # Try to load and normalize 'data' field if present
            blob = json.load(up)
            data = blob.get("data", []) if isinstance(blob, dict) else blob
            df_any = normalize_safe(data)
            st.dataframe(df_any, use_container_width=True)

            # CSV
            csv_bytes = df_any.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv_bytes, "fusion_export.csv", mime="text/csv")

            # Excel (optional, requires openpyxl)
            try:
                with pd.ExcelWriter("fusion_export.xlsx", engine="openpyxl") as xw:
                    df_any.to_excel(xw, index=False, sheet_name="export")
                with open("fusion_export.xlsx", "rb") as f:
                    st.download_button(
                        "Download Excel",
                        f.read(),
                        "fusion_export.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
            except Exception as exl:
                st.info(f"Excel export not available ({exl}). CSV is ready.")

        except Exception as e:
            st.error(f"Export error: {e}")

st.success("Loaded All-in-One Dashboard. Use tabs above to explore plants, devices, and reports.")
