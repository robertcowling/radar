#!/usr/bin/env python3
"""
scratch/fix_june26.py — Repair June 26th (20260626) rain readings in R2.
"""
import os
import sys
from datetime import datetime, timezone

# Ensure parent dir is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch_rain import (
    get_r2,
    r2_get_json,
    r2_put_json,
    fetch_stations,
    fetch_readings_for_date,
    parse_readings,
    fetch_nrw_data,
    META_PATH,
    R2_READINGS_PFX,
    R2_PUBLIC_URL,
    USE_R2,
)

def main():
    if not USE_R2:
        print("Error: R2 credentials not set in environment.")
        sys.exit(1)

    r2 = get_r2()
    date_str = "2026-06-26"
    date_key = "20260626"

    print(f"--- Restoring {date_key} ---")
    stations, suid_to_ref = fetch_stations()
    items = fetch_readings_for_date(date_str)
    by_date, _ = parse_readings(items, suid_to_ref)

    try:
        _, nrw_by_date, _ = fetch_nrw_data()
        for dk, station_slots in nrw_by_date.items():
            if dk == date_key:
                for sid, slots in station_slots.items():
                    by_date.setdefault(dk, {}).setdefault(sid, {}).update(slots)
    except Exception as e:
        print(f"Warning fetching NRW data: {e}")

    readings_map = by_date.get(date_key, {})
    print(f"Built readings for {date_key}: {len(readings_map)} stations")

    r2_key = f"{R2_READINGS_PFX}/{date_key}.json"
    day_obj = {"date": date_key, "readings": readings_map}
    r2_put_json(r2, r2_key, day_obj)
    print(f"Successfully uploaded {r2_key} with {len(readings_map)} stations to R2!")

    # Ensure meta.json includes 20260626
    meta = r2_get_json(r2, META_PATH, {"available_days": []})
    days = set(meta.get("available_days", []))
    days.add(date_key)
    meta["available_days"] = sorted(days)
    meta["r2_base_url"] = R2_PUBLIC_URL
    r2_put_json(r2, META_PATH, meta)
    print("Updated rain/meta.json in R2.")

if __name__ == "__main__":
    main()
