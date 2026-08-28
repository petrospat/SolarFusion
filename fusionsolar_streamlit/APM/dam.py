from entsoe import EntsoePandasClient
import pandas as pd
import requests

api_key = "5d29d2f5-0ed7-4c82-9dae-7fa188615e34"

# Fix SSL issue on corporate networks
session = requests.Session()
session.verify = False

# Suppress the SSL warning noise
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

client = EntsoePandasClient(api_key=api_key, session=session)

start = pd.Timestamp("20250605", tz="Europe/Athens")
end   = pd.Timestamp("20260605", tz="Europe/Athens")

ts = client.query_day_ahead_prices("GR", start=start, end=end)

ts_local = ts.tz_convert("Europe/Athens")
evening = ts_local[ts_local.index.hour.isin([18, 19])]

print(f"Total hourly records (18:00–20:00): {len(evening)}")
print(f"\n--- Average DAM Price (18:00–20:00) ---")
print(f"Overall 12-month avg: {evening.mean():.2f} €/MWh")

print(f"\n--- By Hour ---")
by_hour = evening.groupby(evening.index.hour).mean()
print(f"  18:00–19:00: {by_hour[18]:.2f} €/MWh")
print(f"  19:00–20:00: {by_hour[19]:.2f} €/MWh")

print(f"\n--- By Month ---")
by_month = evening.groupby(evening.index.to_period("M")).mean().round(2)
print(by_month.to_string())