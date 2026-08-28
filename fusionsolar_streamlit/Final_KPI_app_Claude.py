# Final_KPI_app_3.py
# Streamlit app for Huawei FusionSolar Northbound API
# Improvements: Bug fixes, session_state login, historical deep-dive analytics,
#               PR trend, cumulative energy, temperature & electrical plots,
#               zero-string false alarm fix.

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
PLANT_TZ = "Europe/Athens"
SHIFT_MONTHS = 1
PLANT_START_YEAR = 2025          # <-- SET YOUR PLANT COD YEAR HERE
PLANT_PEAK_KW   = 1100.0         # <-- SET YOUR INSTALLED CAPACITY (kWp) HERE
INVERTER_DEV_TYPE_IDS = [1, 38, 39]   # String, HV, and central inverters

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
):
    last_resp = None
    for attempt in range(max_retries):
        resp = session.post(url, json=json_payload, timeout=timeout)
        last_resp = resp
        if resp.status_code == 200:
            try:
                j = resp.json()
                if j.get("failCode") == 407:
                    sleep_for = min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter)
                    time.sleep(sleep_for)
                    continue
                return j
            except Exception:
                continue
        sleep_for = min(max_sleep, base_sleep * (2 ** attempt)) + random.uniform(0, jitter)
        time.sleep(sleep_for)
    return last_resp.json() if last_resp else {"data": None, "failCode": -1}


# ----------------------- Client -----------------------
class HuaweiClient:
    def __init__(self, secrets: dict):
        self.base_url = secrets["base_url"].rstrip("/")
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
            token = resp.cookies.get("XSRF-TOKEN") or resp.headers.get("xsrf-token")
            if not token:
                return False, "No XSRF token received — check credentials or API endpoint."
            self.s.headers.update({"XSRF-TOKEN": token})
            return True, "Login Successful"
        except Exception as e:
            return False, str(e)

    def get_stations(self) -> Tuple[Optional[List[dict]], Optional[str]]:
        r = post_with_backoff(self.s, f"{self.base_url}/thirdData/getStationList", {})
        data = r.get("data", [])
        rows = data.get("list", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if rows:
            return rows, None
        return None, "No stations returned by API"


# ----------------------- Session-state login helper -----------------------
def ensure_client() -> Tuple[Optional[HuaweiClient], Optional[List[dict]]]:
    """
    Returns (client, stations) from session_state, logging in only when needed.
    Shows st.error and returns (None, None) on failure.
    """
    if "hw_client" not in st.session_state:
        fusion = st.secrets["fusion"]
        c = HuaweiClient(fusion)
        ok, msg = c.login()
        if not ok:
            st.error(f"❌ Login failed: {msg}")
            return None, None
        stations, err = c.get_stations()
        if not stations:
            st.error(f"❌ Could not fetch stations: {err}")
            return None, None
        st.session_state["hw_client"] = c
        st.session_state["hw_stations"] = stations

    return st.session_state["hw_client"], st.session_state["hw_stations"]


# ----------------------- Data helpers -----------------------
def get_budget_df(year: int) -> pd.DataFrame:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    targets = [117077, 89742, 140573, 172775, 177950, 186287,
               197265, 190014, 168524, 132649, 86079, 82732]
    if year == 2025:
        for i in range(9):
            targets[i] = 0
    return pd.DataFrame({"Month": months, "Budget_kWh": targets, "Year": year})


def normalize_safe(rows_in) -> pd.DataFrame:
    if not rows_in:
        return pd.DataFrame()
    df = pd.json_normalize(rows_in, sep=".")
    return df.loc[:, ~df.columns.duplicated()].copy()


@st.cache_data(ttl=1800, show_spinner=False)
def get_kpi_station_hour_cached(base_url, station_code, target_date, xsrf_token, verify_ssl) -> dict:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    dt = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
    return post_with_backoff(s, f"{base_url}/thirdData/getKpiStationHour", payload)


@st.cache_data(ttl=300, show_spinner=False)
def get_dev_real_kpi_cached(base_url, dev_type_id, dev_ids_tuple, xsrf_token, verify_ssl) -> pd.DataFrame:
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    payload = {"devTypeId": dev_type_id, "devIds": ",".join(map(str, dev_ids_tuple))}
    j = post_with_backoff(s, f"{base_url}/thirdData/getDevRealKpi", payload)
    return normalize_safe(j.get("data", []))


@st.cache_data(ttl=3600, show_spinner=False)
def get_monthly_kpi_cached(base_url, station_code, year, xsrf_token, verify_ssl) -> pd.DataFrame:
    """Fetch monthly station KPI for a given year via getKpiStationMonth."""
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"Content-Type": "application/json", "XSRF-TOKEN": xsrf_token})
    # First day of year in ms UTC
    dt = datetime(year, 1, 1, tzinfo=timezone.utc)
    payload = {"stationCodes": station_code, "collectTime": int(dt.timestamp() * 1000)}
    j = post_with_backoff(s, f"{base_url}/thirdData/getKpiStationMonth", payload)
    raw = j.get("data", [])
    if raw and isinstance(raw, list) and "kpiList" in raw[0]:
        raw = raw[0]["kpiList"]
    return normalize_safe(raw)


