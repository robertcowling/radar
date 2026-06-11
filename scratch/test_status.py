#!/usr/bin/env python3
"""
scratch/test_status.py — Generates realistic mock data in status/history.json for testing the dashboard.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# Resolve base dir
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE_DIR)


def run_report(job_name, status, run_id, timestamp=None):
    env = os.environ.copy()
    env["JOB_NAME"] = job_name
    env["JOB_STATUS"] = status
    env["GITHUB_RUN_ID"] = str(run_id)
    env["GITHUB_REPOSITORY"] = "robertcowling/radar"
    
    # Force local mode (ignore R2 secrets for the local mock generation)
    env.pop("R2_ACCOUNT_ID", None)
    env.pop("R2_ACCESS_KEY_ID", None)
    env.pop("R2_SECRET_ACCESS_KEY", None)
    env.pop("R2_BUCKET_NAME", None)

    # If a specific timestamp is requested, we will override report_job_status.py behavior by patching
    # or we can just run the script. Since report_job_status.py uses current time, we can run it sequentially.
    # To simulate history, we'll let it generate sequential runs.
    subprocess.run([sys.executable, "report_job_status.py"], env=env, check=True)


def main():
    print("Generating local mock status history...")
    
    # Reset/remove old history
    local_history_path = os.path.join("status", "history.json")
    if os.path.exists(local_history_path):
        os.remove(local_history_path)
        print("Cleared existing local history.json.")

    # 1. Rain Gauges: Operational (30 successes)
    print("Generating status for 'Rain Gauges' (Operational)...")
    for i in range(30):
        run_report("Rain Gauges", "success", 1000 + i)

    # 2. Met Office Radar (Parallel): Degraded (28 successes, 2 failures at the end)
    print("Generating status for 'Met Office Radar (Parallel)' (Degraded)...")
    for i in range(28):
        run_report("Met Office Radar (Parallel)", "success", 2000 + i)
    run_report("Met Office Radar (Parallel)", "failure", 2028)
    run_report("Met Office Radar (Parallel)", "failure", 2029)

    # 3. OPERA Radar (5m): Outage (26 successes, 4 failures at the end)
    print("Generating status for 'OPERA Radar (5m)' (Outage)...")
    for i in range(26):
        run_report("OPERA Radar (5m)", "success", 3000 + i)
    for i in range(4):
        run_report("OPERA Radar (5m)", "failure", 3026 + i)

    # 4. Weather Warnings: Operational (29 successes, 1 cancelled)
    print("Generating status for 'Weather Warnings' (Operational)...")
    for i in range(29):
        run_report("Weather Warnings", "success", 4000 + i)
    run_report("Weather Warnings", "cancelled", 4029)

    # 5. GFS MSLP Forecast: Operational (5 successes)
    print("Generating status for 'GFS MSLP Forecast' (Operational)...")
    for i in range(5):
        run_report("GFS MSLP Forecast", "success", 5000 + i)

    # 6. Met Office UKV Forecast: Operational (10 successes)
    print("Generating status for 'Met Office UKV Forecast' (Operational)...")
    for i in range(10):
        run_report("Met Office UKV Forecast", "success", 6000 + i)

    # 7. Rapid Flood Guidance: Operational (15 successes)
    print("Generating status for 'Rapid Flood Guidance' (Operational)...")
    for i in range(15):
        run_report("Rapid Flood Guidance", "success", 7000 + i)

    # 8. Gauge Quality Control: Operational (3 successes)
    print("Generating status for 'Gauge Quality Control' (Operational)...")
    for i in range(3):
        run_report("Gauge Quality Control", "success", 8000 + i)

    print("\nMock data generation complete!")
    print(f"Status history written to: {os.path.abspath(local_history_path)}")


if __name__ == "__main__":
    main()
