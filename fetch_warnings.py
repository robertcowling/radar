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
import re
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

# ── Met Office RSS Fallback Region Mapping ────────────────────────────────────
REGIONS_MAP = {
    "south west england": ["South West"],
    "south west": ["South West"],
    "london & south east england": ["London", "South East"],
    "london & south east": ["London", "South East"],
    "london and south east england": ["London", "South East"],
    "london and south east": ["London", "South East"],
    "london": ["London"],
    "south east england": ["South East"],
    "south east": ["South East"],
    "wales": ["Wales"],
    "west midlands": ["West Midlands"],
    "east midlands": ["East Midlands"],
    "east of england": ["East"],
    "east": ["East"],
    "yorkshire & humber": ["Yorkshire and the Humber"],
    "yorkshire and the humber": ["Yorkshire and the Humber"],
    "yorkshire and humber": ["Yorkshire and the Humber"],
    "yorkshire": ["Yorkshire and the Humber"],
    "north west england": ["North West"],
    "north west": ["North West"],
    "north east england": ["North East"],
    "north east": ["North East"],
    "northern ireland": ["Northern Ireland"],
    "scotland": ["Scotland"],
    "highlands & islands": ["Scotland"],
    "orkney & shetland": ["Scotland"],
    "grampian": ["Scotland"],
    "central, tayside & fife": ["Scotland"],
    "strathclyde": ["Scotland"],
    "south west scotland": ["Scotland"],
    "dumfries, galloway, lothian & borders": ["Scotland"],
    "lothian & borders": ["Scotland"]
}

# Month helper mapping for parsing Met Office warning dates
MONTHS_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
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