# ----------------------- Plot helpers -----------------------
PLOT_LAYOUT = dict(
    paper_bgcolor="#0e1117",
    plot_bgcolor="#0e1117",
    font=dict(color="#e2e8f0"),
    xaxis=dict(gridcolor="#1f2333", zerolinecolor="#1f2333"),
    yaxis=dict(gridcolor="#1f2333", zerolinecolor="#1f2333"),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
    margin=dict(t=50, b=40, l=10, r=10),
)


def apply_layout(fig, **extra):
    fig.update_layout(**{**PLOT_LAYOUT, **extra})
    return fig


# ----------------------- Main UI -----------------------
st.title("☀️ FusionSolar Advanced Analytics")

year_input = int(st.sidebar.number_input("Year", min_value=PLANT_START_YEAR, max_value=2030, value=2025))
st.sidebar.caption(f"Plant start year: {PLANT_START_YEAR}")
st.sidebar.caption(f"Installed capacity: {PLANT_PEAK_KW:.0f} kWp")
if st.sidebar.button("🔄 Reset Login"):
    for k in ["hw_client", "hw_stations"]:
        st.session_state.pop(k, None)
    st.rerun()

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Monthly Performance",
    "📈 Hourly Curves",
    "📉 Historical Deep-Dive",
    "🛠️ Health & Strings",
])


