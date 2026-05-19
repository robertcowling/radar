"""
purge_ukv_run.py — Remove a specific UKV run from R2 and ukv_meta.json.

Usage:
    python purge_ukv_run.py                    # purge latest 06Z run from meta
    python purge_ukv_run.py 20260516T0600Z     # purge specific run_ts

Required env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
              R2_BUCKET_NAME, R2_PUBLIC_BASE_URL
"""
import json
import os
import sys
from datetime import datetime

import boto3
from botocore.client import Config


R2_ACCOUNT_ID    = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET        = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET        = os.environ["R2_BUCKET_NAME"]

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


def load_meta():
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key="ukv_meta.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return {"runs": []}


def delete_prefix(prefix):
    """Delete all R2 objects under a given prefix."""
    deleted, cont = 0, None
    while True:
        kwargs = {"Bucket": R2_BUCKET, "Prefix": prefix}
        if cont:
            kwargs["ContinuationToken"] = cont
        resp = r2.list_objects_v2(**kwargs)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
        if keys:
            r2.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": keys})
            deleted += len(keys)
            print(f"  Deleted {len(keys)} objects under {prefix}")
        if not resp.get("IsTruncated"):
            break
        cont = resp["NextContinuationToken"]
    if deleted == 0:
        print(f"  No objects found under {prefix}")
    return deleted


def main():
    meta = load_meta()
    runs = meta.get("runs", [])

    if not runs:
        print("ukv_meta.json has no runs — nothing to do.")
        sys.exit(0)

    # Determine target run_ts
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        # Find most recent 06Z run in the meta
        target = None
        for r in runs:
            rt = r.get("run_ts", "")
            # parse hour: "20260516T0600Z" → 6
            try:
                for fmt in ("%Y%m%dT%H%MZ", "%Y%m%dT%HZ"):
                    try:
                        dt = datetime.strptime(rt, fmt)
                        break
                    except ValueError:
                        continue
                if dt.hour == 6:
                    target = rt
                    break
            except Exception:
                continue
        if not target:
            print("No 06Z run found in meta. Available runs:")
            for r in runs:
                print(" ", r.get("run_ts"), r.get("run_label"))
            sys.exit(1)

    # Confirm the run exists in meta
    matching = [r for r in runs if r.get("run_ts") == target]
    if not matching:
        print(f"Run {target} not found in ukv_meta.json. Available runs:")
        for r in runs:
            print(" ", r.get("run_ts"), r.get("run_label"))
        sys.exit(1)

    print(f"\nTarget run: {target}")
    print(f"  Label: {matching[0].get('run_label','?')}  Type: {matching[0].get('run_type','?')}")
    print()

    # Delete R2 objects for this run
    print("Deleting R2 objects...")
    delete_prefix(f"ukv/{target}/")
    delete_prefix(f"ukv_poly/{target}/")

    # Remove from meta and write back
    new_runs = [r for r in runs if r.get("run_ts") != target]
    meta["runs"] = new_runs
    body = json.dumps(meta, indent=2).encode()
    r2.put_object(
        Bucket=R2_BUCKET,
        Key="ukv_meta.json",
        Body=body,
        ContentType="application/json; charset=utf-8",
    )
    print(f"\nRemoved {target} from ukv_meta.json ({len(runs)} -> {len(new_runs)} runs).")
    print("Done. Trigger the UKV workflow to re-process.")


if __name__ == "__main__":
    main()
