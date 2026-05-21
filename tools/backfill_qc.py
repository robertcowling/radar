#!/usr/bin/env python3
"""One-off backfill: patch last 48 h of run archives with new QC checks.

For each run in the last 48 h:
  - Removes old `nearest_neighbour` check key
  - Adds `range_check` (RC) and `spatial` (SCC) if not already present
  - Re-runs `drip_leak` from raw slot history using the current parameters
    (6-hour window, 18/24 non-zero threshold)
  - Recomputes QI from the updated checks
  - Re-uploads the patched run archive

Then rebuilds the event log from the patched runs.

Usage:
  python tools/backfill_qc.py

Requires the same R2 env vars as gauge_qc.py.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

from gauge_qc import (
    get_r2, r2_get_json, r2_put_json,
    R2_BUCKET, MANIFEST_KEY, RUNS_KEY_TPL, LOG_KEY, STATIONS_KEY,
    READINGS_PFX, LOG_RETENTION_HRS, PZERO_WINDOW_SLOTS,
    check_range, check_spatial_consistency, check_drip_leak, compute_qi,
    find_neighbours, get_station_slots,
    DRIP_WINDOW_SLOTS,
)

BACKFILL_HRS = 48
PRELOAD_DAYS = 4  # covers 54-hour lookback from the oldest run in the window


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

    # Station registry (needed for SCC and drip neighbour lookups)
    print("Loading stations...")
    sdata = r2_get_json(r2, STATIONS_KEY)
    if not sdata:
        print("Could not load stations.json — aborting.")
        sys.exit(1)
    stations = sdata.get("stations", {})
    print(f"  {len(stations)} stations")

    # Pre-load raw readings for PRELOAD_DAYS days.
    # This covers any 6-hour drip lookback from the oldest run in the window.
    print(f"Pre-loading {PRELOAD_DAYS} days of gauge readings...")
    days = {}
    for delta in range(PRELOAD_DAYS):
        d = now - timedelta(days=delta)
        date_str = d.strftime("%Y%m%d")
        data = r2_get_json(r2, f"{READINGS_PFX}/{date_str}.json")
        days[date_str] = data.get("readings", {}) if data else {}
        print(f"  {date_str}: {len(days[date_str])} stations")

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

        month           = run_dt.month
        latest_slot     = run_dt.hour * 4 + run_dt.minute // 15
        latest_date_str = run_dt.strftime("%Y%m%d")

        # Reconstruct latest_readings for this slot from the stored summary
        latest_readings = {
            e["id"]: e["mm"]
            for e in run.get("summary", [])
            if "id" in e and "mm" in e
        }

        # Quick summary index for QI sync
        summary_by_id = {
            e["id"]: e
            for e in run.get("summary", [])
            if "id" in e
        }

        n_changed = 0
        for g in run.get("gauges", []):
            sid = g["station_id"]
            val = g.get("latest_value_mm", 0.0)
            checks = g.get("checks", {})

            # Remove old-style key
            checks.pop("nearest_neighbour", None)

            # Add range_check (RC) if absent
            if "range_check" not in checks:
                checks["range_check"] = check_range(val, month)

            # Add spatial (SCC) if absent and station is registered
            if "spatial" not in checks and sid in stations:
                checks["spatial"] = check_spatial_consistency(
                    val, sid, stations, latest_readings
                )

            # Always re-run drip_leak from raw slot history (new 6-hr window)
            history_drip = get_station_slots(
                days, sid, latest_date_str, latest_slot, DRIP_WINDOW_SLOTS
            )
            _nbr_mean = None
            if sid in stations:
                neighbours = find_neighbours(sid, stations, latest_readings)
                nbr_means = []
                for nbr_id, _, _, _ in neighbours:
                    h = get_station_slots(
                        days, nbr_id, latest_date_str, latest_slot, PZERO_WINDOW_SLOTS
                    )
                    valid_vals = [v for v in h if v is not None and v >= 0]
                    nbr_means.append(
                        sum(valid_vals) / len(valid_vals) if valid_vals else None
                    )
                valid_means = [v for v in nbr_means if v is not None]
                _nbr_mean = sum(valid_means) / len(valid_means) if valid_means else None
            checks["drip_leak"] = check_drip_leak(history_drip, nbr_mean_mm=_nbr_mean)

            new_qi = compute_qi(checks)
            g["checks"] = checks
            g["qi"]     = new_qi

            # Sync QI into the flagged summary entry
            se = summary_by_id.get(sid)
            if se and "f" in se:
                se["qi"] = new_qi

            n_changed += 1

        r2_put_json(r2, key, run)
        print(f"  {run_ts}: {n_changed} gauges patched")
        patched.append((run_dt, run_ts, run))

    # Rebuild the event log from patched runs (oldest → newest)
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
