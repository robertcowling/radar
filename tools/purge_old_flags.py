"""
Purge gauge flags identified before a given cutoff from log.json and results.json.
Usage: python purge_old_flags.py [YYYY-MM-DDTHH:MM:SSZ]
Default cutoff: today at 06:00 UTC
"""
import boto3, json, sys
from datetime import datetime, timezone

r2 = boto3.client("s3",
    endpoint_url="https://495181df96d1d46ade158238a67fd89b.r2.cloudflarestorage.com",
    aws_access_key_id="e5a76e234c48aa43eac06611ef788ac8",
    aws_secret_access_key="90928614a301b79d5e6a229fa2e6306c9516fae45c85cf0d9b3b90f0c45b989a",
    region_name="auto")
BUCKET = "radarrainfall"

def r2_get(key):
    try:
        obj = r2.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        print(f"  ERROR fetching {key}: {e}")
        return None

def r2_put(key, data):
    r2.put_object(Bucket=BUCKET, Key=key,
                  Body=json.dumps(data, separators=(',',':')),
                  ContentType="application/json",
                  CacheControl="no-cache, no-store, must-revalidate")
    print(f"  Written {key}")

cutoff_str = sys.argv[1] if len(sys.argv) > 1 else "2026-05-20T06:00:00Z"
cutoff = datetime.strptime(cutoff_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
print(f"Cutoff: {cutoff.isoformat()}")

# ── 1. Load log.json ──────────────────────────────────────────────────────────
log = r2_get("gaugecheck/log.json")
if not log:
    print("Could not load log.json — aborting")
    sys.exit(1)

events_before = log.get("events", [])
events_after  = [e for e in events_before
                 if datetime.strptime(e["first_flagged_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc) >= cutoff]
purged_ids = {e["station_id"] for e in events_before} - {e["station_id"] for e in events_after}

print(f"\nlog.json: {len(events_before)} -> {len(events_after)} events "
      f"(removed {len(events_before)-len(events_after)})")
if purged_ids:
    print("  Purged station IDs:", purged_ids)

log["events"] = events_after
r2_put("gaugecheck/log.json", log)

# ── 2. Load results.json ──────────────────────────────────────────────────────
results = r2_get("gaugecheck/results.json")
if not results:
    print("Could not load results.json — skipping")
    sys.exit(0)

gauges_before = results.get("gauges", [])
gauges_after  = [g for g in gauges_before if g["station_id"] not in purged_ids]

print(f"\nresults.json: {len(gauges_before)} -> {len(gauges_after)} flagged gauges")

# Rebuild summary flags
flagged_ids  = {g["station_id"] for g in gauges_after}
elevated_ids = {g["station_id"] for g in gauges_after if g.get("checks_flag") == "ELEVATED"}
flagged_hard = {g["station_id"] for g in gauges_after if g.get("checks_flag") == "FLAGGED"}

for s in results.get("summary", []):
    if s["id"] not in flagged_ids and s.get("f"):
        del s["f"]   # clear flag marker on summary entries for removed gauges

results["gauges"] = gauges_after
results["total_flagged"]  = len([g for g in gauges_after if g.get("checks_flag") == "FLAGGED"])
results["total_elevated"] = len([g for g in gauges_after if g.get("checks_flag") == "ELEVATED"])

r2_put("gaugecheck/results.json", results)

print("\nDone.")
