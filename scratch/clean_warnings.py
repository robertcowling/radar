import json
import os
import re
import boto3

# 1. Load env variables from tools/.env
env_vars = {}
env_path = "tools/.env"
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    env_vars[parts[0].strip()] = parts[1].strip()

r2_acc_id = env_vars.get("R2_ACCOUNT_ID", "")
r2_bucket = env_vars.get("R2_BUCKET_NAME", "")
r2_key_id = env_vars.get("R2_ACCESS_KEY_ID", "")
r2_secret = env_vars.get("R2_SECRET_ACCESS_KEY", "")

if not all([r2_acc_id, r2_bucket, r2_key_id, r2_secret]):
    print("Error: Missing R2 credentials in tools/.env")
    exit(1)

# Connect to R2 client
r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{r2_acc_id}.r2.cloudflarestorage.com",
    aws_access_key_id=r2_key_id,
    aws_secret_access_key=r2_secret,
    region_name="auto",
)

TARGET_ID = "meteo_6a99161b-e46a-49b2-9092-7d7c9f22e264"
target_warning = None

# A. Clean local warnings first to grab the target warning
local_archive_path = "warnings/archive/warnings_20260527.json"
if os.path.exists(local_archive_path):
    with open(local_archive_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Filter target warning
    filtered = [w for w in data if w["alert_id"] == TARGET_ID]
    if filtered:
        target_warning = filtered[0]
        
    print(f"Found target warning locally: {target_warning is not None}")
    
    # Write back clean local archive
    with open(local_archive_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, separators=(",", ":"))
    print("Cleaned local archive file warnings_20260527.json")

# Delete any other daily archive files locally that do not contain the target warning
archive_dir = "warnings/archive"
if os.path.exists(archive_dir):
    for f in os.listdir(archive_dir):
        if f.startswith("warnings_") and f.endswith(".json"):
            fpath = os.path.join(archive_dir, f)
            if f != "warnings_20260527.json":
                try:
                    os.remove(fpath)
                    print(f"Deleted local archive file: {f}")
                except Exception as e:
                    print(f"Error deleting local archive {f}: {e}")

# Clean local warnings_latest.json
local_latest_path = "warnings/warnings_latest.json"
if os.path.exists(local_latest_path):
    with open(local_latest_path, "r", encoding="utf-8") as f:
        latest = json.load(f)
    
    latest["warnings"] = []
    with open(local_latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, separators=(",", ":"))
    print("Cleaned local warnings_latest.json")

# B. Clean R2 bucket
print("\nScanning Cloudflare R2 bucket warnings/ keys...")
paginator = r2.get_paginator("list_objects_v2")
pages = paginator.paginate(Bucket=r2_bucket, Prefix="warnings/")

for page in pages:
    for obj in page.get("Contents", []):
        key = obj["Key"]
        print(f"Checking R2 key: {key}")
        
        if key == "warnings/warnings_latest.json":
            # Download and clean warnings_latest.json on R2
            resp = r2.get_object(Bucket=r2_bucket, Key=key)
            latest_r2 = json.loads(resp["Body"].read().decode("utf-8"))
            latest_r2["warnings"] = []
            
            # Put back
            r2.put_object(
                Bucket=r2_bucket,
                Key=key,
                Body=json.dumps(latest_r2, separators=(",", ":")).encode("utf-8"),
                ContentType="application/json; charset=utf-8"
            )
            print(f"  Cleaned and uploaded {key} to R2")
            
        elif key == "warnings/archive/warnings_20260527.json":
            # Filter and upload warnings_20260527.json on R2
            resp = r2.get_object(Bucket=r2_bucket, Key=key)
            archive_r2 = json.loads(resp["Body"].read().decode("utf-8"))
            filtered_r2 = [w for w in archive_r2 if w["alert_id"] == TARGET_ID]
            
            # If target warning wasn't on R2 but we have it locally, use the local copy!
            if not filtered_r2 and target_warning:
                filtered_r2 = [target_warning]
                
            r2.put_object(
                Bucket=r2_bucket,
                Key=key,
                Body=json.dumps(filtered_r2, separators=(",", ":")).encode("utf-8"),
                ContentType="application/json; charset=utf-8"
            )
            print(f"  Cleaned and uploaded {key} to R2")
            
        elif key.startswith("warnings/archive/warnings_") and key.endswith(".json"):
            # Delete any other warnings daily archives from R2 to clean up test/wrong data
            r2.delete_object(Bucket=r2_bucket, Key=key)
            print(f"  Deleted old/wrong archive key from R2: {key}")

print("\nCleanup completed successfully!")
