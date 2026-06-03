#!/usr/bin/env python3
"""
fetch_warnings.py — Weather warnings fetcher and R2 archiver.

Queries the EUMETNET MeteoGate OGC-API EDR service to fetch official UK severe weather 
warnings with exact raw MultiPolygon geometries. Normalizes warnings into standard formats,
archives active lists to Cloudflare R2 and local JSON, and handles pruning.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import hashlib
import re
from datetime import datetime, timedelta, timezone

# ── Cloudflare R2 Config ──────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

RETENTION_DAYS = 90
LATEST_KEY     = "warnings/warnings_latest.json"
ARCHIVE_PFX    = "warnings/archive"

# ── EUMETNET MeteoGate API Config ─────────────────────────────────────────────
# We fallback to the user's active API key so it works out-of-the-box in all environments
METEOGATE_API_KEY = (
    os.environ.get("METEOGATE_API_KEY", "").strip()
    or "6a52afd3ac0e491f265f331a52de29eb04a559787e8ff2777682f3ca88619bc8"
)


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


def fetch_warnings_from_meteogate():
    """Queries the EUMETNET MeteoGate OGC-API EDR service to fetch official UK severe weather warnings with exact MultiPolygon shapes."""
    print("Fetching weather warnings from EUMETNET MeteoGate EDR API...")
    
    now = datetime.now(timezone.utc)
    
    # We query two 23-hour windows (past and future) to completely cover ongoing and upcoming warnings
    # since MeteoGate EDR restricts the query duration to < 24 hours.
    windows = [
        # Past 23 hours to now (captures warnings sent/updated recently)
        ((now - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        # Now to future 23 hours (captures future scheduled warnings)
        (now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    ]
    
    headers = {
        'apikey': METEOGATE_API_KEY,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    raw_alerts_by_id = {}
    
    for start_str, end_str in windows:
        url = f"https://api.meteogate.eu/warnings/collections/warnings/locations/UK?datetime={start_str}/{end_str}"
        print(f"  Querying MeteoGate EDR warnings for UK range: {start_str}/{end_str}...")
        
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    print(f"    API returned non-200 status: {response.status}")
                    continue
                    
                data = json.loads(response.read().decode("utf-8"))
                features = data.get("features", [])
                print(f"    Found {len(features)} warnings in this window.")
                
                for feat in features:
                    alert_id = feat.get("properties", {}).get("alertId")
                    if not alert_id or alert_id in raw_alerts_by_id:
                        continue
                        
                    raw_alerts_by_id[alert_id] = feat
        except Exception as e:
            print(f"    Error querying EDR warnings: {e}")
            
    # Process each unique warning
    parsed_alerts = []
    
    for alert_id, feat in raw_alerts_by_id.items():
        links = feat.get("links", [])
        
        json_url = None
        geojson_url = None
        
        for l in links:
            if l.get("type") == "application/json" and l.get("rel") == "json":
                json_url = l.get("href")
            elif l.get("type") == "application/geo+json" and l.get("rel") == "geometry":
                geojson_url = l.get("href")
                
        if not json_url or not geojson_url:
            print(f"  Skipping alert {alert_id}: missing geometry or metadata links.")
            continue
            
        print(f"  Fetching metadata and geometry for alert: {alert_id}...")
        try:
            # 1. Fetch metadata JSON (pre-signed digitalocean URLs do not need headers/auth)
            meta_req = urllib.request.Request(json_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(meta_req, timeout=15) as meta_res:
                meta = json.loads(meta_res.read().decode("utf-8"))
                
            # 2. Fetch GeoJSON geometry
            geom_req = urllib.request.Request(geojson_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(geom_req, timeout=15) as geom_res:
                geom_data = json.loads(geom_res.read().decode("utf-8"))
                
            info_list = meta.get("info", [])
            if not info_list:
                continue
            info = info_list[0]

            # ── Skip EA catchment-specific flood warnings ──────────────────────
            # EA flood warnings come through MeteoGate labelled as "Met Office"
            # but are catchment-specific (e.g. "Flood Warning: Saredon Brook at ...").
            # Met Office weather warnings ALWAYS use colour-coded titles like
            # "Yellow rain warning" or "Red wind warning" — never "Flood Warning: [location]".
            #
            # Filter 1: CAP category "Hydro" = EA flood warning
            categories = info.get("category", [])
            if isinstance(categories, str):
                categories = [categories]
            if "Hydro" in categories:
                print(f"    Skipping EA flood warning (Hydro category): {alert_id}")
                continue

            # Filter 2: Title pattern "Flood Warning: ..." / "Flood Alert: ..." etc.
            # Met Office titles never contain a colon followed by a location name.
            raw_event = info.get("event", "")
            raw_event_lower = raw_event.lower()
            ea_title_patterns = [
                "flood warning:",
                "flood alert:",
                "severe flood warning:",
                "flood watch:",
            ]
            if any(raw_event_lower.startswith(p) for p in ea_title_patterns):
                print(f"    Skipping EA flood warning (title pattern): {alert_id} — '{raw_event}'")
                continue

            title = info.get("event", "Weather Warning")
            description = info.get("description", "")
            instruction = info.get("instruction", "")
            headline = info.get("headline", "")
            
            area_list = info.get("area", [])
            area_desc = area_list[0].get("areaDesc", "") if area_list else ""
            
            start_date = info.get("onset") or info.get("effective")
            end_date = info.get("expires")
            
            # Filter out expired warnings relative to current UTC time
            # if end_date:
            #     end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            #     if end_dt < now:
            #         print(f"    Alert {alert_id} has expired (expires at {end_date}), skipping.")
            #         continue
            
            severity_str = info.get("severity", "Moderate").lower()
            certainty = info.get("certainty", "Likely").lower()
            urgency = info.get("urgency", "Expected").lower()
            
            # Map severity level
            if "extreme" in severity_str:
                severity = "extreme"
            elif "severe" in severity_str:
                severity = "severe"
            elif "moderate" in severity_str or "yellow" in severity_str:
                severity = "moderate"
            elif "minor" in severity_str:
                severity = "minor"
            else:
                severity = "moderate"
                
            # Tags extraction using precise title and description parsing
            tags = []
            event_lower = title.lower()
            desc_lower = description.lower()
            
            if "thunderstorm" in event_lower:
                tags.append("thunderstorm")
            elif "rain" in event_lower or "shower" in event_lower:
                tags.append("rain")
            elif "wind" in event_lower or "gale" in event_lower:
                tags.append("wind")
            elif "flood" in event_lower:
                tags.append("flood")
            elif "snow" in event_lower or "ice" in event_lower:
                tags.append("snow")
            elif "fog" in event_lower:
                tags.append("fog")
                
            if not tags:
                def has_word(pattern, text):
                    return bool(re.search(r'\b(?:' + pattern + r')\b', text))
                    
                if has_word("thunderstorm|thunderstorms|lightning", event_lower) or has_word("thunderstorm|thunderstorms|lightning", desc_lower):
                    tags.append("thunderstorm")
                if has_word("rain|precipitation|showers?", event_lower) or has_word("rain|precipitation|showers?", desc_lower):
                    tags.append("rain")
                if has_word("wind|winds|gale|gales|storm|storms", event_lower) or has_word("wind|winds|gale|gales|storm|storms", desc_lower):
                    tags.append("wind")
                if has_word("flood|flooding", event_lower) or has_word("flood|flooding", desc_lower):
                    tags.append("flood")
                if has_word("snow|snowing|ice|icy", event_lower) or (has_word("snow|snowing|ice|icy", desc_lower) and "advice" not in desc_lower):
                    tags.append("snow")
                if has_word("fog|foggy|mist|misty", event_lower) or has_word("fog|foggy|mist|misty", desc_lower):
                    tags.append("fog")
                    
            if not tags:
                tags.append("rain")
                
            geometry = geom_data.get("geometry")
            
            parsed_alerts.append({
                "alert_id": f"meteo_{alert_id}",
                "source": "ukmetoffice",
                "title": title,
                "description": description,
                "instruction": instruction,
                "headline": headline,
                "area_desc": area_desc,
                "severity": severity,
                "certainty": certainty,
                "urgency": urgency,
                "tag": tags,
                "start_date": start_date,
                "end_date": end_date,
                "geometry": geometry,
                "raw_meta": meta,
                "raw_properties": feat.get("properties", {})
            })
        except Exception as e:
            print(f"    Error processing alert {alert_id}: {e}")
            
    print(f"Successfully processed {len(parsed_alerts)} raw-polygon weather warnings from MeteoGate EDR API.")
    return parsed_alerts


def main():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y%m%d")
    
    r2 = get_r2_client()
    if r2:
        print("Connected to Cloudflare R2 successfully.")
    else:
        print("Cloudflare R2 not configured. Operating in LOCAL ONLY / DRY RUN mode.")
    
    # ── Step 1: Ingest Alerts from EUMETNET MeteoGate ─────────────────────────
    active_alerts = fetch_warnings_from_meteogate()
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
