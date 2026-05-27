import os
import json
import boto3

# Load R2 config from tools/.env
env_vars = {}
try:
    with open("tools/.env", "r") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env_vars[k.strip()] = v.strip()
except Exception as e:
    print(f"Error reading .env: {e}")

R2_ACCOUNT_ID = env_vars.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = env_vars.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = env_vars.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = env_vars.get("R2_BUCKET_NAME", "")
LOG_KEY = "gaugecheck/log.json"

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

def r2_get_json(key):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        print(f"Error loading {key}: {e}")
        return None

def r2_put_json(key, data):
    body = json.dumps(data, separators=(",", ":")).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")

def main():
    print("Downloading log.json...")
    log_data = r2_get_json(LOG_KEY)
    if not log_data:
        print("Could not load log.json!")
        return

    events = log_data.get("events", [])
    print(f"Loaded {len(events)} events.")

    # Corrections mapping by station_id
    corrections = {
        # 1. Wickenby (E1908) -> 2026-05-23T03:15:00Z, 3.2 mm, 1 slot
        "E1908": {
            "first_flagged_at": "2026-05-23T03:15:00Z",
            "last_seen": "2026-05-23T03:15:00Z",
            "latest_slot_time": "2026-05-23T03:15:00Z",
            "consecutive_flags": 1,
            "latest_value_mm": 3.2
        },
        # 2. Canwick (E45881) -> 2026-05-23T03:00:00Z, 3.6 mm, 1 slot
        "E45881": {
            "first_flagged_at": "2026-05-23T03:00:00Z",
            "last_seen": "2026-05-23T03:00:00Z",
            "latest_slot_time": "2026-05-23T03:00:00Z",
            "consecutive_flags": 1,
            "latest_value_mm": 3.6
        },
        # 3. Tealby (E1613) -> 2026-05-23T03:15:00Z, 2.32 mm, 1 slot
        "E1613": {
            "first_flagged_at": "2026-05-23T03:15:00Z",
            "last_seen": "2026-05-23T03:15:00Z",
            "latest_slot_time": "2026-05-23T03:15:00Z",
            "consecutive_flags": 1,
            "latest_value_mm": 2.32
        },
        # 4. Egremont Ivy Hill (592158) -> Already aligned, but we can enforce it just in case
        "592158": {
            "first_flagged_at": "2026-05-22T23:45:00Z",
            "last_seen": "2026-05-22T23:45:00Z",
            "latest_slot_time": "2026-05-22T23:45:00Z",
            "consecutive_flags": 1,
            "latest_value_mm": 2.1
        },
        # 5. Havering Bower (000180TP) -> 10:45 to 11:00, 8.0 mm, 2 slots
        "000180TP": {
            "first_flagged_at": "2026-05-22T10:45:00Z",
            "last_seen": "2026-05-22T11:00:00Z",
            "latest_slot_time": "2026-05-22T11:00:00Z",
            "consecutive_flags": 2,
            "latest_value_mm": 8.0
        },
        # 6. Nags Head Lane (237740TP) -> 2026-05-22T10:15:00Z, 15.19 mm, 1 slot
        "237740TP": {
            "first_flagged_at": "2026-05-22T10:15:00Z",
            "last_seen": "2026-05-22T10:15:00Z",
            "latest_slot_time": "2026-05-22T10:15:00Z",
            "consecutive_flags": 1,
            "latest_value_mm": 15.19
        },
        # 7. Stanford Rivers (238944TP) -> 2026-05-22T09:15:00Z, 12.58 mm, 1 slot
        "238944TP": {
            "first_flagged_at": "2026-05-22T09:15:00Z",
            "last_seen": "2026-05-22T09:15:00Z",
            "latest_slot_time": "2026-05-22T09:15:00Z",
            "consecutive_flags": 1,
            "latest_value_mm": 12.58
        },
        # 8. Moreton (238777TP) -> 2026-05-22T07:30:00Z to 07:45:00Z, 3.0 mm, 2 slots
        "238777TP": {
            "first_flagged_at": "2026-05-22T07:30:00Z",
            "last_seen": "2026-05-22T07:45:00Z",
            "latest_slot_time": "2026-05-22T07:45:00Z",
            "consecutive_flags": 2,
            "latest_value_mm": 3.0
        }
    }

    updated = False
    for ev in events:
        sid = ev["station_id"]
        if sid in corrections:
            corr = corrections[sid]
            print(f"\nCorrecting {ev['name']} ({sid}):")
            print(f"  Old: {ev.get('first_flagged_at')} -> {ev.get('last_seen')} ({ev.get('latest_value_mm')} mm, N={ev.get('consecutive_flags')})")
            
            # Apply corrections
            ev["first_flagged_at"] = corr["first_flagged_at"]
            ev["last_seen"] = corr["last_seen"]
            ev["latest_slot_time"] = corr["latest_slot_time"]
            ev["consecutive_flags"] = corr["consecutive_flags"]
            ev["latest_value_mm"] = corr["latest_value_mm"]
            
            # If the failing checks contain value details, update them too
            checks = ev.get("checks", {})
            for check_name, c in checks.items():
                if isinstance(c, dict) and "value_mm" in c:
                    c["value_mm"] = corr["latest_value_mm"]
            
            print(f"  New: {ev['first_flagged_at']} -> {ev['last_seen']} ({ev['latest_value_mm']} mm, N={ev['consecutive_flags']})")
            updated = True

    if updated:
        print("\nUploading updated log.json...")
        r2_put_json(LOG_KEY, log_data)
        print("Done!")
    else:
        print("No matches found for correction!")

if __name__ == "__main__":
    main()
