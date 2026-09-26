#!/usr/bin/env python3
"""
amber_backfill.py -- one-time historical backfill of Amber Electric usage
into Home Assistant's Energy Dashboard.

Designed to be a single "run it and walk away" step: rate limits from
Amber's API are retried automatically and indefinitely (with a capped
backoff), so this never requires the user to notice a failure and
re-run it manually. Progress is cached to disk after every successful
day, so even a hard interruption (Ctrl+C, machine sleep, etc.) loses
nothing -- simply run it again and it resumes exactly where it left off.

PREREQUISITES -- see the README. In particular:
    - The "Import Statistics" HACS integration must be installed and added.
    - sensor.amber_energy_import / sensor.amber_energy_export and the two
      input_number helpers must already exist (see configuration_snippet.yaml).
    - Know whether your Home Assistant instance serves HTTP or HTTPS.
"""

import requests
import time
import json
import os
from datetime import date, timedelta

# ============================================================================
# EDIT THESE
# ============================================================================
AMBER_API_KEY = "psk_REPLACE_ME"
AMBER_SITE_ID = "REPLACE_ME"
HA_BASE_URL = "http://REPLACE_ME:8123"          # include the scheme! http or https
HA_TOKEN = "REPLACE_ME_LONG_LIVED_TOKEN"
HA_VERIFY_SSL = False
END_DATE = date.today() - timedelta(days=1)
DAYS = 90
TIMEZONE = "Australia/Sydney"
CACHE_FILE = "amber_backfill_cache.json"
REQUEST_DELAY = 2.0
RATE_LIMIT_DEFAULT_WAIT = 60      # seconds, used if Amber doesn't send Retry-After
RATE_LIMIT_MAX_WAIT = 600          # cap how long any single wait can be
# ============================================================================

START_DATE = END_DATE - timedelta(days=DAYS - 1)

amber_headers = {"Authorization": f"Bearer {AMBER_API_KEY}"}
ha_headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

cache = {}
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE) as f:
        cache = json.load(f)
    print(f"Loaded {len(cache)} cached days from {CACHE_FILE}")


def fetch_day(ds):
    """Fetch one day's usage from Amber. Retries indefinitely on rate limits
    (429) and on transient network errors -- this function only returns
    once it has real data, or raises on a genuine non-retryable error
    (e.g. bad API key, which would be a 401/403)."""
    url = f"https://api.amber.com.au/v1/sites/{AMBER_SITE_ID}/usage?startDate={ds}&endDate={ds}"
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, headers=amber_headers, timeout=30)
        except requests.exceptions.RequestException as e:
            wait = min(RATE_LIMIT_DEFAULT_WAIT * attempt, RATE_LIMIT_MAX_WAIT)
            print(f"  Network error on {ds} ({e}); retrying in {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", RATE_LIMIT_DEFAULT_WAIT))
            wait = min(max(wait, 1), RATE_LIMIT_MAX_WAIT)
            print(f"  Rate limited on {ds} (attempt {attempt}), waiting {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code in (401, 403):
            # Not retryable -- a bad API key waiting longer won't fix it.
            resp.raise_for_status()

        if resp.status_code >= 500:
            wait = min(RATE_LIMIT_DEFAULT_WAIT * attempt, RATE_LIMIT_MAX_WAIT)
            print(f"  Amber server error ({resp.status_code}) on {ds}, retrying in {wait}s...")
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp.json()


print(f"Fetching {DAYS} days from Amber: {START_DATE} to {END_DATE}")
print("(Rate limits are handled automatically -- this may pause and resume on its own; just let it run.)\n")

d = START_DATE
while d <= END_DATE:
    ds = d.strftime("%Y-%m-%d")
    if ds in cache:
        print(f"  {ds}: (cached) import={cache[ds][0]:.3f} kWh, export={cache[ds][1]:.3f} kWh")
        d += timedelta(days=1)
        continue

    records = fetch_day(ds)
    import_kwh = round(sum(r["kwh"] for r in records if r.get("channelType") == "general"), 3)
    export_kwh = round(sum(r["kwh"] for r in records if r.get("channelType") == "feedIn"), 3)

    cache[ds] = [import_kwh, export_kwh]
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    print(f"  {ds}: import={import_kwh:.3f} kWh, export={export_kwh:.3f} kWh")
    d += timedelta(days=1)
    time.sleep(REQUEST_DELAY)

print("\nAll days fetched. Computing running totals...")

import_values = []
running_import = 0.0
running_export = 0.0
d = START_DATE
while d <= END_DATE:
    ds = d.strftime("%Y-%m-%d")
    imp, exp = cache[ds]
    running_import = round(running_import + imp, 3)
    running_export = round(running_export + exp, 3)
    import_values.append((d, running_import, running_export))
    d += timedelta(days=1)


def fmt(d):
    return d.strftime("%d.%m.%Y 00:00")


final_import = import_values[-1][1]
final_export = import_values[-1][2]
print(f"\nFinal cumulative totals through {END_DATE}: import={final_import} kWh, export={final_export} kWh")

payload = {
    "timezone_identifier": TIMEZONE,
    "entities": [
        {
            "id": "sensor.amber_energy_import",
            "unit": "kWh",
            "values": [{"datetime": fmt(d), "sum": imp, "state": imp} for d, imp, exp in import_values],
        },
        {
            "id": "sensor.amber_energy_export",
            "unit": "kWh",
            "values": [{"datetime": fmt(d), "sum": exp, "state": exp} for d, imp, exp in import_values],
        },
    ],
}

print("\nImporting into Home Assistant statistics (single call)...")
import_url = f"{HA_BASE_URL}/api/services/import_statistics/import_from_json"
resp = requests.post(import_url, headers=ha_headers, json=payload, timeout=180, verify=HA_VERIFY_SSL)
print(f"Status: {resp.status_code}")
print(resp.text[:1000])
resp.raise_for_status()

print("\nSeeding input_number running-total helpers...")
for entity_id, value in [
    ("input_number.amber_energy_import_running_total", final_import),
    ("input_number.amber_energy_export_running_total", final_export),
]:
    set_url = f"{HA_BASE_URL}/api/services/input_number/set_value"
    r = requests.post(
        set_url, headers=ha_headers, json={"entity_id": entity_id, "value": value},
        timeout=30, verify=HA_VERIFY_SSL,
    )
    print(f"  {entity_id} -> {value}: HTTP {r.status_code}")
    r.raise_for_status()

print("\nDone. Verify with import_statistics.export_statistics before relying on the")
print("Energy dashboard's history view -- see the README's verification steps.")
