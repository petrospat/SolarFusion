**Purpose**: Brief, actionable guidance for AI coding assistants working on this repo.

**Big Picture**:
- **What**: A small Streamlit dashboard (two variants) that queries the Huawei FusionSolar Northbound API and visualises plant & device KPIs.
- **Where**: Main code lives in `fusionsolar_streamlit/`; `app_extended.py` is the full dashboard, `app.py` is a simpler variant. Root `app.py` is a playground demo.
- **Why**: Provide plant/device observability and lightweight export for tenants that expose Huawei Northbound endpoints.

**Key files**:
- `fusionsolar_streamlit/app_extended.py` — primary, production-like dashboard. Read first for architecture and helpers.
- `fusionsolar_streamlit/app.py` — smaller example with the same login/listing patterns.
- `fusionsolar_streamlit/requirements.txt` — declared dependencies (add extras if needed).

**Architecture & data flow**:
- Authentication: `login_session()` posts to `/thirdData/login` and extracts an XSRF token from cookies/headers.
- Station discovery: `list_stations()` tries three approaches in order: port 27200 `/thirdData/stations`, port 443 `/thirdData/stations`, then legacy `/thirdData/getStationList`.
- KPI calls: station and device KPI helpers call endpoints like `/getStationRealKpi`, `/getKpiStationHour`, `/getDevRealKpi`, etc. Responses are JSON-normalized into pandas DataFrames via `normalize_safe()`.
- Device handling: device calls chunk IDs into batches of 100 (see `device_realtime_kpi()`), and `estimate_device_budget()` computes API-call budget per 5 minutes.

**Project-specific conventions & patterns**:
- Use `st.secrets.get("fusion", {})` for configuration; if missing the apps show a UI form to input secrets.
- `VERIFY_SSL` defaults to `False` (the apps disable SSL warnings) — be mindful when toggling.
- Data normalization: use `normalize_safe()` which deduplicates columns; prefer it over raw `pd.json_normalize` for robustness.
- Time handling: API timestamps are milliseconds since epoch; helpers attempt `pd.to_datetime(..., unit='ms')` and look for `collectTime`/`*.collectTime` or any column with `time`.
- Cache policy: functions use `@st.cache_data(ttl=...)` — TTLs are tuned per endpoint (5m, 10m, 25m, 30m). Respect these when changing behaviour.

**Integration & dependencies**:
- Network: depends on access to the Huawei FusionSolar Northbound endpoints and correct Northbound user permissions.
- Proxy support: optional `proxies` can be provided via secrets/config.
- Exports: app writes Excel using `openpyxl` engine; add `openpyxl` to `requirements.txt` if you need Excel downloads to work in the environment.

**Run / dev workflows**
- Install deps (virtualenv) and run from the `fusionsolar_streamlit` folder. Example (PowerShell):
```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r .\fusionsolar_streamlit\requirements.txt
pip install openpyxl    # optional: required for Excel export
cd .\fusionsolar_streamlit
streamlit run app_extended.py
```
- Quick dev run: `streamlit run app.py` in the same folder to use the smaller example.

**Examples to reference in edits**
- To add a new KPI endpoint, follow `station_hour_kpi()` and `normalize_safe()` patterns: login -> POST JSON -> `resp.raise_for_status()` -> `normalize_safe(resp.json().get('data', []))`.
- For batching device queries, reuse the 100-id chunking pattern in `device_realtime_kpi()`.

**What to watch for / gotchas**
- Secrets: the app falls back to an interactive form; automated runs should provide `.streamlit/secrets.toml` with a `[fusion]` section matching keys used in code.
- Dependency gaps: exports use `openpyxl` but it's not currently listed; add it if Excel features are needed in CI or containers.
- TLS: default `VERIFY_SSL=False` may hide SSL problems during local dev; prefer `True` in production.

If anything here looks wrong or you'd like specific examples (secret file template, unit-test scaffolding, CI commands), tell me which part to expand.
