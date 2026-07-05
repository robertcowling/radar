#!/usr/bin/env python3
"""
upload_flood_history.py — one-off upload of a locally-rebuilt flood-history
dataset to R2, for when build_flood_stats.py has been re-run manually
(rather than incrementally via update_flood_stats.py).

Usage:
  python upload_flood_history.py <outdir>

<outdir> must contain area_stats.json, overall_stats.json,
events_master.csv.gz, code_aliases.csv (the output of build_flood_stats.py).
"""

import json
import os
import sys
from pathlib import Path

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")

FILES = {
    "area_stats.json":       ("flood_history/area_stats.json", "application/json; charset=utf-8"),
    "overall_stats.json":    ("flood_history/overall_stats.json", "application/json; charset=utf-8"),
    "events_master.csv.gz":  ("flood_history/events_master.csv.gz", "text/csv"),
    "code_aliases.csv":      ("flood_history/code_aliases.csv", "text/csv"),
}


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    outdir = Path(sys.argv[1])

    missing = [os.environ.get(n) is None or os.environ.get(n) == "" for n in
               ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")]
    if any(missing):
        print("R2 env vars not set (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / "
              "R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME). Aborting.", file=sys.stderr)
        sys.exit(1)

    import boto3
    r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

    for fname, (key, content_type) in FILES.items():
        path = outdir / fname
        if not path.exists():
            print(f"  ! missing {path}, skipping", file=sys.stderr)
            continue
        body = path.read_bytes()
        extra = {"ContentEncoding": "gzip"} if fname.endswith(".gz") else {}
        r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body, ContentType=content_type, **extra)
        print(f"  uploaded {key} ({len(body):,} bytes)")

    print("Done.")


if __name__ == "__main__":
    main()
