#!/usr/bin/env python3
"""
backfill_rain_archive.py — One-time historical backfill of per-gauge daily
rainfall totals into the permanent rain/archive/daily_{YYYY}.json files.

make_rain_summary.py only starts accumulating daily totals from the day it's
first deployed, since raw 15-min readings are pruned after RETENTION_DAYS=14
(fetch_rain.py) and can't be re-derived once expired. This script fills that
gap for EA gauges by pulling as much history as the EA Hydrology API still
holds and computing the same per-station daily totals make_rain_summary.py
would have computed had it been running the whole time.

Confirmed by probing environment.data.gov.uk/flood-monitoring/archive: the
EA archive holds a rolling ~15 months (~455-460 days) before returning 404
for older dates — DEFAULT_LOOKBACK_DAYS is set with headroom past that; any
date outside the real window just 404s per-day and is skipped harmlessly.

NRW and SEPA have no equivalent historical bulk-readings endpoint (NRW's
Telemetry API only exposes ~24hr rolling windows per station; SEPA's public
API exposes a rolling ~3-4 day window — see fetch_rain.py's fetch_nrw_data /
fetch_sepa_data docstrings), so their archives keep building up day by day
from make_rain_summary.py's normal daily finalisation instead — there's no
equivalent bulk-history source to backfill from for those two networks.

Run once via workflow_dispatch, or locally with R2 env vars set. Safe to
re-run: skips any calendar day that's already substantially archived.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

from fetch_rain import HYDRO_BASE, get_r2, parse_readings, USE_R2
from make_rain_summary import ARCHIVE_PFX, r2_get_json, r2_put_json

MAX_WORKERS = 6
DEFAULT_LOOKBACK_DAYS = 470  # headroom past the confirmed ~455-460 day EA archive window


def fetch_day_rainfall(session, date_str):
    """date_str: YYYY-MM-DD. Returns raw items (fetch_rain.parse_readings shape), or [] if unavailable."""
    url = (f"{HYDRO_BASE}/data/readings.json"
           f"?observedProperty=rainfall&period=900&date={date_str}&_limit=200000")
    try:
        r = session.get(url, timeout=120)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception as e:
        print(f"  Warning: fetch failed for {date_str}: {e}")
        return []


def daily_totals_from_items(items, suid_to_ref):
    """Sum 15-min values per station for a single day's items -> {station_ref: total_mm}."""
    by_date, _ = parse_readings(items, suid_to_ref)
    totals = {}
    for _date_key, station_slots in by_date.items():
        for sid, slots in station_slots.items():
            totals[sid] = totals.get(sid, 0.0) + sum(slots.values())
    return {sid: round(v, 2) for sid, v in totals.items()}


def already_covered(archives, date_str, min_stations=50):
    """Cheap heuristic: a day counts as already archived if at least
    min_stations already have an entry for it, avoiding a full re-fetch on
    reruns without needing an exact per-station diff."""
    archive = archives.get(date_str[:4], {})
    hits = 0
    for station_days in archive.values():
        if date_str in station_days:
            hits += 1
            if hits >= min_stations:
                return True
    return False


def main():
    lookback = int(os.environ.get("BACKFILL_DAYS", DEFAULT_LOOKBACK_DAYS))
    if not USE_R2:
        print("Error: R2 env vars not set — aborting (this job only makes sense writing to R2)")
        sys.exit(1)
    r2 = get_r2()

    stations_data = r2_get_json(r2, "rain/stations.json", None)
    if not stations_data or not stations_data.get("stations"):
        print("Error: rain/stations.json missing on R2 — aborting")
        sys.exit(1)
    stations = stations_data["stations"]
    suid_to_ref = {s["guid"]: ref for ref, s in stations.items() if s.get("guid")}
    print(f"{len(stations)} known stations, {len(suid_to_ref)} EA GUIDs to backfill against")

    now = datetime.now(timezone.utc)
    dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, lookback + 1)]

    years = sorted({d[:4] for d in dates}, reverse=True)
    archives = {y: (r2_get_json(r2, f"{ARCHIVE_PFX}/daily_{y}.json", {}) or {}) for y in years}

    todo = [d for d in dates if not already_covered(archives, d)]
    print(f"{len(dates)} candidate days ({dates[-1]} .. {dates[0]}), {len(todo)} not yet archived")

    session = requests.Session()
    session.mount(HYDRO_BASE, requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

    filled, done = 0, 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_day_rainfall, session, d): d for d in todo}
        for fut in as_completed(futures):
            date_str = futures[fut]
            items = fut.result()
            done += 1
            if items:
                totals = daily_totals_from_items(items, suid_to_ref)
                if totals:
                    archive = archives.setdefault(date_str[:4], {})
                    for sid, mm in totals.items():
                        if sid not in stations:
                            continue
                        archive.setdefault(sid, {})[date_str] = mm
                    filled += 1
            if done % 20 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} days fetched ({filled} with data)")

    for y, archive in archives.items():
        if not archive:
            continue
        changed = r2_put_json(r2, archive, f"{ARCHIVE_PFX}/daily_{y}.json")
        print(f"daily_{y}.json: {'uploaded' if changed else 'unchanged'} ({len(archive)} stations)")

    print(f"\nBackfill complete: {filled}/{len(todo)} attempted days had data "
          f"({len(dates) - len(todo)} days were already archived).")


if __name__ == "__main__":
    main()
