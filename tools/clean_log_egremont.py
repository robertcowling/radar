import os
import json
import boto3

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
LOG_KEY          = "gaugecheck/log.json"

def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

def r2_get_json(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        print(f"Error loading {key}: {e}")
        return default

def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")

def main():
    r2 = get_r2()
    print("Downloading log.json...")
    log_data = r2_get_json(r2, LOG_KEY)
    if not log_data:
        print("Could not load log.json!")
        return

    events = log_data.get("events", [])
    print(f"Loaded {len(events)} events.")

    updated = False
    for ev in events:
        # Find Egremont Ivy Hill event
        if ev["station_id"] == "592158":
            print(f"Found Egremont Ivy Hill event: {ev['first_flagged_at']} -> {ev['last_seen']} ({ev['latest_value_mm']} mm, consecutive={ev['consecutive_flags']})")
            
            # Re-align to the correct, actual 2.1 mm slot: 2026-05-22T23:45:00Z
            ev["first_flagged_at"] = "2026-05-22T23:45:00Z"
            ev["last_seen"] = "2026-05-22T23:45:00Z"
            ev["latest_slot_time"] = "2026-05-22T23:45:00Z"
            ev["consecutive_flags"] = 1
            ev["latest_value_mm"] = 2.1
            
            # Recalculate checks for 2.1 mm spatial failure
            ev["checks"] = {
                "hard_threshold":  {"passed": True},
                "range_check":     {"passed": True, "threshold_mm": 25.0},
                "temporal":        {"passed": True, "skipped": "below_min_absolute"},
                "spatial":         {"passed": False, "reason": "spatial_outlier", "outlier_class": "weak", "di": 4.2, "value_mm": 2.1, "domain_median_mm": 0.0, "domain_mad": 0.5, "domain_n": 52},
                "persistent_zero": {"passed": True, "skipped": "gauge_nonzero"},
                "radar":           {"passed": True, "skipped": "below_trigger"},
                "drip_leak":       {"passed": True, "skipped": "insufficient_nonzero_slots"},
                "accum_anomaly":   {"passed": True, "skipped": "total_below_threshold", "gauge_24hr_mm": 5.2}
            }
            ev["qi"] = 0.90
            
            print(f"Updated event fields to: {ev['first_flagged_at']} -> {ev['last_seen']} ({ev['latest_value_mm']} mm, consecutive={ev['consecutive_flags']})")
            updated = True
            break

    if updated:
        print("Uploading updated log.json...")
        r2_put_json(r2, LOG_KEY, log_data)
        print("Done!")
    else:
        print("Egremont Ivy Hill event not found in log.json!")

if __name__ == "__main__":
    main()
