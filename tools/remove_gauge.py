"""
Remove a named gauge from results.json, log.json, and all run archives.
Usage: python remove_gauge.py "Great Gidding"
Requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME in the environment.
"""
import boto3, json, os, sys, re

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
BUCKET           = os.environ.get("R2_BUCKET_NAME", "")

if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, BUCKET]):
    sys.exit("Missing R2 credentials — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
              "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME in the environment.")

r2 = boto3.client("s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto")

name_pat = sys.argv[1] if len(sys.argv) > 1 else "Great Gidding"
print(f"Removing gauge matching: '{name_pat}'")

def r2_get(key):
    try:
        return json.loads(r2.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except Exception as e:
        print(f"  ERROR fetching {key}: {e}"); return None

def r2_put(key, data):
    r2.put_object(Bucket=BUCKET, Key=key,
                  Body=json.dumps(data, separators=(',',':')),
                  ContentType="application/json",
                  CacheControl="no-cache, no-store, must-revalidate")
    print(f"  Written {key}")

def matches(name):
    return name_pat.lower() in (name or '').lower()

# Collect station IDs to purge (from run archives)
purge_ids = set()

# Scan run archives for the station ID
paginator = r2.get_paginator("list_objects_v2")
print("\nScanning run archives...")
run_keys = []
for page in paginator.paginate(Bucket=BUCKET, Prefix="gaugecheck/runs/"):
    for obj in page.get("Contents", []):
        run_keys.append(obj["Key"])
print(f"  Found {len(run_keys)} run files")

for key in run_keys:
    data = r2_get(key)
    if not data: continue
    gauges = data.get("gauges", [])
    found = [g for g in gauges if matches(g.get("name",""))]
    if found:
        for g in found:
            purge_ids.add(g["station_id"])
        data["gauges"] = [g for g in gauges if not matches(g.get("name",""))]
        r2_put(key, data)

# Also scan summary array in run files for the flag marker — update f field
print(f"\nStation IDs found: {purge_ids}")

# Clean results.json
results = r2_get("gaugecheck/results.json")
if results:
    before = len(results.get("gauges", []))
    results["gauges"] = [g for g in results.get("gauges", [])
                         if not matches(g.get("name","")) and g.get("station_id") not in purge_ids]
    for s in results.get("summary", []):
        if s.get("id") in purge_ids and s.get("f"):
            del s["f"]
    print(f"\nresults.json: {before} -> {len(results['gauges'])} flagged gauges")
    r2_put("gaugecheck/results.json", results)

# Clean log.json
log = r2_get("gaugecheck/log.json")
if log:
    before = len(log.get("events", []))
    log["events"] = [e for e in log.get("events", [])
                     if not matches(e.get("name","")) and e.get("station_id") not in purge_ids]
    print(f"\nlog.json: {before} -> {len(log['events'])} events")
    r2_put("gaugecheck/log.json", log)

print("\nDone.")
