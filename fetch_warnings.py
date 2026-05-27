#!/usr/bin/env python3
"""
fetch_warnings.py — Weather warnings fetcher and R2 archiver.

Queries the OpenWeather Alerts API using a UK-wide bounding box polygon,
parses Met Office/OWM weather warnings, saves the active list to warnings_latest.json,
merges daily archives, and prunes older daily archives (> 30 days retention).
"""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

# ── Cloudflare R2 Config ──────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

RETENTION_DAYS = 30
LATEST_KEY     = "warnings/warnings_latest.json"
ARCHIVE_PFX    = "warnings/archive"

# ── OpenWeather alerts API Config ─────────────────────────────────────────────
OWM_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

# UK Bounding Box Polygon (First and last coordinates close the ring)
UK_BOUNDING_BOX = {
    "type": "Polygon",
    "coordinates": [[
        [-11.0, 49.0],
        [-11.0, 61.5],
        [2.5, 61.5],
        [2.5, 49.0],
        [-11.0, 49.0]
    ]]
}


def get_r2_client():
    if not USE_R2:
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    except ImportError:
        print("boto3 not installed. Cloudflare R2 will be bypassed.")
        return None


def r2_get_json(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    r2.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8"
    )


def write_local_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


def read_local_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default



def fetch_warnings_from_api():
    """Queries the OpenWeather Alerts API and parses raw alerts securely."""
    location_str = json.dumps(UK_BOUNDING_BOX, separators=(",", ":"))
    encoded_loc = urllib.parse.quote(location_str)
    url = f"https://api.openweathermap.org/alerts/1.0?location={encoded_loc}&appid={OWM_API_KEY}"
    
    print(f"Fetching OpenWeather Alerts from API...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
            if status != 200:
                print(f"API returned status code: {status}")
                return None
            
            raw_data = json.loads(response.read().decode("utf-8"))
            
            # Robust extraction of the list of alerts
            alerts_list = []
            if isinstance(raw_data, list):
                alerts_list = raw_data
            elif isinstance(raw_data, dict):
                if "items" in raw_data:
                    alerts_list = raw_data["items"]
                elif "alerts" in raw_data:
                    alerts_list = raw_data["alerts"]
                else:
                    # Single alert item representation
                    alerts_list = [raw_data]
            
            print(f"Successfully retrieved {len(alerts_list)} raw alert items from API.")
            
            parsed_alerts = []
            for item in alerts_list:
                if not isinstance(item, dict):
                    continue
                
                alert_id = item.get("alert_ID") or item.get("alert_id")
                if not alert_id:
                    continue
                
                # Normalize tags to a list
                tags_raw = item.get("tag") or item.get("tags") or []
                if isinstance(tags_raw, str):
                    tags = [t.strip().lower() for t in tags_raw.split(",")]
                elif isinstance(tags_raw, list):
                    tags = [str(t).strip().lower() for t in tags_raw]
                else:
                    tags = [str(tags_raw).strip().lower()]
                
                # Geometry extraction
                geom = item.get("location") or {}
                if not geom or not isinstance(geom, dict):
                    geom = {
                        "type": item.get("type") or "Polygon",
                        "coordinates": item.get("coordinates")
                    }
                
                parsed_alerts.append({
                    "alert_id": str(alert_id),
                    "source": item.get("source", "government"),
                    "title": item.get("title") or "Weather Warning",
                    "description": item.get("description") or "",
                    "severity": str(item.get("severity", "unknown")).lower(),
                    "certainty": str(item.get("certainty", "unknown")).lower(),
                    "urgency": str(item.get("urgency", "unknown")).lower(),
                    "tag": tags,
                    "start_date": item.get("start_date") or item.get("date"),
                    "end_date": item.get("end_date"),
                    "geometry": geom
                })
            
            return parsed_alerts
            
    except Exception as e:
        print(f"API Fetch Error: {e}")
        return None


def main():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y%m%d")
    
    if not OWM_API_KEY:
        print("Error: OPENWEATHER_API_KEY environment variable is not configured.")
        sys.exit(1)
        
    r2 = get_r2_client()
    if r2:
        print("Connected to Cloudflare R2 successfully.")
    else:
        print("Cloudflare R2 not configured. Operating in LOCAL ONLY / DRY RUN mode.")
    
    # ── Step 1: Ingest Alerts ─────────────────────────────────────────────────
    active_alerts = fetch_warnings_from_api()
    if active_alerts is None:
        print("Error: Weather warnings ingestion failed or was unauthorized.")
        sys.exit(1)
        
    print(f"Active Warnings to save: {len(active_alerts)}")
    
    # ── Step 2: Write warnings_latest.json ────────────────────────────────────
    latest_obj = {
        "generated_at": now.isoformat(),
        "is_mock": False,
        "warnings": active_alerts
    }
    
    # Local save
    write_local_json("warnings/warnings_latest.json", latest_obj)
    print("Saved warnings/warnings_latest.json locally.")
    
    # R2 save
    if r2:
        try:
            r2_put_json(r2, LATEST_KEY, latest_obj)
            print("Uploaded warnings_latest.json to Cloudflare R2.")
        except Exception as e:
            print(f"Error uploading warnings_latest.json to R2: {e}")
            
    # ── Step 3: Archive Daily Ingests ─────────────────────────────────────────
    # We load the existing daily archive to merge alerts (keyed by alert_id)
    archive_key = f"{ARCHIVE_PFX}/warnings_{today_str}.json"
    archive_local_path = f"warnings/archive/warnings_{today_str}.json"
    
    existing_archive = []
    if r2:
        existing_archive = r2_get_json(r2, archive_key, default=[])
    else:
        existing_archive = read_local_json(archive_local_path, default=[])
        
    # Merge existing and new alerts by alert_id
    merged_alerts = {a["alert_id"]: a for a in existing_archive}
    for alert in active_alerts:
        merged_alerts[alert["alert_id"]] = alert
        
    archive_list = list(merged_alerts.values())
    
    # Local save
    write_local_json(archive_local_path, archive_list)
    print(f"Saved daily archive locally: {archive_local_path} ({len(archive_list)} warnings total)")
    
    # R2 save
    if r2:
        try:
            r2_put_json(r2, archive_key, archive_list)
            print(f"Uploaded daily archive to R2: {archive_key}")
        except Exception as e:
            print(f"Error uploading daily archive to R2: {e}")
            
    # ── Step 4: Prune Old Archives (> 30 days) ──────────────────────────────
    cutoff_date = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
    print(f"Pruning daily archives older than: {cutoff_date}")
    
    # Local pruning
    archive_dir = "warnings/archive"
    if os.path.exists(archive_dir):
        for f in os.listdir(archive_dir):
            if f.startswith("warnings_") and f.endswith(".json"):
                date_part = f.replace("warnings_", "").replace(".json", "")
                if date_part.isdigit() and date_part < cutoff_date:
                    try:
                        os.remove(os.path.join(archive_dir, f))
                        print(f"  Deleted old local archive file: {f}")
                    except Exception as e:
                        print(f"  Error deleting local file {f}: {e}")
                        
    # R2 pruning
    if r2:
        try:
            paginator = r2.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=ARCHIVE_PFX + "/"):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Extract date segment: warnings/archive/warnings_YYYYMMDD.json
                    fname = key.rsplit("/", 1)[-1].replace(".json", "")
                    date_part = fname.replace("warnings_", "")
                    if len(date_part) == 8 and date_part.isdigit() and date_part < cutoff_date:
                        try:
                            r2.delete_object(Bucket=R2_BUCKET, Key=key)
                            print(f"  Deleted old R2 archive object: {key}")
                        except Exception as e:
                            print(f"  Warning: could not delete R2 object {key}: {e}")
        except Exception as e:
            print(f"  Warning: R2 list scan for pruning failed: {e}")
            
    print("Warnings processing completed successfully!")


if __name__ == "__main__":
    main()
