#!/usr/bin/env python3
"""
fetch_warnings.py — Weather warnings fetcher and R2 archiver.

Queries the OpenWeather One Call API 3.0 for all UK region centroids defined in
uk_regions.geojson, parses and merges weather warnings, saves the active list to
warnings_latest.json, merges daily archives, and prunes older archives (> 30 days).
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import hashlib
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

# ── OpenWeather API Config ────────────────────────────────────────────────────
OWM_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")


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


def fetch_warnings_from_onecall():
    """Queries the OpenWeather One Call API 3.0 at the centroid of each UK region to ingest active alerts."""
    print("Fetching weather warnings from OpenWeather One Call 3.0 API...")
    
    # Load region features and centroids from local uk_regions.geojson
    geojson_path = "uk_regions.geojson"
    if not os.path.exists(geojson_path):
        print(f"Error: {geojson_path} not found.")
        return []
        
    try:
        with open(geojson_path, "r", encoding="utf-8") as f:
            geojson_data = json.load(f)
    except Exception as e:
        print(f"Error loading {geojson_path}: {e}")
        return []
        
    regions_list = []
    for feature in geojson_data.get("features", []):
        props = feature.get("properties", {})
        name = props.get("rgn19nm")
        lat = props.get("lat")
        lon = props.get("long")
        geom = feature.get("geometry")
        
        if name and lat is not None and lon is not None:
            regions_list.append({
                "name": name,
                "lat": float(lat),
                "lon": float(lon),
                "geometry": geom
            })
            
    print(f"Loaded {len(regions_list)} region centroids from GeoJSON.")
    
    raw_alerts_by_key = {}
    
    # Query One Call 3.0 for each region's centroid coordinate
    for r in regions_list:
        url = f"https://api.openweathermap.org/data/3.0/onecall?lat={r['lat']}&lon={r['lon']}&appid={OWM_API_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        
        print(f"  Querying One Call 3.0 for: {r['name']} ({r['lat']}, {r['lon']})...")
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status != 200:
                    print(f"    API returned non-200 status for {r['name']}: {response.status}")
                    continue
                    
                data = json.loads(response.read().decode("utf-8"))
                alerts = data.get("alerts", [])
                if alerts:
                    print(f"    Found {len(alerts)} alerts for {r['name']}.")
                    
                for alert in alerts:
                    event = alert.get("event") or "Weather Warning"
                    description = alert.get("description") or ""
                    start = alert.get("start")
                    end = alert.get("end")
                    sender = alert.get("sender_name") or "Government"
                    
                    if start is None or end is None:
                        continue
                        
                    # Create stable key for deduplication and grouping
                    key_str = f"{event}_{start}_{end}_{description}"
                    
                    if key_str not in raw_alerts_by_key:
                        raw_alerts_by_key[key_str] = {
                            "event": event,
                            "description": description,
                            "start": start,
                            "end": end,
                            "sender": sender,
                            "regions": []
                        }
                        
                    raw_alerts_by_key[key_str]["regions"].append({
                        "name": r["name"],
                        "geometry": r["geometry"]
                    })
        except Exception as e:
            print(f"    Error querying One Call 3.0 for {r['name']}: {e}")
            
    # Process and build final alert list
    parsed_alerts = []
    
    for key_str, item in raw_alerts_by_key.items():
        event = item["event"]
        description = item["description"]
        start = item["start"]
        end = item["end"]
        sender = item["sender"]
        regions = item["regions"]
        
        # Calculate unique stable alert_id based on event info
        hasher = hashlib.md5()
        hasher.update(f"{event}_{start}_{end}".encode("utf-8"))
        alert_id = f"owm_{hasher.hexdigest()}"
        
        # Determine severity based on event title
        event_lower = event.lower()
        if "red" in event_lower or "extreme" in event_lower:
            severity = "extreme"
        elif "amber" in event_lower or "severe" in event_lower:
            severity = "severe"
        elif "yellow" in event_lower or "moderate" in event_lower:
            severity = "moderate"
        elif "minor" in event_lower:
            severity = "minor"
        else:
            severity = "moderate"  # default standard warning severity
            
        # Determine tags using precise regex word boundaries to avoid false positives (e.g. "advice" -> "ice")
        import re
        tags = []
        desc_lower = description.lower()
        title_lower = event_lower
        
        # 1. Prioritize matching the primary warning type explicitly named in the event title
        if "thunderstorm" in title_lower:
            tags.append("thunderstorm")
        elif "rain" in title_lower or "shower" in title_lower:
            tags.append("rain")
        elif "wind" in title_lower or "gale" in title_lower:
            tags.append("wind")
        elif "flood" in title_lower:
            tags.append("flood")
        elif "snow" in title_lower or "ice" in title_lower:
            tags.append("snow")
        elif "fog" in title_lower:
            tags.append("fog")
            
        # 2. If title is generic, match using strict word boundaries from description
        if not tags:
            def has_word(pattern, text):
                return bool(re.search(r'\b(?:' + pattern + r')\b', text))
                
            if has_word("thunderstorm|thunderstorms|lightning", title_lower) or has_word("thunderstorm|thunderstorms|lightning", desc_lower):
                tags.append("thunderstorm")
            if has_word("rain|precipitation|showers?", title_lower) or has_word("rain|precipitation|showers?", desc_lower):
                tags.append("rain")
            if has_word("wind|winds|gale|gales|storm|storms", title_lower) or has_word("wind|winds|gale|gales|storm|storms", desc_lower):
                tags.append("wind")
            if has_word("flood|flooding", title_lower) or has_word("flood|flooding", desc_lower):
                tags.append("flood")
            # For snow/ice, check specifically to avoid matching 'advice'
            if has_word("snow|snowing|ice|icy", title_lower) or (has_word("snow|snowing|ice|icy", desc_lower) and "advice" not in desc_lower):
                tags.append("snow")
            if has_word("fog|foggy|mist|misty", title_lower) or has_word("fog|foggy|mist|misty", desc_lower):
                tags.append("fog")
                
        if not tags:
            tags.append("rain")  # default fallback tag
            
        # Convert start/end Unix timestamps to ISO strings in UTC
        start_date = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
        end_date = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
        
        # Combine geometries of all matched regions
        geoms = [r["geometry"] for r in regions if r["geometry"]]
        if len(geoms) == 1:
            geometry = geoms[0]
        elif len(geoms) > 1:
            geometry = {
                "type": "GeometryCollection",
                "geometries": geoms
            }
        else:
            geometry = None
            
        # Append to parsed list
        parsed_alerts.append({
            "alert_id": alert_id,
            "source": sender.lower().replace(" ", ""),
            "title": event,
            "description": description,
            "severity": severity,
            "certainty": "likely",
            "urgency": "expected",
            "tag": tags,
            "start_date": start_date,
            "end_date": end_date,
            "geometry": geometry
        })
        
    print(f"Successfully processed {len(parsed_alerts)} merged alerts from One Call 3.0 API.")
    return parsed_alerts


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
    
    # ── Step 1: Ingest Alerts from OpenWeather One Call 3.0 ──────────────────
    active_alerts = fetch_warnings_from_onecall()
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