def get_geojson_regions():
    geojson_path = "uk_regions.geojson"
    if not os.path.exists(geojson_path):
        print(f"Warning: {geojson_path} not found.")
        return {}
    
    try:
        with open(geojson_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        regions = {}
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            name = props.get("rgn19nm")
            if name:
                regions[name.strip().lower()] = feature.get("geometry")
        return regions
    except Exception as e:
        print(f"Error reading GeoJSON: {e}")
        return {}


def parse_guid_params(guid):
    base_id = "warning"
    region = None
    
    parts = guid.split("#")
    query_str = parts[1] if len(parts) > 1 else ""
    if not query_str and "?" in guid:
        query_str = guid.split("?")[1]
        
    if query_str:
        query_str = query_str.lstrip("?")
        params = urllib.parse.parse_qs(query_str)
        if "id" in params:
            base_id = params["id"][0]
        if "region" in params:
            region = params["region"][0]
            
    return base_id, region


def parse_nswws_dates(desc, current_year=2026):
    # Match pattern: "valid from 1400 Wed 27 May to 2259 Wed 27 May"
    pattern = r"valid from\s+(\d{4})\s+\w{3}\s+(\d{1,2})\s+(\w{3})\s+to\s+(\d{4})\s+\w{3}\s+(\d{1,2})\s+(\w{3})"
    match = re.search(pattern, desc, re.IGNORECASE)
    
    if match:
        try:
            shr_min, sday, smonth, ehr_min, eday, emonth = match.groups()
            
            start_hr = int(shr_min[:2])
            start_min = int(shr_min[2:])
            start_day = int(sday)
            start_month = MONTHS_MAP.get(smonth.lower()[:3], 5)
            
            end_hr = int(ehr_min[:2])
            end_min = int(ehr_min[2:])
            end_day = int(eday)
            end_month = MONTHS_MAP.get(emonth.lower()[:3], 5)
            
            tz_offset = "+01:00" if (3 < start_month < 11) else "+00:00"
            start_iso = f"{current_year:04d}-{start_month:02d}-{start_day:02d}T{start_hr:02d}:{start_min:02d}:00{tz_offset}"
            
            tz_offset_end = "+01:00" if (3 < end_month < 11) else "+00:00"
            end_iso = f"{current_year:04d}-{end_month:02d}-{end_day:02d}T{end_hr:02d}:{end_min:02d}:00{tz_offset_end}"
            
            return start_iso, end_iso
        except Exception as e:
            print(f"Error parsing regex match fields: {e}")
            
    return None, None


def fetch_warnings_from_metoffice_rss():
    """Fetches public Met Office severe weather warnings from the RSS cache feed."""
    url = "https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/uk"
    print(f"Fetching Met Office severe weather warnings RSS feed from {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
            if status != 200:
                print(f"RSS Feed returned status code: {status}")
                return []
            
            content = response.read()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(content)
            
            channel = root.find("channel")
            if channel is None:
                print("Invalid RSS feed structure (no channel element found).")
                return []
                
            items = channel.findall("item")
            print(f"Found {len(items)} warning items in RSS feed.")
            
            regions_geoms = get_geojson_regions()
            parsed_warnings = []
            
            for item in items:
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                desc = item.find("description").text if item.find("description") is not None else ""
                guid = item.find("guid").text if item.find("guid") is not None else link
                
                if not title:
                    continue
                
                base_id, region_param = parse_guid_params(guid)
                region_name = region_param
                if not region_name:
                    if "affecting" in title:
                        region_name = title.split("affecting")[-1].strip()
                    else:
                        region_name = "uk"
                
                sanitized_reg = str(region_name).lower().replace(" ", "_").replace("&", "and")
                alert_id = f"metoffice_{base_id}_{sanitized_reg}"
                
                title_lower = title.lower()
                if "red" in title_lower:
                    severity = "extreme"
                elif "amber" in title_lower:
                    severity = "severe"
                elif "yellow" in title_lower:
                    severity = "moderate"
                else:
                    severity = "minor"
                
                tags = []
                if "thunderstorm" in title_lower:
                    tags.append("thunderstorm")
                if "rain" in title_lower or "precipitation" in title_lower:
                    tags.append("rain")
                if "wind" in title_lower or "gale" in title_lower:
                    tags.append("wind")
                if "flood" in title_lower:
                    tags.append("flood")
                if "snow" in title_lower or "ice" in title_lower:
                    tags.append("snow")
                if "fog" in title_lower or "mist" in title_lower:
                    tags.append("fog")
                if not tags:
                    tags.append("rain")
                
                now_yr = datetime.now(timezone.utc).year
                start_date, end_date = parse_nswws_dates(desc, current_year=now_yr)
                
                if not start_date or not end_date:
                    now_utc = datetime.now(timezone.utc)
                    tz_offset = "+01:00" if (3 < now_utc.month < 11) else "+00:00"
                    start_date = now_utc.strftime(f"%Y-%m-%dT%H:%M:%S{tz_offset}")
                    end_date = (now_utc + timedelta(days=1)).strftime(f"%Y-%m-%dT%H:%M:%S{tz_offset}")
                
                matched_regions = []
                r_lower = region_name.lower().strip()
                if r_lower in REGIONS_MAP:
                    matched_regions.extend(REGIONS_MAP[r_lower])
                else:
                    for key, val in REGIONS_MAP.items():
                        if key in r_lower:
                            matched_regions.extend(val)
                
                matched_regions = list(set(matched_regions))
                
                geoms = []
                for rname in matched_regions:
                    rgeom = regions_geoms.get(rname.lower())
                    if rgeom:
                        geoms.append(rgeom)
                
                if len(geoms) == 1:
                    geometry = geoms[0]
                elif len(geoms) > 1:
                    geometry = {
                        "type": "GeometryCollection",
                        "geometries": geoms
                    }
                else:
                    geometry = None
                
                parsed_warnings.append({
                    "alert_id": alert_id,
                    "source": "metoffice",
                    "title": title,
                    "description": desc,
                    "severity": severity,
                    "certainty": "likely",
                    "urgency": "expected",
                    "tag": tags,
                    "start_date": start_date,
                    "end_date": end_date,
                    "geometry": geometry
                })
                
            print(f"Successfully parsed {len(parsed_warnings)} warnings from Met Office RSS.")
            return parsed_warnings
            
    except Exception as e:
        print(f"Error fetching/parsing Met Office RSS Feed: {e}")
        return []


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
            
            alerts_list = []
            if isinstance(raw_data, list):
                alerts_list = raw_data
            elif isinstance(raw_data, dict):
                if "items" in raw_data:
                    alerts_list = raw_data["items"]
                elif "alerts" in raw_data:
                    alerts_list = raw_data["alerts"]
                else:
                    alerts_list = [raw_data]
            
            print(f"Successfully retrieved {len(alerts_list)} raw alert items from API.")
            
            parsed_alerts = []
            for item in alerts_list:
                if not isinstance(item, dict):
                    continue
                
                alert_id = item.get("alert_ID") or item.get("alert_id")
                if not alert_id:
                    continue
                
                tags_raw = item.get("tag") or item.get("tags") or []
                if isinstance(tags_raw, str):
                    tags = [t.strip().lower() for t in tags_raw.split(",")]
                elif isinstance(tags_raw, list):
                    tags = [str(t).strip().lower() for t in tags_raw]
                else:
                    tags = [str(tags_raw).strip().lower()]
                
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
        print("Warning: Weather warnings ingestion was unauthorized (HTTP 401) or failed.")
        print("Your OpenWeather API key is valid, but your account's subscription tier does not include")
        print("access to the premium Alerts API (alerts/1.0).")
        print("Falling back to parsing the public Met Office Severe Weather Warnings RSS feed...")
        active_alerts = fetch_warnings_from_metoffice_rss()
        
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
