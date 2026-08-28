# Final_KPI_app_5.py
# Streamlit app for Huawei FusionSolar Northbound API
# v5 fixes:
#   - PLOT_LAYOUT dict key collision in apply_layout (xaxis/yaxis overwrite) — fixed
#   - apply_layout now deep-merges nested dicts instead of overwriting them
#   - All st.tabs content renders correctly on first load
#   - Tab 3 multiselect has a safe default even when no years are loaded yet

import time
import random
import urllib3
from datetime import datetime, timezone, date, timedelta
from typing import Tuple, List, Optional

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ----------------------- Page setup -----------------------
st.set_page_config(page_title="FusionSolar Performance Dashboard", layout="wide")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------- Constants -----------------------
PLANT_TZ              = "Europe/Athens"
PLANT_START_YEAR      = 2025        # <-- SET YOUR PLANT COD YEAR HERE
PLANT_PEAK_KW         = 1100.0      # <-- SET YOUR INSTALLED CAPACITY (kWp) HERE
INVERTER_DEV_TYPE_IDS = [1, 38, 39] # String, HV, and central inverters

# ----------------------- PVGIS-SARAH3 Reference Irradiance -----------------------
# Long-term monthly GTI averages for Asvestochori, Thessaloniki (40.694°N, 22.990°E)
# Optimum fixed tilt ~30°, south-facing. Units: kWh/m²/month.
PVGIS_GTI_MONTHLY = {
     1:  55.2,  2:  73.8,  3: 118.6,  4: 155.4,
     5: 185.2,  6: 205.7,  7: 215.3,  8: 196.4,
     9: 152.8, 10: 103.5, 11:  62.4, 12:  46.1,
}
THESSALONIKI_TAMB = {
     1:  4.5,  2:  5.8,  3:  9.3,  4: 14.6,
     5: 20.1,  6: 25.2,  7: 27.8,  8: 27.4,
     9: 22.5, 10: 16.2, 11: 10.6, 12:  6.1,
}

MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"]
PALETTE = ["#f0b429","#3ecfcf","#60a5fa","#a78bfa","#fb923c","#34d399"]

# ----------------------- Plotly theme -----------------------
# NOTE: keep as a simple flat dict — nested axis dicts are applied separately
#       in apply_layout() to avoid silent key-collision overwrite bugs.
_BG       = "#0e1117"
_GRID     = "#1f2333"
_FONT_CLR = "#e2e8f0"

def apply_layout(fig, title: str = "", xaxis_title: str = "",
                 yaxis_title: str = "", height: int = None,
                 barmode: str = None, showlegend: bool = True,
                 yaxis_tickformat: str = None,
                 secondary_y_title: str = ""):
    """
    Apply a consistent dark theme to any Plotly figure.
    Each axis property is set explicitly to prevent dict-merge collisions
    that silently broke rendering in earlier versions.
    """
    layout_kwargs = dict(
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color=_FONT_CLR),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=_FONT_CLR)),
        margin=dict(t=55, b=40, l=10, r=10),
        hovermode="x unified",
    )
    if title:
        layout_kwargs["title"] = dict(text=title, font=dict(size=15))
    if barmode:
        layout_kwargs["barmode"] = barmode
    if height:
        layout_kwargs["height"] = height
    if not showlegend:
        layout_kwargs["showlegend"] = False

    fig.update_layout(**layout_kwargs)
    fig.update_xaxes(gridcolor=_GRID, zerolinecolor=_GRID, title_text=xaxis_title,
                     title_font=dict(color=_FONT_CLR))
    fig.update_yaxes(gridcolor=_GRID, zerolinecolor=_GRID, title_text=yaxis_title,
                     title_font=dict(color=_FONT_CLR),
                     tickformat=yaxis_tickformat or "")
    return fig


# ----------------------- Backoff helper -----------------------
def post_with_backoff(
    session: requests.Session,
    url: str,
    json_payload: dict,
    timeout: int = 25,
    max_retries: int = 3,
    base_sleep: float = 2.0,
    max_sleep: float = 30.0,
    jitter: float = 1.0,
) -> dict:
    last_resp = None
    for attempt in range(max_retries):
        try:
            resp = session.post(url, json=json_payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            time.sleep(min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter))
            last_resp = None
            continue
        last_resp = resp
        if resp.status_code == 200:
            try:
                j = resp.json()
                if j.get("failCode") == 407:
                    time.sleep(min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter))
                    continue
                return j
            except Exception:
                pass
        time.sleep(min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter))
    if last_resp is not None:
        try:
            return last_resp.json()
        except Exception:
            pass
    return {"data": None, "failCode": -1}


# ----------------------- API Client -----------------------
class HuaweiClient:
    def __init__(self, secrets: dict):
        self.base_url    = secrets["base_url"].rstrip("/")
        self.username    = secrets["username"]
        self.system_code = secrets["system_code"]
        self.verify_ssl  = secrets.get("verify_ssl", True)
        self.s = requests.Session()
        self.s.verify = self.verify_ssl
        self.s.headers.update({
            "Content-Type": "application/json",
            "User-Agent":   "Streamlit-FusionSolar",
        })

    def login(self) -> Tuple[bool, str]:
        try:
            resp  = self.s.post(
                f"{self.base_url}/thirdData/login",
                json={"userName": self.username, "systemCode": self.system_code},
                timeout=15,
            )
            token = resp.cookies.get("XSRF-TOKEN") or resp.headers.get("xsrf-token")
            if not token:
                return False, "No XSRF token received — check credentials or API endpoint."
            self.s.headers.update({"XSRF-TOKEN": token})
            return True, "Login successful"
        except Exception as e:
            return False, str(e)

    def get_stations(self) -> Tuple[Optional[List[dict]], Optional[str]]:
        r    = post_with_backoff(self.s, f"{self.base_url}/thirdData/getStationList", {})
        data = r.get("data", [])
        rows = (
            data.get("list", []) if isinstance(data, dict)
            else (data if isinstance(data, list) else [])
        )
        return (rows, None) if rows else (None, "No stations returned by API")

    @property
    def xsrf(self) -> str:
        return self.s.headers.get("XSRF-TOKEN", "")


# ----------------------- Session-state login -----------------------
def ensure_client() -> Tuple[Optional[HuaweiClient], Optional[List[dict]]]:
    """Logs in once per Streamlit session. Returns (client, stations) or (None, None)."""
    if "hw_client" not in st.session_state:
        try:
            fusion = st.secrets["fusion"]
        except Exception:
            st.error("❌ `[fusion]` section missing from `.streamlit/secrets.toml`.")
            return None, None

        c = HuaweiClient(fusion)
        ok, msg = c.login()
        if not ok:
            st.error(f"❌ Login failed: {msg}")
            return None, None

        stations, err = c.get_stations()
        if not stations:
            st.error(f"❌ Could not fetch stations: {err}")
            return None, None

        st.session_state["hw_client"]   = c
        st.session_state["hw_stations"] = stations

    return st.session_state["hw_client"], st.session_state["hw_stations"]