# ============================================================
# TAB 1 — Monthly Performance (current year vs budget)
# ============================================================
with tab1:
    st.header("Monthly Energy vs Budget")
    if st.button("Refresh Monthly KPIs", key="btn_monthly"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")
            xsrf = client.s.headers.get("XSRF-TOKEN")

            with st.spinner("Fetching monthly data…"):
                df_raw = get_monthly_kpi_cached(client.base_url, sid, year_input, xsrf, client.verify_ssl)

            if df_raw.empty:
                st.warning("No monthly data returned for this year.")
            else:
                # ---- resolve column names robustly ----
                tcol = next((c for c in df_raw.columns if "time" in c.lower() or "collect" in c.lower()), None)
                ecol = next((c for c in df_raw.columns if "inverterYield" in c or "month_cap" in c or "energy" in c.lower()), None)

                if not tcol or not ecol:
                    st.error(f"Unexpected column names: {list(df_raw.columns)}")
                else:
                    df_raw["dt"] = pd.to_datetime(df_raw[tcol], unit="ms", utc=True).dt.tz_convert(PLANT_TZ)
                    df_raw["MonthNum"] = df_raw["dt"].dt.month
                    df_raw["Energy_kWh"] = pd.to_numeric(df_raw[ecol], errors="coerce")

                    budget_df = get_budget_df(year_input)
                    budget_df["MonthNum"] = range(1, 13)
                    merged = budget_df.merge(df_raw[["MonthNum", "Energy_kWh"]], on="MonthNum", how="left")
                    merged["Delta_kWh"] = merged["Energy_kWh"] - merged["Budget_kWh"]
                    merged["Achievement_%"] = (merged["Energy_kWh"] / merged["Budget_kWh"].replace(0, pd.NA) * 100).round(1)

                    # ---- bar chart ----
                    fig = go.Figure()
                    fig.add_bar(x=merged["Month"], y=merged["Budget_kWh"], name="Budget", marker_color="#334155")
                    fig.add_bar(x=merged["Month"], y=merged["Energy_kWh"], name="Actual", marker_color="#f0b429")
                    apply_layout(fig, title=f"{year_input} Monthly Energy vs Budget", barmode="group",
                                 yaxis_title="kWh", xaxis_title="Month")
                    st.plotly_chart(fig, use_container_width=True)

                    # ---- delta waterfall ----
                    colors = ["#4ade80" if v >= 0 else "#ff5f5f" for v in merged["Delta_kWh"].fillna(0)]
                    fig2 = go.Figure(go.Bar(
                        x=merged["Month"],
                        y=merged["Delta_kWh"],
                        marker_color=colors,
                        name="Actual − Budget",
                    ))
                    apply_layout(fig2, title="Monthly Delta (Actual − Budget)", yaxis_title="kWh")
                    st.plotly_chart(fig2, use_container_width=True)

                    # ---- summary table ----
                    st.subheader("Summary Table")
                    display_cols = ["Month", "Budget_kWh", "Energy_kWh", "Delta_kWh", "Achievement_%"]
                    st.dataframe(
                        merged[display_cols].style.format({
                            "Budget_kWh": "{:,.0f}",
                            "Energy_kWh": "{:,.0f}",
                            "Delta_kWh": "{:+,.0f}",
                            "Achievement_%": "{:.1f}%",
                        }),
                        use_container_width=True,
                    )


# ============================================================
# TAB 2 — Hourly Production Curves
# ============================================================
with tab2:
    st.header("Daily Production Profile")
    target_date = st.date_input("Select Analysis Day", value=date.today() - timedelta(days=1))

    if st.button("Generate Curve", key="btn_hourly"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")
            j_hr = get_kpi_station_hour_cached(
                client.base_url, sid, target_date,
                client.s.headers.get("XSRF-TOKEN"), client.verify_ssl
            )

            raw_hr = j_hr.get("data", [])
            if raw_hr and "kpiList" in raw_hr[0]:
                raw_hr = raw_hr[0]["kpiList"]
            df_hr = normalize_safe(raw_hr)

            if df_hr.empty:
                st.warning("No hourly data available for the selected date.")
            else:
                tcol = next((c for c in df_hr.columns if "time" in c.lower() or "collect" in c.lower()), None)
                ycol = next((c for c in df_hr.columns if "inverterYield" in c or "day_cap" in c), None)

                if tcol and ycol:
                    df_hr["dt"] = pd.to_datetime(df_hr[tcol], unit="ms", utc=True).dt.tz_convert(PLANT_TZ)
                    df_hr["Hour"] = df_hr["dt"].dt.hour
                    df_hr[ycol] = pd.to_numeric(df_hr[ycol], errors="coerce")

                    fig = go.Figure(go.Scatter(
                        x=df_hr["Hour"], y=df_hr[ycol],
                        fill="tozeroy", line_color="#f0b429",
                        fillcolor="rgba(240,180,41,0.15)",
                        name="Hourly Yield (kWh)",
                    ))
                    apply_layout(fig, title=f"Generation Profile: {target_date}",
                                 xaxis_title="Hour of Day", yaxis_title="kWh")
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.error(f"Could not identify time/energy columns. Found: {list(df_hr.columns)}")


# ============================================================
# TAB 3 — Historical Deep-Dive
# ============================================================
with tab3:
    st.header("Historical Performance Deep-Dive")
    st.caption("Fetches and aggregates monthly data across all years since plant start.")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        hist_years = st.multiselect(
            "Select years to analyse",
            options=list(range(PLANT_START_YEAR, date.today().year + 1)),
            default=list(range(PLANT_START_YEAR, date.today().year + 1)),
        )
    with col_b:
        irr_kwh_m2 = st.number_input(
            "Avg monthly irradiation (kWh/m²) — for PR estimate",
            value=150.0, step=5.0,
            help="Used as a flat reference for PR trend. Replace with real POA data if available.",
        )

    if st.button("Load Historical Data", key="btn_hist"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")
            xsrf = client.s.headers.get("XSRF-TOKEN")

            all_frames = []
            progress = st.progress(0, text="Fetching years…")
            for i, yr in enumerate(sorted(hist_years)):
                with st.spinner(f"Fetching {yr}…"):
                    df_yr = get_monthly_kpi_cached(client.base_url, sid, yr, xsrf, client.verify_ssl)
                if not df_yr.empty:
                    df_yr["_year"] = yr
                    all_frames.append(df_yr)
                progress.progress((i + 1) / len(hist_years), text=f"Fetched {yr}")
            progress.empty()

            if not all_frames:
                st.warning("No historical data returned.")
            else:
                df_all = pd.concat(all_frames, ignore_index=True)

                tcol = next((c for c in df_all.columns if "time" in c.lower() or "collect" in c.lower()), None)
                ecol = next((c for c in df_all.columns if "inverterYield" in c or "month_cap" in c or "energy" in c.lower()), None)
                temp_col = next((c for c in df_all.columns if "temperature" in c.lower() or "temp" in c.lower()), None)
                # Electrical properties
                pwr_col  = next((c for c in df_all.columns if "active_power" in c.lower() or "power" in c.lower()), None)
                pf_col   = next((c for c in df_all.columns if "power_factor" in c.lower()), None)
                eff_col  = next((c for c in df_all.columns if "efficiency" in c.lower()), None)
                freq_col = next((c for c in df_all.columns if "elec_freq" in c.lower() or "freq" in c.lower()), None)

                if not tcol or not ecol:
                    st.error(f"Cannot identify required columns. Got: {list(df_all.columns)}")
                else:
                    df_all["dt"] = pd.to_datetime(df_all[tcol], unit="ms", utc=True).dt.tz_convert(PLANT_TZ)
                    df_all["YearMonth"] = df_all["dt"].dt.to_period("M").astype(str)
                    df_all["MonthNum"] = df_all["dt"].dt.month
                    df_all["Year"] = df_all["_year"]
                    df_all["Energy_kWh"] = pd.to_numeric(df_all[ecol], errors="coerce")
                    df_all = df_all.sort_values("dt")

                    # ---- 1. Monthly Energy vs Budget (all years overlay) ----
                    st.subheader("① Monthly Energy — Year-over-Year")
                    fig_yoy = go.Figure()
                    month_labels = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
                    palette = ["#f0b429","#3ecfcf","#60a5fa","#a78bfa","#fb923c","#34d399"]
                    for idx, yr in enumerate(sorted(hist_years)):
                        df_y = df_all[df_all["Year"] == yr].sort_values("MonthNum")
                        fig_yoy.add_scatter(
                            x=df_y["MonthNum"], y=df_y["Energy_kWh"],
                            mode="lines+markers",
                            name=str(yr),
                            line=dict(color=palette[idx % len(palette)], width=2),
                        )
                    bdf = get_budget_df(2024)  # use base-year budget as reference
                    fig_yoy.add_scatter(
                        x=list(range(1, 13)), y=bdf["Budget_kWh"],
                        mode="lines", name="Budget (ref)",
                        line=dict(color="#475569", dash="dash"),
                    )
                    apply_layout(fig_yoy,
                                 xaxis=dict(tickmode="array", tickvals=list(range(1,13)), ticktext=month_labels,
                                            gridcolor="#1f2333"),
                                 yaxis=dict(title="kWh", gridcolor="#1f2333"),
                                 title="Monthly Energy per Year vs Budget Reference")
                    st.plotly_chart(fig_yoy, use_container_width=True)

                    # ---- 2. Cumulative Energy ----
                    st.subheader("② Cumulative Energy Since COD")
                    df_cum = df_all.groupby("YearMonth")["Energy_kWh"].sum().reset_index()
                    df_cum = df_cum.sort_values("YearMonth")
                    df_cum["Cumulative_MWh"] = df_cum["Energy_kWh"].cumsum() / 1000

                    fig_cum = go.Figure(go.Scatter(
                        x=df_cum["YearMonth"], y=df_cum["Cumulative_MWh"],
                        fill="tozeroy", line_color="#3ecfcf",
                        fillcolor="rgba(62,207,207,0.12)",
                        name="Cumulative MWh",
                    ))
                    apply_layout(fig_cum, title="Cumulative Energy Production (MWh)", yaxis_title="MWh")
                    st.plotly_chart(fig_cum, use_container_width=True)
                    total_mwh = df_cum["Cumulative_MWh"].iloc[-1] if not df_cum.empty else 0
                    st.metric("Total Production to Date", f"{total_mwh:,.1f} MWh")

                    # ---- 3. Performance Ratio Trend ----
                    st.subheader("③ Performance Ratio (PR) Trend")
                    st.caption(
                        "PR = Actual Energy / (Irradiation × Capacity). "
                        "Using a flat irradiation reference — replace with real POA data for accuracy."
                    )
                    df_pr = df_all.groupby("YearMonth")["Energy_kWh"].sum().reset_index()
                    df_pr = df_pr.sort_values("YearMonth")
                    # PR = E_actual (kWh) / (G_ref (kWh/m²) × Wp/1000)
                    reference_yield = irr_kwh_m2 * PLANT_PEAK_KW  # kWh
                    df_pr["PR"] = df_pr["Energy_kWh"] / reference_yield

                    fig_pr = go.Figure()
                    fig_pr.add_scatter(
                        x=df_pr["YearMonth"], y=df_pr["PR"],
                        mode="lines+markers", line_color="#60a5fa",
                        marker=dict(size=6), name="Monthly PR",
                    )
                    fig_pr.add_hline(y=0.75, line_dash="dash", line_color="#4ade80",
                                     annotation_text="Target PR 0.75")
                    fig_pr.add_hline(y=0.65, line_dash="dot", line_color="#ff5f5f",
                                     annotation_text="Alert PR 0.65")
                    apply_layout(fig_pr, title="Performance Ratio Over Time",
                                 yaxis=dict(title="PR", tickformat=".0%", gridcolor="#1f2333"),
                                 yaxis_tickformat=".0%")
                    st.plotly_chart(fig_pr, use_container_width=True)

                    # ---- 4. Temperature Trend ----
                    if temp_col:
                        st.subheader("④ Inverter Temperature Trend")
                        df_all["Temp"] = pd.to_numeric(df_all[temp_col], errors="coerce")
                        df_temp = df_all.groupby("YearMonth")["Temp"].mean().reset_index().sort_values("YearMonth")

                        fig_temp = go.Figure(go.Scatter(
                            x=df_temp["YearMonth"], y=df_temp["Temp"],
                            mode="lines+markers",
                            line=dict(color="#fb923c", width=2),
                            fill="tozeroy", fillcolor="rgba(251,146,60,0.1)",
                            name="Avg Inverter Temp (°C)",
                        ))
                        fig_temp.add_hline(y=75, line_dash="dash", line_color="#ff5f5f",
                                           annotation_text="⚠ 75°C Threshold")
                        apply_layout(fig_temp, title="Monthly Average Inverter Temperature", yaxis_title="°C")
                        st.plotly_chart(fig_temp, use_container_width=True)
                    else:
                        st.info("④ Temperature data column not found in API response — skipping temp chart.")

                    # ---- 5. Electrical Properties Dashboard ----
                    st.subheader("⑤ Electrical Properties Over Time")
                    elec_available = {
                        "AC Power (kW)": pwr_col,
                        "Power Factor": pf_col,
                        "Efficiency (%)": eff_col,
                        "Grid Frequency (Hz)": freq_col,
                    }
                    elec_available = {k: v for k, v in elec_available.items() if v}

                    if not elec_available:
                        st.info("No electrical property columns (power factor, efficiency, frequency) found in API response.")
                    else:
                        n = len(elec_available)
                        fig_elec = make_subplots(
                            rows=n, cols=1,
                            shared_xaxes=True,
                            subplot_titles=list(elec_available.keys()),
                            vertical_spacing=0.06,
                        )
                        elec_colors = ["#a78bfa", "#34d399", "#60a5fa", "#f472b6"]
                        for row_idx, (label, col) in enumerate(elec_available.items(), start=1):
                            df_all[col] = pd.to_numeric(df_all[col], errors="coerce")
                            df_e = df_all.groupby("YearMonth")[col].mean().reset_index().sort_values("YearMonth")
                            fig_elec.add_trace(
                                go.Scatter(
                                    x=df_e["YearMonth"], y=df_e[col],
                                    mode="lines+markers",
                                    line=dict(color=elec_colors[(row_idx - 1) % len(elec_colors)], width=1.5),
                                    name=label,
                                ),
                                row=row_idx, col=1,
                            )
                        fig_elec.update_layout(
                            height=220 * n,
                            showlegend=False,
                            **PLOT_LAYOUT,
                        )
                        for i in range(1, n + 1):
                            fig_elec.update_xaxes(gridcolor="#1f2333", row=i, col=1)
                            fig_elec.update_yaxes(gridcolor="#1f2333", row=i, col=1)
                        st.plotly_chart(fig_elec, use_container_width=True)

                    # ---- 6. Energy vs Temp scatter (if both available) ----
                    if temp_col and "Temp" in df_all.columns:
                        st.subheader("⑥ Energy vs Temperature Scatter")
                        df_scatter = df_all[["YearMonth", "Energy_kWh", "Temp", "Year"]].dropna()
                        fig_sc = go.Figure()
                        for idx, yr in enumerate(sorted(hist_years)):
                            ds = df_scatter[df_scatter["Year"] == yr]
                            fig_sc.add_scatter(
                                x=ds["Temp"], y=ds["Energy_kWh"],
                                mode="markers", name=str(yr),
                                marker=dict(color=palette[idx % len(palette)], size=9, opacity=0.8),
                            )
                        apply_layout(fig_sc,
                                     title="Monthly Energy vs Avg Inverter Temperature",
                                     xaxis_title="Avg Inverter Temp (°C)",
                                     yaxis_title="Energy (kWh)")
                        st.plotly_chart(fig_sc, use_container_width=True)


# ============================================================
# TAB 4 — Health & String Diagnostics
# ============================================================
with tab4:
    st.header("Equipment Health & DC String Analysis")

    if st.button("Run Live Diagnostics", key="btn_diag"):
        client, stations = ensure_client()
        if client and stations:
            sid = stations[0].get("stationCode") or stations[0].get("plantCode")
            xsrf = client.s.headers.get("XSRF-TOKEN")

            with st.spinner("Fetching device list…"):
                j_devs = post_with_backoff(
                    client.s,
                    f"{client.base_url}/thirdData/getDevList",
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
                    client.base_url, 1, tuple(inv_ids[:3]), xsrf, client.verify_ssl
                )

                if df_kpi.empty:
                    st.warning("No real-time KPI data available.")
                else:
                    # ---- Inverter Telemetry Table ----
                    tech_map = {
                        "dataItemMap.active_power": "AC Power (kW)",
                        "dataItemMap.temperature":  "Temp (°C)",
                        "dataItemMap.elec_freq":     "Freq (Hz)",
                        "dataItemMap.power_factor":  "PF",
                        "dataItemMap.efficiency":    "Eff %",
                    }
                    df_display = df_kpi.rename(columns=tech_map)
                    available_cols = [v for v in tech_map.values() if v in df_display.columns]
                    st.subheader("Inverter Telemetry")
                    st.dataframe(df_display[available_cols], use_container_width=True)

                    # ---- DC String Analysis ----
                    st.divider()
                    st.subheader("DC String Analysis")
                    string_cols = [c for c in df_kpi.columns if "pv" in c and "_i" in c]

                    if not string_cols:
                        st.info("No DC string current columns found (expected pattern: pv*_i).")
                    else:
                        s_vals_raw = df_kpi[string_cols].iloc[0].astype(float).sort_values(ascending=False)

                        # FIX: filter near-zero strings (night / disconnected) before CV calculation
                        active_strings = s_vals_raw[s_vals_raw > 0.1]

                        fig_s = go.Figure(go.Bar(
                            x=s_vals_raw.index,
                            y=s_vals_raw.values,
                            marker_color=[
                                "#ff5f5f" if v <= 0.1 else
                                "#f0b429" if v < active_strings.mean() * 0.88 else
                                "#4ade80"
                                for v in s_vals_raw.values
                            ],
                        ))
                        apply_layout(fig_s, title="DC String Currents (Amps)", yaxis_title="Amperes")
                        if len(active_strings) > 0:
                            fig_s.add_hline(
                                y=active_strings.mean(),
                                line_dash="dash", line_color="#60a5fa",
                                annotation_text=f"Mean: {active_strings.mean():.2f} A",
                            )
                        st.plotly_chart(fig_s, use_container_width=True)

                        # ---- Statistical anomaly check (on active strings only) ----
                        if len(active_strings) > 1:
                            cv = active_strings.std() / active_strings.mean()
                            low_strings = active_strings[active_strings < active_strings.mean() * 0.88]
                            col1, col2, col3 = st.columns(3)
                            col1.metric("Active Strings", len(active_strings))
                            col2.metric("Mean Current", f"{active_strings.mean():.2f} A")
                            col3.metric("CV (variation)", f"{cv:.1%}")

                            if cv > 0.12:
                                st.error(
                                    f"⚠️ **Anomaly Detected:** String current variation is {cv:.1%}. "
                                    f"{len(low_strings)} string(s) are >12% below average — "
                                    "check for shading, soiling, or connection issues."
                                )
                                if not low_strings.empty:
                                    st.write("**Underperforming strings:**", list(low_strings.index))
                            else:
                                st.success(f"✅ Strings are balanced (CV: {cv:.1%}).")
                        else:
                            st.info("Not enough active strings for statistical analysis (plant may be offline).")