#!/usr/bin/env python3
"""One-off backfill: patch last 48 h of run archives with new QC checks.

For each run in the last 48 h:
  - Removes old `nearest_neighbour` check key
  - Adds `range_check` (RC) and `spatial` (SCC) if not already present
  - Recomputes QI from the updated checks
  - Re-uploads the patched run archive

Then rebuilds the event log from the patched runs so old-style entries
(with nearest_neighbour) are gone.

Usage:
  python tools/backfill_qc.py

Requires the same R2 env vars as gauge_qc.py.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timedelta, timezone

from gauge_qc import (
    get_r2, r2_get_json, r2_put_json,
    R2_BUCKET, MANIFEST_KEY, RUNS_KEY_TPL, LOG_KEY, STATIONS_KEY,
    LOG_RETENTION_HRS,
    check_range, check_spatial_consistency, compute_qi,
)

BACKFILL_HRS = 48


def parse_run_ts(ts):
    try:
        return datetime.strptime(ts, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def main():
    if not R2_BUCKET:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    r2  = get_r2()
    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(hours=BACKFILL_HRS)

    # Station registry (needed for SCC lat/lon lookups)
    print("Loading stations...")
    sdata = r2_get_json(r2, STATIONS_KEY)
    if not sdata:
        print("Could not load stations.json — aborting.")
        sys.exit(1)
    stations = sdata.get("stations", {})
    print(f"  {len(stations)} stations")

    # Manifest → runs within window, sorted oldest-first
    print("Loading manifest...")
    manifest  = r2_get_json(r2, MANIFEST_KEY, {"runs": []})
    all_runs  = manifest.get("runs", [])
    runs_in_window = sorted(
        (dt, ts)
        for ts in all_runs
        for dt in (parse_run_ts(ts),)
        if dt and dt >= cutoff_dt
    )
    print(f"  {len(runs_in_window)} runs in last {BACKFILL_HRS} h to patch")

    # Patch each run archive
    patched = []
    for run_dt, run_ts in runs_in_window:
        key = RUNS_KEY_TPL.format(ts=run_ts)
        run = r2_get_json(r2, key)
        if not run:
            print(f"  SKIP {run_ts} (not found)")
            continue

        month = run_dt.month

        # Reconstruct latest_readings for this slot from the stored summary.
        # summary contains every station that was checked at this run time.
        latest_readings = {
            e["id"]: e["mm"]
            for e in run.get("summary", [])
            if "id" in e and "mm" in e
        }

        # Build quick summary index for QI updates
        summary_by_id = {
            e["id"]: e
            for e in run.get("summary", [])
            if "id" in e
        }

        n_changed = 0
        for g in run.get("gauges", []):
            sid   = g["station_id"]
            val   = g.get("latest_value_mm", 0.0)
            checks = g.get("checks", {})

            # Remove old-style nearest_neighbour key
            checks.pop("nearest_neighbour", None)

            # Add range_check (RC) if absent
            if "range_check" not in checks:
                checks["range_check"] = check_range(val, month)

            # Add spatial (SCC) if absent and station is registered
            if "spatial" not in checks and sid in stations:
                checks["spatial"] = check_spatial_consistency(
                    val, sid, stations, latest_readings
                )

            new_qi = compute_qi(checks)
            g["checks"] = checks
            g["qi"]     = new_qi

            # Sync QI into the summary entry if it was flagged
            se = summary_by_id.get(sid)
            if se and "f" in se:
                se["qi"] = new_qi

            n_changed += 1

        r2_put_json(r2, key, run)
        print(f"  {run_ts}: {n_changed} gauges patched")
        patched.append((run_dt, run_ts, run))

    # Rebuild the event log from patched runs (oldest → newest so last_seen
    # ends up as the most recent occurrence of each flagging episode)
    print("\nRebuilding log from patched runs...")
    log_cutoff = (now - timedelta(hours=LOG_RETENTION_HRS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = {}

    for run_dt, run_ts, run in patched:
        run_now_str = run.get("generated_at",
                              run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        for g in run.get("gauges", []):
            ev_key = (g["station_id"], g["first_flagged_at"])
            events[ev_key] = {
                "station_id":        g["station_id"],
                "name":              g["name"],
                "county":            g.get("county"),
                "lat":               g["lat"],
                "lon":               g["lon"],
                "first_flagged_at":  g["first_flagged_at"],
                "last_seen":         run_now_str,
                "consecutive_flags": g["consecutive_flags"],
                "checks_flag":       g["checks_flag"],
                "qi":                g.get("qi"),
                "latest_value_mm":   g["latest_value_mm"],
                "checks":            g["checks"],
                "llm_verdict":       g.get("llm_verdict"),
                "llm_reasoning":     g.get("llm_reasoning"),
                "accums":            g.get("accums", {}),
            }

    # Trim to retention window
    events = {k: v for k, v in events.items() if v["last_seen"] > log_cutoff}
    log_data = {
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": sorted(events.values(),
                         key=lambda e: e["last_seen"], reverse=True),
    }
    r2_put_json(r2, LOG_KEY, log_data)
    print(f"Log rebuilt: {len(log_data['events'])} events.")
    print("Done.")


if __name__ == "__main__":
    main()
