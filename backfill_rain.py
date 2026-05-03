#!/usr/bin/env python3
"""
backfill_rain.py — One-time 30-day historic data loader.

Downloads EA archive CSVs and uploads to R2 as daily JSON files.
Run once via manual workflow_dispatch or locally with R2 env vars set.

Uses fetch_rain.py helpers (must be in the same directory).
"""

import csv
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from fetch_rain import (
    RETENTION_DAYS,
    META_PATH,
    STATIONS_PATH,
    R2_READINGS_PFX,
    R2_BUCKET,
    R2_PUBLIC_URL,
    USE_R2,
    get_r2,
    r2_put_json,
    station_id_from_measure,
    dt_to_slot,
    fetch_stations,
    write_local_json,
)

EA_ARCHIVE_BASE = "https://environment.data.gov.uk/flood-monitoring/archive"


def fetch_archive_day(date_str):
    """Download EA archive CSV for YYYY-MM-DD. Returns list of (dt_str, station_id, value)."""
    url = f"{EA_ARCHIVE_BASE}/readings-{date_str}.csv"
    print(f"  Downloading {url}...")
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/csv"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Warning: could not download {url}: {e}")
        return []

    rows = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        measure = row.get("measure", "")
        if "-rainfall-" not in measure:
            continue
        dt_str  = (row.get("dateTime") or "").strip()
        raw_val = (row.get("value") or "").strip()
        if not dt_str or not raw_val:
            continue
        try:
            val = float(raw_val)
        except ValueError:
            continue
        if val < 0:
            continue
        station = station_id_from_measure(measure)
        if station:
            rows.append((dt_str, station, val))
    return rows


def build_day_obj(date_key, rows):
    readings = {}
    for dt_str, station, val in rows:
        _, slot = dt_to_slot(dt_str)
        readings.setdefault(station, {})[str(slot)] = round(val, 2)
    return {"date": date_key, "readings": readings}


def main():
    now = datetime.now(timezone.utc)
    os.makedirs("rain", exist_ok=True)
    r2 = get_r2() if USE_R2 else None
    if not USE_R2:
        print("Warning: R2 env vars not set — dry run only")

    # ── Stations ──────────────────────────────────────────────────────────────
    stations = fetch_stations()
    stations_data = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stations": stations}
    write_local_json(STATIONS_PATH, stations_data)
    if USE_R2:
        r2_put_json(r2, "rain/stations.json", stations_data)
        print(f"Uploaded stations.json ({len(stations)} stations)")

    # ── Download and upload archive CSVs ──────────────────────────────────────
    available_days = []
    latest_dt = None
    end_date   = now.date()
    start_date = end_date - timedelta(days=RETENTION_DAYS - 1)

    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        date_key = current.strftime("%Y%m%d")

        rows = fetch_archive_day(date_str)
        if rows:
            day_obj = build_day_obj(date_key, rows)
            r2_key  = f"{R2_READINGS_PFX}/{date_key}.json"
            if USE_R2:
                try:
                    r2_put_json(r2, r2_key, day_obj)
                    print(f"  Uploaded {r2_key} ({len(day_obj['readings'])} stations)")
                except Exception as e:
                    print(f"  Warning: {r2_key} upload failed: {e}")
            else:
                print(f"  (dry run) {r2_key}: {len(day_obj['readings'])} stations")
            available_days.append(date_key)
            for dt_str, _, _ in rows:
                if latest_dt is None or dt_str > latest_dt:
                    latest_dt = dt_str
        else:
            print(f"  No data for {date_str}")

        current += timedelta(days=1)
        time.sleep(0.5)  # be polite to the EA API

    # ── Write meta ────────────────────────────────────────────────────────────
    meta = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest_time": latest_dt or "",
        "available_days": sorted(available_days),
        "r2_base_url": R2_PUBLIC_URL,
    }
    write_local_json(META_PATH, meta)
    if USE_R2:
        r2_put_json(r2, "rain/meta.json", meta)
    print(f"\nBackfill complete: {len(available_days)} days, latest={latest_dt}")


if __name__ == "__main__":
    main()
