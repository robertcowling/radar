#!/usr/bin/env python3
"""Reset gaugecheck log and results to empty state in R2.

Run once after a code change when you want a clean slate.
Requires the same R2 env vars as gauge_qc.py.
"""
import json, os, sys
from datetime import datetime, timezone
import boto3

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET     = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET_NAME", "")

if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET, R2_BUCKET]):
    print("R2 credentials not set — aborting.")
    sys.exit(1)

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET,
    region_name="auto",
)

now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

empty_log = {"updated_at": now, "events": []}
body = json.dumps(empty_log, separators=(",", ":")).encode()
r2.put_object(Bucket=R2_BUCKET, Key="gaugecheck/log.json", Body=body,
              ContentType="application/json; charset=utf-8")
print("gaugecheck/log.json reset.")
