import os
import json
import boto3
from datetime import datetime, timezone

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

def main():
    r2 = get_r2()
    print("Downloading log.json...")
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=LOG_KEY)
        log_data = json.loads(obj["Body"].read())
    except Exception as e:
        print(f"Error loading log.json: {e}")
        return

    events = log_data.get("events", [])
    print(f"Loaded {len(events)} events.")

    for ev in events:
        first = ev.get("first_flagged_at")
        last = ev.get("last_seen")
        if not first or not last:
            continue

        # Convert to datetime objects
        try:
            t_first = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ")
            t_last = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            try:
                t_first = datetime.fromisoformat(first.replace("Z", "+00:00")).replace(tzinfo=None)
                t_last = datetime.fromisoformat(last.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue

        # Floor both to standard 15-minute slot boundary (900 seconds)
        first_epoch = int(t_first.replace(tzinfo=timezone.utc).timestamp())
        last_epoch = int(t_last.replace(tzinfo=timezone.utc).timestamp())

        first_snapped = (first_epoch // 900) * 900
        last_snapped = (last_epoch // 900) * 900

        t_first_snapped = datetime.fromtimestamp(first_snapped, tz=timezone.utc)
        t_last_snapped = datetime.fromtimestamp(last_snapped, tz=timezone.utc)

        first_flagged_str = t_first_snapped.strftime("%Y-%m-%dT%H:%M:%SZ")
        last_seen_str = t_last_snapped.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Set correct fields
        ev["first_flagged_at"] = first_flagged_str
        ev["last_seen"] = last_seen_str
        ev["latest_slot_time"] = last_seen_str

        # Recalculate duration and consecutive_flags
        duration_mins = (last_snapped - first_snapped) // 60
        consecutive_flags = (duration_mins // 15) + 1
        
        # Override specific Havering Bower consecutive count
        if ev["station_id"] == "000180TP": # Havering Bower ID
            consecutive_flags = 1
            ev["last_seen"] = first_flagged_str
            ev["latest_slot_time"] = first_flagged_str

        ev["consecutive_flags"] = max(1, consecutive_flags)
        print(f"  {ev['name']}: {ev['first_flagged_at']} -> {ev['last_seen']} ({ev['consecutive_flags']} slots)")

    # Save and upload
    body = json.dumps(log_data, separators=(",", ":")).encode()
    print("Uploading clean log.json...")
    r2.put_object(Bucket=R2_BUCKET, Key=LOG_KEY, Body=body,
                  ContentType="application/json; charset=utf-8")
    print("Done!")

if __name__ == "__main__":
    main()
