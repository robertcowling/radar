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

def main():
    readings_20260523 = r2_get_json("rain/readings/20260523.json")
    if not readings_20260523:
        print("Could not load readings!")
        return

    stations = ["E1613", "E45881", "E1908"]
    all_readings = readings_20260523.get("readings", {})
    
    for sid in stations:
        sdata = all_readings.get(sid, {})
        print(f"\n--- Station: {sid} ---")
        # Sort slots by integer slot index
        for slot_str in sorted(sdata.keys(), key=int):
            slot = int(slot_str)
            hour = slot // 4
            minute = (slot % 4) * 15
            # Convert to BST (+1h from UTC)
            bst_hour = (hour + 1) % 24
            val = sdata[slot_str]
            if val > 0.0:
                print(f"Slot {slot:02d} ({hour:02d}:{minute:02d} UTC / {bst_hour:02d}:{minute:02d} BST): {val} mm")

if __name__ == "__main__":
    main()
