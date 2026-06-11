#!/usr/bin/env python3
"""
report_job_status.py — Reports GitHub Actions workflow job status to R2.

This script is run at the end of key workflows. It downloads status/history.json
from R2, appends/updates the current run's status, trims history to the last 30
runs, and uploads the file back to R2.
"""

import json
import os
import sys
from datetime import datetime, timezone

# ── Cloudflare R2 Config ──────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

STATUS_KEY = "status/history.json"
ROLLING_LIMIT = 30


def get_r2_client():
    if not USE_R2:
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    except ImportError:
        print("Warning: boto3 not installed. Cloudflare R2 will be bypassed.")
        return None


def main():
    job_name = os.environ.get("JOB_NAME")
    job_status = os.environ.get("JOB_STATUS")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not job_name or not job_status:
        print("Error: JOB_NAME and JOB_STATUS environment variables are required.")
        sys.exit(1)

    # Normalize status to lowercase (success, failure, cancelled, etc.)
    job_status = job_status.lower()

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Reporting status for job: '{job_name}' | status: '{job_status}' | run: '{run_id}'")

    # Connect to R2 or fallback locally
    r2 = get_r2_client()
    status_data = {"jobs": {}}

    # Load existing status
    if r2:
        try:
            obj = r2.get_object(Bucket=R2_BUCKET, Key=STATUS_KEY)
            status_data = json.loads(obj["Body"].read())
            print("Successfully loaded existing status/history.json from R2.")
        except Exception as e:
            print(f"No existing status/history.json found in R2 or error reading: {e}. Starting fresh.")
    else:
        # Local fallback file for testing
        local_path = os.path.join("status", "history.json")
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    status_data = json.load(f)
                print(f"Loaded existing status from local file: {local_path}")
            except Exception as e:
                print(f"Error reading local file: {e}")

    # Prepare job record
    jobs = status_data.setdefault("jobs", {})
    job_entry = jobs.setdefault(job_name, {
        "last_run": "",
        "last_status": "",
        "history": []
    })

    # Update latest run details
    job_entry["last_run"] = now_iso
    job_entry["last_status"] = job_status

    # Append to rolling history
    history = job_entry.setdefault("history", [])
    history.insert(0, {
        "timestamp": now_iso,
        "status": job_status,
        "run_id": run_id,
        "repo": repo
    })

    # Trim to ROLLING_LIMIT
    job_entry["history"] = history[:ROLLING_LIMIT]

    # Save/upload status
    if r2:
        try:
            body = json.dumps(status_data, separators=(",", ":")).encode("utf-8")
            r2.put_object(
                Bucket=R2_BUCKET,
                Key=STATUS_KEY,
                Body=body,
                ContentType="application/json; charset=utf-8"
            )
            print("Successfully uploaded updated status/history.json to R2.")
        except Exception as e:
            print(f"Error uploading status to R2: {e}")
            sys.exit(1)
    else:
        # Save locally
        local_path = os.path.join("status", "history.json")
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(status_data, f, indent=2)
            print(f"Saved updated status locally: {local_path}")
        except Exception as e:
            print(f"Error saving status locally: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