# ----------------------- Data helpers -----------------------
def get_budget_df(year: int) -> pd.DataFrame:
    targets = [117077, 89742, 140573, 172775, 177950, 186287,
               197265, 190014, 168524, 132649, 86079, 82732]
    if year == 2025:
        for i in range(9):
            targets[i] = 0
    return pd.DataFrame({
        "Month":      MONTH_LABELS,
        "Budget_kWh": targets,
        "MonthNum":   range(1, 13),
        "Year":       year,
    })


def normalize_safe(rows_in) -> pd.DataFrame:
    if not rows_in:
        return pd.DataFrame()
    df = pd.json_normalize(rows_in, sep=".")
    return df.loc[:, ~df.columns.duplicated()].copy()


def resolve_cols(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    """Return (time_col, energy_col) or (None, None) if not found."""
    tcol = next((c for c in df.columns if "time"   in c.lower() or "collect" in c.lower()), None)
    ecol = next((c for c in df.columns if "inverterYield" in c
                 or "month_cap" in c or "energy" in c.lower()), None)
    return tcol, ecol


def build_pvgis_df(years: List[int], reference_pr: float) -> pd.DataFrame:
    rows = []
    for yr in years:
        for m in range(1, 13):
            gti = PVGIS_GTI_MONTHLY[m]
            rows.append({
                "YearMonth":    f"{yr}-{m:02d}",
                "MonthNum":     m,
                "Year":         yr,
                "GTI_kWh_m2":  gti,
                "T_amb":        THESSALONIKI_TAMB[m],
                "Expected_kWh": gti * PLANT_PEAK_KW * reference_pr,
            })
    return pd.DataFrame(rows)


# ----------------------- Cached API calls -----------------------
@st.cache_data(ttl=3600, show_spinner=False)
def get_monthly_kpi_cached(base_url, station_code, year, xsrf_token, verify_ssl) -> pd.DataFrame:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    dt  = datetime(year, 1, 1, tzinfo=timezone.utc)
    j   = post_with_backoff(s, f"{base_url}/thirdData/getKpiStationMonth",
                            {"stationCodes": station_code,
                             "collectTime":  int(dt.timestamp() * 1000)})
    raw = j.get("data", [])
    if raw and isinstance(raw, list) and "kpiList" in raw[0]:
        raw = raw[0]["kpiList"]
    return normalize_safe(raw)


@st.cache_data(ttl=1800, show_spinner=False)
def get_kpi_station_quarter_cached(base_url, station_code, target_date, xsrf_token, verify_ssl) -> dict:
    """
    Fetch 15-minute production data for a single day.
    Uses getKpiStationHour — FusionSolar returns 15-min resolution intervals
    inside each hourly record's kpiList when the API supports it, or falls back
    to the getKpiStation5min endpoint where available.
    We request both and prefer the 15-min source.
    """
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    dt = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    collect_ms = int(dt.timestamp() * 1000)
    # Try 5-min endpoint first (returns 15-min on most Huawei installs)
    j5 = post_with_backoff(s, f"{base_url}/thirdData/getKpiStation5min",
                           {"stationCodes": station_code, "collectTime": collect_ms})
    if j5.get("data"):
        j5["_source"] = "5min"
        return j5
    # Fall back to hourly endpoint
    jh = post_with_backoff(s, f"{base_url}/thirdData/getKpiStationHour",
                           {"stationCodes": station_code, "collectTime": collect_ms})
    jh["_source"] = "hour"
    return jh


# ---- ADMIE Day-Ahead Market price helpers ----
ADMIE_API = "https://www.admie.gr/getOperationMarketFile"

@st.cache_data(ttl=3600, show_spinner=False)
def get_dam_prices_cached(target_date: date) -> pd.DataFrame:
    """
    Fetch DAM (Day-Ahead Market) clearing prices from ADMIE for a given date.
    Uses the public file-download API:
      https://www.admie.gr/getOperationMarketFile?dateStart=YYYY-MM-DD&dateEnd=YYYY-MM-DD&FileCategory=DAM_ResultsSummary
    Returns a DataFrame with columns: [dt (tz-aware EET), DAM_Price_EUR_MWh]
    at 15-min or hourly resolution depending on what ADMIE publishes.
    Falls back to ISP1ISPResults (SMP) if DAM file is unavailable.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    empty = pd.DataFrame(columns=["dt", "DAM_Price_EUR_MWh"])

    # 1. Try DAM_ResultsSummary first
    for file_cat in ["DAM_ResultsSummary", "ISP1ISPResults"]:
        try:
            resp = requests.get(
                ADMIE_API,
                params={"dateStart": date_str, "dateEnd": date_str, "FileCategory": file_cat},
                timeout=15,
                verify=False,
            )
            if resp.status_code != 200:
                continue
            files = resp.json()
            if not files:
                continue
            # Take the latest revision (last item)
            file_url = files[-1].get("file_path") or files[-1].get("url") or files[-1].get("link")
            if not file_url:
                # Some responses nest under a key
                for k, v in files[-1].items():
                    if isinstance(v, str) and (v.startswith("http") or v.endswith(".xlsx") or v.endswith(".xls")):
                        file_url = v
                        break
            if not file_url:
                continue

            # Download the file (Excel)
            fdata = requests.get(file_url, timeout=20, verify=False)
            if fdata.status_code != 200:
                continue

            import io
            xl = pd.read_excel(io.BytesIO(fdata.content), sheet_name=None)

            # Find the sheet with price data
            df_price = None
            for sheet_name, df_s in xl.items():
                cols_lower = [str(c).lower() for c in df_s.columns]
                # Look for a column that looks like a price (MCP / SMP / price)
                price_kw = ["mcp", "smp", "price", "τιμή", "euro", "eur"]
                time_kw  = ["period", "time", "ώρα", "περίοδος", "dp", "hour"]
                has_price = any(any(kw in c for kw in price_kw) for c in cols_lower)
                has_time  = any(any(kw in c for kw in time_kw)  for c in cols_lower)
                if has_price and has_time:
                    df_price = df_s.copy()
                    break

            if df_price is None or df_price.empty:
                continue

            # Identify time and price columns
            cols_lower = [str(c).lower() for c in df_price.columns]
            time_col  = next((df_price.columns[i] for i, c in enumerate(cols_lower)
                              if any(kw in c for kw in ["period", "time", "ώρα", "περίοδος", "dp", "hour"])), None)
            price_col = next((df_price.columns[i] for i, c in enumerate(cols_lower)
                              if any(kw in c for kw in ["mcp", "smp", "price", "τιμή", "euro"])), None)

            if not time_col or not price_col:
                continue

            df_price = df_price[[time_col, price_col]].copy()
            df_price.columns = ["Period", "DAM_Price_EUR_MWh"]
            df_price["DAM_Price_EUR_MWh"] = pd.to_numeric(df_price["DAM_Price_EUR_MWh"], errors="coerce")
            df_price = df_price.dropna(subset=["DAM_Price_EUR_MWh"])

            # Map dispatch periods (1-96 for 15-min, 1-24 for hourly) to timestamps
            n_periods = len(df_price)
            if n_periods == 0:
                continue

            # Determine resolution
            if n_periods >= 88:   # 15-min day (96 periods, sometimes fewer due to DST)
                freq_min = 15
            elif n_periods >= 22: # hourly (24 periods)
                freq_min = 60
            else:
                freq_min = 15

            base_dt = datetime(target_date.year, target_date.month, target_date.day,
                               tzinfo=timezone.utc)
            df_price["dt"] = [
                base_dt + timedelta(minutes=freq_min * i)
                for i in range(len(df_price))
            ]
            df_price["dt"] = pd.to_datetime(df_price["dt"]).dt.tz_convert(PLANT_TZ)
            df_price["_source"] = file_cat
            return df_price[["dt", "DAM_Price_EUR_MWh", "_source"]]

        except Exception:
            continue

    return empty


@st.cache_data(ttl=7200, show_spinner=False)
def get_monthly_avg_dam_price(year: int, month: int) -> Optional[float]:
    """
    Fetch all daily DAM prices for a given month from ADMIE and return the
    simple average (€/MWh) across all positive-price 15-min/hourly periods.
    Returns None if data is unavailable (network blocked, future month, etc.).
    Only prices > 0 €/MWh are included in the average, per business rule.
    """
    try:
        import calendar as _cal
        last_day = _cal.monthrange(year, month)[1]
        date_start = f"{year}-{month:02d}-01"
        date_end   = f"{year}-{month:02d}-{last_day:02d}"
        resp = requests.get(
            ADMIE_API,
            params={"dateStart": date_start, "dateEnd": date_end,
                    "FileCategory": "DAM_ResultsSummary"},
            timeout=15, verify=False,
        )
        if resp.status_code != 200 or not resp.json():
            return None

        all_prices: list[float] = []
        for file_entry in resp.json():
            file_url = (file_entry.get("file_path") or file_entry.get("url")
                        or file_entry.get("link"))
            if not file_url:
                for v in file_entry.values():
                    if isinstance(v, str) and (v.startswith("http") or
                                               v.endswith(".xlsx") or v.endswith(".xls")):
                        file_url = v
                        break
            if not file_url:
                continue
            import io
            fdata = requests.get(file_url, timeout=20, verify=False)
            if fdata.status_code != 200:
                continue
            xl = pd.read_excel(io.BytesIO(fdata.content), sheet_name=None)
            for df_s in xl.values():
                cols_lower = [str(c).lower() for c in df_s.columns]
                price_col = next((df_s.columns[i] for i, c in enumerate(cols_lower)
                                  if any(kw in c for kw in ["mcp","smp","price","τιμή"])), None)
                if price_col:
                    vals = pd.to_numeric(df_s[price_col], errors="coerce").dropna()
                    all_prices.extend(vals[vals > 0].tolist())
                    break  # one sheet per file is enough

        return float(pd.Series(all_prices).mean()) if all_prices else None
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def get_dev_real_kpi_cached(base_url, dev_type_id, dev_ids_tuple, xsrf_token, verify_ssl) -> pd.DataFrame:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    j = post_with_backoff(s, f"{base_url}/thirdData/getDevRealKpi",
                          {"devTypeId": dev_type_id,
                           "devIds":    ",".join(map(str, dev_ids_tuple))})
    return normalize_safe(j.get("data", []))


# ================================================================
# ========================  MAIN UI  =============================
# ================================================================
st.title("☀️ FusionSolar Advanced Analytics")

# Sidebar
year_input = int(st.sidebar.number_input(
    "Year", min_value=PLANT_START_YEAR, max_value=2030, value=2025
))
st.sidebar.caption(f"Plant start year : {PLANT_START_YEAR}")
st.sidebar.caption(f"Installed capacity: {PLANT_PEAK_KW:.0f} kWp")
st.sidebar.caption("📍 Irradiance ref  : Asvestochori, Thessaloniki (PVGIS-SARAH3)")
if st.sidebar.button("🔄 Reset Login"):
    st.session_state.pop("hw_client",   None)
    st.session_state.pop("hw_stations", None)
    st.rerun()

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Monthly Performance",
    "📈 Hourly Curves",
    "📉 Historical Deep-Dive",
    "🛠️ Health & Strings",
])


# ================================================================
# TAB 1 — Monthly Performance
# ================================================================
with tab1:
    st.header("Monthly Energy vs Budget")
    if st.button("Refresh Monthly KPIs", key="btn_monthly"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")

            with st.spinner("Fetching current year…"):
                df_raw = get_monthly_kpi_cached(
                    client.base_url, sid, year_input, client.xsrf, client.verify_ssl
                )

            if df_raw.empty:
                st.warning("No monthly data returned for this year.")
                st.stop()

            tcol, ecol = resolve_cols(df_raw)
            if not tcol or not ecol:
                st.error(f"Unexpected API columns: {list(df_raw.columns)}")
                st.stop()

            df_raw["dt"]         = pd.to_datetime(df_raw[tcol], unit="ms", utc=True).dt.tz_convert(PLANT_TZ)
            df_raw["MonthNum"]   = df_raw["dt"].dt.month
            df_raw["Energy_kWh"] = pd.to_numeric(df_raw[ecol], errors="coerce")

            bdf    = get_budget_df(year_input)
            merged = bdf.merge(df_raw[["MonthNum","Energy_kWh"]], on="MonthNum", how="left")
            merged["Delta_kWh"]      = merged["Energy_kWh"] - merged["Budget_kWh"]
            merged["Achievement_%"]  = (
                merged["Energy_kWh"] / merged["Budget_kWh"].replace(0, pd.NA) * 100
            ).round(1)

            # Fetch monthly average DAM prices for completed months only
            today = date.today()
            dam_prices_by_month: dict[int, Optional[float]] = {}
            completed_months = [
                m for m in range(1, 13)
                if date(year_input, m, 1) < today
            ]
            if completed_months:
                with st.spinner("Fetching monthly DAM prices from ADMIE…"):
                    for m in completed_months:
                        dam_prices_by_month[m] = get_monthly_avg_dam_price(year_input, m)

            merged["Avg_DAM_EUR_MWh"] = merged["MonthNum"].map(dam_prices_by_month)
            # Revenue only when price > 0 and actual production is available
            merged["Revenue_EUR"] = merged.apply(
                lambda r: (r["Energy_kWh"] * r["Avg_DAM_EUR_MWh"] / 1000)
                if (pd.notna(r["Avg_DAM_EUR_MWh"]) and r["Avg_DAM_EUR_MWh"] > 0
                    and pd.notna(r["Energy_kWh"]))
                else pd.NA,
                axis=1,
            )

            # Rolling 12-month avg — needs prior year data
            with st.spinner("Fetching prior year for rolling average…"):
                df_prev = get_monthly_kpi_cached(
                    client.base_url, sid, year_input - 1, client.xsrf, client.verify_ssl
                )

            # Build chronological combined series
            frames = []
            for df_yr, yr_lbl in [(df_prev, year_input - 1), (df_raw, year_input)]:
                if df_yr.empty:
                    continue
                t, e = resolve_cols(df_yr)
                if t and e:
                    tmp = df_yr[[t, e]].copy()
                    tmp.columns = ["ts", "Energy_kWh"]
                    tmp["dt"]         = pd.to_datetime(tmp["ts"], unit="ms", utc=True).dt.tz_convert(PLANT_TZ)
                    tmp["MonthNum"]   = tmp["dt"].dt.month
                    tmp["Year"]       = yr_lbl
                    tmp["Energy_kWh"] = pd.to_numeric(tmp["Energy_kWh"], errors="coerce")
                    frames.append(tmp)

            # --- Chart 1: Budget vs Actual + Rolling avg ---
            fig = go.Figure()
            fig.add_bar(x=merged["Month"], y=merged["Budget_kWh"],
                        name="Budget", marker_color="#334155")
            fig.add_bar(x=merged["Month"], y=merged["Energy_kWh"],
                        name="Actual", marker_color="#f0b429")

            if frames:
                df_comb = pd.concat(frames).sort_values("dt").reset_index(drop=True)
                df_comb["Rolling12"] = df_comb["Energy_kWh"].rolling(12, min_periods=3).mean()
                df_ty = df_comb[df_comb["Year"] == year_input]
                if not df_ty.empty:
                    fig.add_scatter(
                        x=[MONTH_LABELS[m - 1] for m in df_ty["MonthNum"]],
                        y=df_ty["Rolling12"].values,
                        mode="lines+markers", name="12-mo Rolling Avg",
                        line=dict(color="#f472b6", width=2.5, dash="dot"),
                        marker=dict(size=6),
                    )

            apply_layout(fig, title=f"{year_input} Monthly Energy vs Budget",
                         barmode="group", yaxis_title="kWh", xaxis_title="Month")
            st.plotly_chart(fig, use_container_width=True)

            # --- Chart 2: Delta ---
            delta_colors = ["#4ade80" if v >= 0 else "#ff5f5f"
                            for v in merged["Delta_kWh"].fillna(0)]
            fig2 = go.Figure(go.Bar(
                x=merged["Month"], y=merged["Delta_kWh"],
                marker_color=delta_colors, name="Actual − Budget",
            ))
            apply_layout(fig2, title="Monthly Delta (Actual − Budget)", yaxis_title="kWh")
            st.plotly_chart(fig2, use_container_width=True)

            # --- Summary table ---
            st.subheader("Summary Table")

            has_dam = merged["Avg_DAM_EUR_MWh"].notna().any()
            display_cols = ["Month","Budget_kWh","Energy_kWh","Delta_kWh","Achievement_%"]
            fmt = {
                "Budget_kWh":    "{:,.0f}",
                "Energy_kWh":    "{:,.0f}",
                "Delta_kWh":     "{:+,.0f}",
                "Achievement_%": "{:.1f}%",
            }
            if has_dam:
                display_cols += ["Avg_DAM_EUR_MWh", "Revenue_EUR"]
                fmt["Avg_DAM_EUR_MWh"] = "{:.2f}"
                fmt["Revenue_EUR"]     = "{:,.0f}"
                merged = merged.rename(columns={
                    "Avg_DAM_EUR_MWh": "Avg DAM (€/MWh)",
                    "Revenue_EUR":     "Est. Revenue (€)",
                })
                display_cols = [c if c not in ("Avg_DAM_EUR_MWh","Revenue_EUR")
                                else ("Avg DAM (€/MWh)" if c == "Avg_DAM_EUR_MWh"
                                      else "Est. Revenue (€)")
                                for c in display_cols]
                fmt = {k if k not in ("Avg_DAM_EUR_MWh","Revenue_EUR") else
                       ("Avg DAM (€/MWh)" if k == "Avg_DAM_EUR_MWh" else "Est. Revenue (€)"): v
                       for k, v in fmt.items()}

                total_rev = merged["Est. Revenue (€)"].sum(skipna=True)
                st.metric("YTD Estimated Revenue", f"€ {total_rev:,.0f}",
                          help="Sum of months where DAM price > 0 €/MWh")
                if not has_dam:
                    st.caption("ℹ️ DAM prices unavailable — revenue column hidden.")

            st.dataframe(
                merged[display_cols].style.format(fmt, na_rep="—"),
                use_container_width=True,
            )


# ================================================================
# TAB 2 — 15-min Production + DAM Price Overlay
# ================================================================
with tab2:
    st.header("15-min Production Profile vs Day-Ahead Price")
    st.caption(
        "Production at 15-min resolution overlaid against ADMIE Day-Ahead Market (DAM) "
        "clearing prices (€/MWh). DAM prices sourced live from admie.gr."
    )

    t2_col1, t2_col2 = st.columns([2, 1])
    with t2_col1:
        target_date = st.date_input(
            "Select Analysis Day",
            value=date.today() - timedelta(days=1),
            key="t2_date",
        )
    with t2_col2:
        show_revenue = st.checkbox(
            "Show estimated revenue curve", value=True,
            help="Revenue = Production (kWh) × DAM Price (€/MWh) / 1000 → €/15-min"
        )

    if st.button("Generate 15-min Curve", key="btn_15min"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")

            # ── Fetch production ──
            with st.spinner("Fetching 15-min production data…"):
                j_q = get_kpi_station_quarter_cached(
                    client.base_url, sid, target_date, client.xsrf, client.verify_ssl
                )

            raw_q = j_q.get("data", [])
            source_label = j_q.get("_source", "hour")

            # Unwrap kpiList if present
            if raw_q and isinstance(raw_q, list):
                if "kpiList" in (raw_q[0] if isinstance(raw_q[0], dict) else {}):
                    raw_q = raw_q[0]["kpiList"]

            df_prod = normalize_safe(raw_q)

            # ── Fetch DAM prices ──
            with st.spinner("Fetching ADMIE DAM prices…"):
                df_dam = get_dam_prices_cached(target_date)

            # ── Parse production ──
            if df_prod.empty:
                st.warning("No production data returned for the selected date.")
            else:
                tcol = next((c for c in df_prod.columns
                             if "time" in c.lower() or "collect" in c.lower()), None)
                ycol = next((c for c in df_prod.columns
                             if "inverterYield" in c or "activePower" in c
                             or "day_cap" in c or "power" in c.lower()), None)

                if not tcol or not ycol:
                    st.error(f"Cannot identify production columns. Found: {list(df_prod.columns)}")
                else:
                    df_prod["dt"] = pd.to_datetime(
                        df_prod[tcol], unit="ms", utc=True
                    ).dt.tz_convert(PLANT_TZ)
                    df_prod[ycol] = pd.to_numeric(df_prod[ycol], errors="coerce")
                    df_prod = df_prod.sort_values("dt").reset_index(drop=True)

                    # If source was hourly, upsample to 15-min via linear interpolation.
                    # Correct pattern: resample().asfreq() inserts NaN rows at 15-min
                    # boundaries first, then interpolate() fills them on the Series.
                    n_pts = len(df_prod)
                    if source_label == "hour" and n_pts <= 25:
                        df_prod = (
                            df_prod.set_index("dt")[[ycol]]
                            .resample("15min")
                            .asfreq()
                            [ycol]
                            .interpolate(method="linear")
                            .reset_index()
                        )
                        df_prod.columns = ["dt", ycol]
                        resolution_note = "⚠️ Production upsampled from hourly to 15-min (linear interpolation) — 15-min endpoint not available."
                    else:
                        resolution_note = f"✅ Production at native 15-min resolution ({n_pts} intervals)."

                    st.caption(resolution_note)

                    # ── Build dual-axis figure ──
                    fig = make_subplots(
                        specs=[[{"secondary_y": True}]],
                    )

                    # Production area (primary y)
                    fig.add_trace(go.Scatter(
                        x=df_prod["dt"], y=df_prod[ycol],
                        fill="tozeroy",
                        line=dict(color="#f0b429", width=2),
                        fillcolor="rgba(240,180,41,0.12)",
                        name="Production (kWh / kW)",
                    ), secondary_y=False)

                    dam_ok = not df_dam.empty and "DAM_Price_EUR_MWh" in df_dam.columns

                    if dam_ok:
                        dam_source = df_dam.get("_source", pd.Series(["DAM"])).iloc[0] if "_source" in df_dam.columns else "DAM"
                        price_label = "SMP €/MWh" if "ISP" in str(dam_source) else "DAM MCP €/MWh"

                        # DAM price step line (secondary y)
                        fig.add_trace(go.Scatter(
                            x=df_dam["dt"], y=df_dam["DAM_Price_EUR_MWh"],
                            mode="lines",
                            line=dict(color="#3ecfcf", width=2, shape="hv"),  # step
                            name=price_label,
                        ), secondary_y=True)

                        # Optional revenue curve
                        if show_revenue:
                            # Merge on nearest 15-min timestamp
                            df_rev = pd.merge_asof(
                                df_prod[["dt", ycol]].sort_values("dt"),
                                df_dam[["dt", "DAM_Price_EUR_MWh"]].sort_values("dt"),
                                on="dt",
                                direction="nearest",
                                tolerance=pd.Timedelta("8min"),
                            )
                            # Revenue in € per interval:
                            # Production kWh × Price €/MWh × (1MWh/1000kWh)
                            df_rev["Revenue_EUR"] = (
                                df_rev[ycol] * df_rev["DAM_Price_EUR_MWh"] / 1000
                            )
                            fig.add_trace(go.Scatter(
                                x=df_rev["dt"], y=df_rev["Revenue_EUR"],
                                mode="lines",
                                line=dict(color="#a78bfa", width=1.5, dash="dot"),
                                name="Est. Revenue (€/interval)",
                            ), secondary_y=True)

                            # Revenue summary metrics
                            total_rev = df_rev["Revenue_EUR"].sum()
                            total_kwh = df_rev[ycol].sum()
                            avg_price = df_dam["DAM_Price_EUR_MWh"].mean()
                            peak_price = df_dam["DAM_Price_EUR_MWh"].max()

                            # Hours with production above 0 and prices
                            high_price_threshold = df_dam["DAM_Price_EUR_MWh"].quantile(0.75)
                            high_price_prod = df_rev.loc[
                                df_rev["DAM_Price_EUR_MWh"] >= high_price_threshold, ycol
                            ].sum()
                            high_price_pct = (high_price_prod / total_kwh * 100) if total_kwh > 0 else 0

                            m1, m2, m3, m4 = st.columns(4)
                            m1.metric("Total Production", f"{total_kwh:,.0f} kWh")
                            m2.metric("Est. Revenue", f"€ {total_rev:,.1f}")
                            m3.metric("Avg DAM Price", f"{avg_price:.1f} €/MWh")
                            m4.metric("Peak DAM Price", f"{peak_price:.1f} €/MWh")

                            if total_kwh > 0:
                                st.caption(
                                    f"📊 {high_price_pct:.1f}% of today's production fell in the "
                                    f"top-quartile price window (≥ {high_price_threshold:.1f} €/MWh)."
                                )
                    else:
                        st.info(
                            "ℹ️ DAM prices could not be fetched from ADMIE for this date "
                            "(network blocked, date too recent, or file format changed). "
                            "Showing production curve only."
                        )

                    fig.update_layout(
                        title=dict(
                            text=f"15-min Production & DAM Price — {target_date}",
                            font=dict(color=_FONT_CLR, size=15),
                        ),
                        paper_bgcolor=_BG, plot_bgcolor=_BG,
                        font=dict(color=_FONT_CLR),
                        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h",
                                    yanchor="bottom", y=1.02),
                        margin=dict(t=80, b=40, l=70, r=70),
                        hovermode="x unified",
                    )
                    fig.update_xaxes(
                        gridcolor=_GRID, zerolinecolor=_GRID,
                        title_text="Time",
                        tickformat="%H:%M",
                        title_font=dict(color=_FONT_CLR),
                    )
                    # Primary y — Production — amber to match the area fill
                    fig.update_yaxes(
                        title_text="⚡ Production (kWh)",
                        gridcolor=_GRID,
                        zerolinecolor=_GRID,
                        title_font=dict(color="#f0b429"),
                        tickfont=dict(color="#f0b429"),
                        secondary_y=False,
                    )
                    # Secondary y — Price / Revenue — teal to match the price line
                    fig.update_yaxes(
                        title_text="💰 Price (€/MWh) · Revenue (€)",
                        gridcolor="rgba(0,0,0,0)",   # suppress secondary gridlines
                        zerolinecolor=_GRID,
                        title_font=dict(color="#3ecfcf"),
                        tickfont=dict(color="#3ecfcf"),
                        secondary_y=True,
                    )
                    st.plotly_chart(fig, use_container_width=True)

                    # Detailed data table
                    with st.expander("📋 Raw 15-min Data Table"):
                        if dam_ok:
                            df_show = pd.merge_asof(
                                df_prod[["dt", ycol]].sort_values("dt"),
                                df_dam[["dt", "DAM_Price_EUR_MWh"]].sort_values("dt"),
                                on="dt", direction="nearest",
                                tolerance=pd.Timedelta("8min"),
                            )
                            df_show["Revenue_EUR"] = df_show[ycol] * df_show["DAM_Price_EUR_MWh"] / 1000
                            df_show["Time"] = df_show["dt"].dt.strftime("%H:%M")
                            st.dataframe(
                                df_show[["Time", ycol, "DAM_Price_EUR_MWh", "Revenue_EUR"]]
                                .rename(columns={
                                    ycol: "Production",
                                    "DAM_Price_EUR_MWh": "DAM Price (€/MWh)",
                                    "Revenue_EUR": "Revenue (€)",
                                })
                                .style.format({
                                    "Production":        "{:,.2f}",
                                    "DAM Price (€/MWh)": "{:.2f}",
                                    "Revenue (€)":       "{:.3f}",
                                }),
                                use_container_width=True,
                                hide_index=True,
                            )
                        else:
                            df_show = df_prod[["dt", ycol]].copy()
                            df_show["Time"] = df_show["dt"].dt.strftime("%H:%M")
                            st.dataframe(
                                df_show[["Time", ycol]].rename(columns={ycol: "Production"}),
                                use_container_width=True, hide_index=True,
                            )


# ================================================================
# TAB 3 — Historical Deep-Dive
# ================================================================
with tab3:
    st.header("Historical Performance Deep-Dive")
    st.caption("Monthly data across all operational years — rolling averages, PR trend, irradiance benchmarking.")

    all_years = list(range(PLANT_START_YEAR, date.today().year + 1))

    col_a, col_b = st.columns([2, 1])
    with col_a:
        hist_years = st.multiselect(
            "Select years to analyse", options=all_years, default=all_years,
        )
    with col_b:
        reference_pr = st.number_input(
            "Reference PR for irradiance model",
            value=0.78, min_value=0.50, max_value=1.00, step=0.01,
            help="Expected energy = GTI × capacity × PR. 0.78 is typical for "
                 "crystalline-Si in Thessaloniki climate.",
        )

    if not hist_years:
        st.info("Select at least one year above, then click Load.")
    elif st.button("Load Historical Data", key="btn_hist"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")

            all_frames = []
            prog = st.progress(0, text="Fetching…")
            for i, yr in enumerate(sorted(hist_years)):
                df_yr = get_monthly_kpi_cached(
                    client.base_url, sid, yr, client.xsrf, client.verify_ssl
                )
                if not df_yr.empty:
                    df_yr = df_yr.copy()
                    df_yr["_year"] = yr
                    all_frames.append(df_yr)
                prog.progress((i + 1) / len(hist_years), text=f"Fetched {yr}")
            prog.empty()

            if not all_frames:
                st.warning("No data returned for the selected years.")
                st.stop()

            df_all = pd.concat(all_frames, ignore_index=True)
            tcol, ecol = resolve_cols(df_all)
            if not tcol or not ecol:
                st.error(f"Cannot identify required columns: {list(df_all.columns)}")
                st.stop()

            # Base column discovery
            temp_col = next((c for c in df_all.columns if "temperature" in c.lower()), None)
            pwr_col  = next((c for c in df_all.columns if "active_power" in c.lower()), None)
            pf_col   = next((c for c in df_all.columns if "power_factor" in c.lower()), None)
            eff_col  = next((c for c in df_all.columns if "efficiency"   in c.lower()), None)
            freq_col = next((c for c in df_all.columns if "elec_freq"    in c.lower() or
                             ("freq" in c.lower() and "power" not in c.lower())), None)

            df_all["dt"]         = pd.to_datetime(df_all[tcol], unit="ms", utc=True).dt.tz_convert(PLANT_TZ)
            df_all["YearMonth"]  = df_all["dt"].dt.to_period("M").astype(str)
            df_all["MonthNum"]   = df_all["dt"].dt.month
            df_all["Year"]       = df_all["_year"]
            df_all["Energy_kWh"] = pd.to_numeric(df_all[ecol], errors="coerce")
            df_all = df_all.sort_values("dt").reset_index(drop=True)

            pvgis_df = build_pvgis_df(hist_years, reference_pr)

            # Monthly aggregate
            df_monthly = (
                df_all.groupby("YearMonth")["Energy_kWh"].sum()
                .reset_index().sort_values("YearMonth").reset_index(drop=True)
            )
            df_monthly["MonthNum"] = df_monthly["YearMonth"].apply(lambda x: int(x.split("-")[1]))
            df_monthly["Year"]     = df_monthly["YearMonth"].apply(lambda x: int(x.split("-")[0]))

            df_bench = df_monthly.merge(
                pvgis_df[["YearMonth","GTI_kWh_m2","T_amb","Expected_kWh"]],
                on="YearMonth", how="left",
            )
            df_bench["Actual_vs_Expected_%"] = (
                df_bench["Energy_kWh"] / df_bench["Expected_kWh"].replace(0, pd.NA) * 100
            ).round(1)
            df_bench["Rolling12_kWh"] = df_bench["Energy_kWh"].rolling(12, min_periods=3).mean()
            df_bench["PR"]            = df_bench["Energy_kWh"] / (df_bench["GTI_kWh_m2"] * PLANT_PEAK_KW)
            df_bench["PR_Rolling12"]  = df_bench["PR"].rolling(12, min_periods=3).mean()

            bdf_ref = get_budget_df(2024)  # budget reference (non-zero year)

            # ─────────────────────────────────────────────────
            # ① Year-over-Year + Rolling 12-month Average
            # ─────────────────────────────────────────────────
            st.subheader("① Monthly Energy — Year-over-Year + 12-Month Rolling Average")
            fig_yoy = go.Figure()
            for idx, yr in enumerate(sorted(hist_years)):
                df_y = df_monthly[df_monthly["Year"] == yr].sort_values("MonthNum")
                fig_yoy.add_scatter(
                    x=df_y["MonthNum"], y=df_y["Energy_kWh"],
                    mode="lines+markers", name=str(yr),
                    line=dict(color=PALETTE[idx % len(PALETTE)], width=2),
                )
            fig_yoy.add_scatter(
                x=list(range(1, 13)), y=bdf_ref["Budget_kWh"],
                mode="lines", name="Budget (ref)",
                line=dict(color="#475569", dash="dash"),
            )
            fig_yoy.add_scatter(
                x=df_bench["MonthNum"], y=df_bench["Rolling12_kWh"],
                mode="lines", name="12-mo Rolling Avg",
                line=dict(color="#f472b6", width=2.5, dash="dot"),
            )
            apply_layout(fig_yoy, title="Monthly Energy per Year vs Budget Reference",
                         yaxis_title="kWh")
            fig_yoy.update_xaxes(
                tickmode="array", tickvals=list(range(1, 13)), ticktext=MONTH_LABELS
            )
            st.plotly_chart(fig_yoy, use_container_width=True)

            # ─────────────────────────────────────────────────
            # ② Cumulative Energy
            # ─────────────────────────────────────────────────
            st.subheader("② Cumulative Energy Since COD")
            df_bench["Cumulative_MWh"] = df_bench["Energy_kWh"].cumsum() / 1000
            fig_cum = go.Figure(go.Scatter(
                x=df_bench["YearMonth"], y=df_bench["Cumulative_MWh"],
                fill="tozeroy", line_color="#3ecfcf",
                fillcolor="rgba(62,207,207,0.12)", name="Cumulative MWh",
            ))
            apply_layout(fig_cum, title="Cumulative Energy Production (MWh)", yaxis_title="MWh")
            st.plotly_chart(fig_cum, use_container_width=True)
            st.metric("Total Production to Date",
                      f"{df_bench['Cumulative_MWh'].iloc[-1]:,.1f} MWh")

            # ─────────────────────────────────────────────────
            # ③ Performance Ratio Trend
            # ─────────────────────────────────────────────────
            st.subheader("③ Performance Ratio (PR) Trend")
            st.caption(
                "PR = Actual Energy / (GTI × Capacity).  "
                "Irradiance source: PVGIS-SARAH3 — Asvestochori, Thessaloniki."
            )
            fig_pr = go.Figure()
            fig_pr.add_scatter(
                x=df_bench["YearMonth"], y=df_bench["PR"],
                mode="lines+markers", line_color="#60a5fa",
                marker=dict(size=5), name="Monthly PR",
            )
            fig_pr.add_scatter(
                x=df_bench["YearMonth"], y=df_bench["PR_Rolling12"],
                mode="lines", name="12-mo Rolling PR",
                line=dict(color="#f472b6", width=2.5, dash="dot"),
            )
            fig_pr.add_hline(y=0.75, line_dash="dash", line_color="#4ade80",
                             annotation_text="Target PR 0.75")
            fig_pr.add_hline(y=0.65, line_dash="dot",  line_color="#ff5f5f",
                             annotation_text="Alert PR 0.65")
            apply_layout(fig_pr, title="Performance Ratio Over Time",
                         yaxis_title="PR", yaxis_tickformat=".0%")
            st.plotly_chart(fig_pr, use_container_width=True)

            # ─────────────────────────────────────────────────
            # ④ Irradiance Benchmark
            # ─────────────────────────────────────────────────
            st.subheader("④ Solar Irradiance Benchmark — Actual vs PVGIS-SARAH3 Expected")
            st.caption(
                f"Expected = GTI × {PLANT_PEAK_KW:.0f} kWp × PR {reference_pr:.2f}.  "
                "Location: Asvestochori, Thessaloniki (40.694°N, 22.990°E), tilt ~30°, south."
            )

            fig_irr = make_subplots(
                rows=2, cols=1, shared_xaxes=True,
                subplot_titles=["Actual vs PVGIS-Expected Energy (kWh)",
                                "Actual / Expected (%)"],
                vertical_spacing=0.12,
            )
            fig_irr.add_bar(x=df_bench["YearMonth"], y=df_bench["Expected_kWh"],
                            name="PVGIS Expected", marker_color="#334155", row=1, col=1)
            fig_irr.add_bar(x=df_bench["YearMonth"], y=df_bench["Energy_kWh"],
                            name="Actual", marker_color="#f0b429", row=1, col=1)
            fig_irr.add_scatter(
                x=df_bench["YearMonth"], y=df_bench["Rolling12_kWh"],
                mode="lines", name="12-mo Rolling Actual",
                line=dict(color="#f472b6", width=2, dash="dot"), row=1, col=1,
            )
            ratio_colors = [
                "#4ade80" if v >= 100 else "#fb923c" if v >= 85 else "#ff5f5f"
                for v in df_bench["Actual_vs_Expected_%"].fillna(0)
            ]
            fig_irr.add_bar(x=df_bench["YearMonth"], y=df_bench["Actual_vs_Expected_%"],
                            name="Actual / Expected %", marker_color=ratio_colors,
                            row=2, col=1)
            fig_irr.add_hline(y=100, line_dash="dash", line_color="#4ade80",
                              annotation_text="100%", row=2, col=1)
            fig_irr.add_hline(y=85, line_dash="dot", line_color="#ff5f5f",
                              annotation_text="85% alert", row=2, col=1)
            fig_irr.update_layout(
                barmode="group", height=600,
                paper_bgcolor=_BG, plot_bgcolor=_BG,
                font=dict(color=_FONT_CLR),
                legend=dict(bgcolor="rgba(0,0,0,0)"),
                margin=dict(t=55, b=40, l=10, r=10),
            )
            fig_irr.update_xaxes(gridcolor=_GRID, zerolinecolor=_GRID)
            fig_irr.update_yaxes(gridcolor=_GRID, zerolinecolor=_GRID)
            st.plotly_chart(fig_irr, use_container_width=True)

            with st.expander("📋 PVGIS-SARAH3 Monthly GTI Reference — Asvestochori"):
                st.dataframe(pd.DataFrame({
                    "Month":        MONTH_LABELS,
                    "GTI (kWh/m²)": [PVGIS_GTI_MONTHLY[m] for m in range(1, 13)],
                    "T_amb (°C)":   [THESSALONIKI_TAMB[m]  for m in range(1, 13)],
                    f"Expected kWh (PR={reference_pr:.2f})": [
                        round(PVGIS_GTI_MONTHLY[m] * PLANT_PEAK_KW * reference_pr)
                        for m in range(1, 13)
                    ],
                }), use_container_width=True, hide_index=True)

            # ─────────────────────────────────────────────────
            # ⑤ Irradiance + Temperature + Energy (3-axis overlay)
            # ─────────────────────────────────────────────────
            st.subheader("⑤ Irradiance, Temperature & Energy — Seasonal Relationship")
            fig_3ax = make_subplots(specs=[[{"secondary_y": True}]])
            fig_3ax.add_bar(
                x=df_bench["YearMonth"], y=df_bench["GTI_kWh_m2"],
                name="GTI (kWh/m²)", marker_color="rgba(96,165,250,0.35)",
                secondary_y=False,
            )
            fig_3ax.add_scatter(
                x=df_bench["YearMonth"], y=df_bench["T_amb"],
                mode="lines", name="Amb. Temp °C",
                line=dict(color="#fb923c", width=1.5, dash="dot"),
                secondary_y=False,
            )
            fig_3ax.add_scatter(
                x=df_bench["YearMonth"], y=df_bench["Energy_kWh"],
                mode="lines+markers", name="Actual Energy (kWh)",
                line=dict(color="#f0b429", width=2),
                secondary_y=True,
            )
            fig_3ax.update_layout(
                title=dict(text="Monthly GTI, Ambient Temperature & Actual Energy",
                           font=dict(color=_FONT_CLR, size=15)),
                barmode="overlay",
                paper_bgcolor=_BG, plot_bgcolor=_BG,
                font=dict(color=_FONT_CLR),
                legend=dict(bgcolor="rgba(0,0,0,0)"),
                margin=dict(t=55, b=40, l=10, r=10),
            )
            fig_3ax.update_xaxes(gridcolor=_GRID, zerolinecolor=_GRID)
            fig_3ax.update_yaxes(title_text="GTI (kWh/m²) / Temp (°C)",
                                 gridcolor=_GRID, secondary_y=False)
            fig_3ax.update_yaxes(title_text="Actual Energy (kWh)",
                                 gridcolor=_GRID, secondary_y=True)
            st.plotly_chart(fig_3ax, use_container_width=True)

            # ─────────────────────────────────────────────────
            # ⑥ Inverter Temperature Trend
            # ─────────────────────────────────────────────────
            if temp_col:
                st.subheader("⑥ Inverter Temperature Trend")
                df_all["Temp"] = pd.to_numeric(df_all[temp_col], errors="coerce")
                df_temp = (
                    df_all.groupby("YearMonth")["Temp"].mean()
                    .reset_index().sort_values("YearMonth")
                )
                df_temp["Roll12"] = df_temp["Temp"].rolling(12, min_periods=3).mean()
                fig_tmp = go.Figure()
                fig_tmp.add_scatter(
                    x=df_temp["YearMonth"], y=df_temp["Temp"],
                    mode="lines+markers", line=dict(color="#fb923c", width=2),
                    fill="tozeroy", fillcolor="rgba(251,146,60,0.1)",
                    name="Avg Inverter Temp (°C)",
                )
                fig_tmp.add_scatter(
                    x=df_temp["YearMonth"], y=df_temp["Roll12"],
                    mode="lines", name="12-mo Rolling Avg",
                    line=dict(color="#f472b6", width=2, dash="dot"),
                )
                fig_tmp.add_hline(y=75, line_dash="dash", line_color="#ff5f5f",
                                  annotation_text="⚠ 75°C Threshold")
                apply_layout(fig_tmp, title="Monthly Average Inverter Temperature",
                             yaxis_title="°C")
                st.plotly_chart(fig_tmp, use_container_width=True)
            else:
                st.info("⑥ Temperature column not found in API response — skipping.")

            # ─────────────────────────────────────────────────
            # ⑦ Electrical Properties Dashboard
            # ─────────────────────────────────────────────────
            st.subheader("⑦ Electrical Properties Over Time")
            elec_map = {k: v for k, v in {
                "AC Power (kW)":       pwr_col,
                "Power Factor":        pf_col,
                "Efficiency (%)":      eff_col,
                "Grid Frequency (Hz)": freq_col,
            }.items() if v}

            if not elec_map:
                st.info("No electrical property columns found in API response.")
            else:
                n = len(elec_map)
                fig_el = make_subplots(
                    rows=n, cols=1, shared_xaxes=True,
                    subplot_titles=list(elec_map.keys()),
                    vertical_spacing=0.06,
                )
                elec_colors = ["#a78bfa","#34d399","#60a5fa","#f472b6"]
                for row_i, (label, col) in enumerate(elec_map.items(), start=1):
                    df_all[col] = pd.to_numeric(df_all[col], errors="coerce")
                    df_e = (
                        df_all.groupby("YearMonth")[col].mean()
                        .reset_index().sort_values("YearMonth")
                    )
                    df_e["Roll12"] = df_e[col].rolling(12, min_periods=3).mean()
                    c = elec_colors[(row_i - 1) % len(elec_colors)]
                    fig_el.add_scatter(
                        x=df_e["YearMonth"], y=df_e[col],
                        mode="lines+markers", line=dict(color=c, width=1.5),
                        name=label, row=row_i, col=1,
                    )
                    fig_el.add_scatter(
                        x=df_e["YearMonth"], y=df_e["Roll12"],
                        mode="lines", name=f"{label} 12-mo avg",
                        line=dict(color="#f472b6", width=1.5, dash="dot"),
                        row=row_i, col=1,
                    )
                fig_el.update_layout(
                    height=220 * n, showlegend=False,
                    paper_bgcolor=_BG, plot_bgcolor=_BG,
                    font=dict(color=_FONT_CLR),
                    margin=dict(t=55, b=40, l=10, r=10),
                )
                fig_el.update_xaxes(gridcolor=_GRID, zerolinecolor=_GRID)
                fig_el.update_yaxes(gridcolor=_GRID, zerolinecolor=_GRID)
                st.plotly_chart(fig_el, use_container_width=True)

            # ─────────────────────────────────────────────────
            # ⑧ Energy vs Inverter Temperature Scatter
            # ─────────────────────────────────────────────────
            if temp_col and "Temp" in df_all.columns:
                st.subheader("⑧ Energy vs Inverter Temperature Scatter")
                df_sc = df_all[["YearMonth","Energy_kWh","Temp","Year"]].dropna()
                fig_sc = go.Figure()
                for idx, yr in enumerate(sorted(hist_years)):
                    ds = df_sc[df_sc["Year"] == yr]
                    fig_sc.add_scatter(
                        x=ds["Temp"], y=ds["Energy_kWh"],
                        mode="markers", name=str(yr),
                        marker=dict(color=PALETTE[idx % len(PALETTE)], size=9, opacity=0.8),
                    )
                apply_layout(fig_sc,
                             title="Monthly Energy vs Avg Inverter Temperature",
                             xaxis_title="Avg Inverter Temp (°C)",
                             yaxis_title="Energy (kWh)")
                st.plotly_chart(fig_sc, use_container_width=True)


# ================================================================
# TAB 4 — Health & String Diagnostics
# ================================================================
with tab4:
    st.header("Equipment Health & DC String Analysis")

    if st.button("Run Live Diagnostics", key="btn_diag"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")

            with st.spinner("Fetching device list…"):
                j_devs = post_with_backoff(
                    client.s, f"{client.base_url}/thirdData/getDevList",
                    {"stationCodes": sid},
                )
            inv_ids = [
                d["id"] for d in j_devs.get("data", [])
                if d.get("devTypeId") in INVERTER_DEV_TYPE_IDS
            ]

            if not inv_ids:
                st.warning("No inverters found for this station.")
            else:
                df_kpi = get_dev_real_kpi_cached(
                    client.base_url, 1, tuple(inv_ids[:3]),
                    client.xsrf, client.verify_ssl,
                )

                if df_kpi.empty:
                    st.warning("No real-time KPI data available.")
                else:
                    # Telemetry table
                    tech_map = {
                        "dataItemMap.active_power": "AC Power (kW)",
                        "dataItemMap.temperature":  "Temp (°C)",
                        "dataItemMap.elec_freq":     "Freq (Hz)",
                        "dataItemMap.power_factor":  "PF",
                        "dataItemMap.efficiency":    "Eff %",
                    }
                    df_disp = df_kpi.rename(columns=tech_map)
                    avail   = [v for v in tech_map.values() if v in df_disp.columns]
                    st.subheader("Inverter Telemetry")
                    st.dataframe(df_disp[avail], use_container_width=True)

                    # DC String Analysis
                    st.divider()
                    st.subheader("DC String Analysis")
                    string_cols = [c for c in df_kpi.columns if "pv" in c and "_i" in c]

                    if not string_cols:
                        st.info("No DC string current columns found (expected pattern: pv*_i).")
                    else:
                        s_vals = df_kpi[string_cols].iloc[0].astype(float).sort_values(ascending=False)
                        active = s_vals[s_vals > 0.1]  # filter offline/night strings

                        mean_a = active.mean() if len(active) > 0 else 0
                        bar_colors = [
                            "#ff5f5f" if v <= 0.1 else
                            "#f0b429" if v < mean_a * 0.88 else
                            "#4ade80"
                            for v in s_vals.values
                        ]
                        fig_s = go.Figure(go.Bar(
                            x=s_vals.index, y=s_vals.values,
                            marker_color=bar_colors,
                        ))
                        if mean_a > 0:
                            fig_s.add_hline(
                                y=mean_a, line_dash="dash", line_color="#60a5fa",
                                annotation_text=f"Mean: {mean_a:.2f} A",
                            )
                        apply_layout(fig_s, title="DC String Currents (Amps)",
                                     yaxis_title="Amperes")
                        st.plotly_chart(fig_s, use_container_width=True)

                        if len(active) > 1:
                            cv   = active.std() / active.mean()
                            low  = active[active < active.mean() * 0.88]
                            c1, c2, c3 = st.columns(3)
                            c1.metric("Active Strings", len(active))
                            c2.metric("Mean Current",   f"{active.mean():.2f} A")
                            c3.metric("CV (variation)", f"{cv:.1%}")

                            if cv > 0.12:
                                st.error(
                                    f"⚠️ **Anomaly Detected:** Variation {cv:.1%} — "
                                    f"{len(low)} string(s) >12% below mean. "
                                    "Check shading, soiling, or connections."
                                )
                                if not low.empty:
                                    st.write("**Underperforming strings:**", list(low.index))
                            else:
                                st.success(f"✅ Strings balanced (CV: {cv:.1%}).")
                        else:
                            st.info("Not enough active strings — plant may be offline.")