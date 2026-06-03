#!/usr/bin/env python3
"""
backfill_warnings.py — Backfills historical severe weather warnings from April 2025 to today.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

# Import helpers and configurations from fetch_warnings
from fetch_warnings import (
    get_r2_client, r2_put_json, write_local_json, read_local_json,
    is_weather_warning, METEOGATE_API_KEY, R2_BUCKET, ARCHIVE_PFX, USE_R2
)

def query_window(start_str, end_str):
    """Queries EDR API for a specific time window."""
    headers = {
        'apikey': METEOGATE_API_KEY,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    url = f"https://api.meteogate.eu/warnings/collections/warnings/locations/UK?datetime={start_str}/{end_str}"
    
    # Retry loop with backoff for rate limit (429)
    backoff = 2.0
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    return data.get("features", [])
                elif response.status == 204:
                    return []
                print(f"    API returned status {response.status}")
                return []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"    [Rate limited 429, sleeping {backoff}s before retry...]")
                time.sleep(backoff)
                backoff *= 2.0
                continue
            print(f"    HTTP Error querying range {start_str}/{end_str}: {e.code} {e.reason}")
            return []
        except Exception as e:
            print(f"    Error querying range {start_str}/{end_str}: {e}")
            return []
    print("    Failed after maximum retries due to rate limit.")
    return []

def fetch_alert_details(json_url, geojson_url):
    """Fetches raw metadata and geometry for a warning."""
    backoff = 2.0
    for attempt in range(3):
        try:
            # 1. Fetch metadata JSON
            meta_req = urllib.request.Request(json_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(meta_req, timeout=15) as meta_res:
                meta = json.loads(meta_res.read().decode("utf-8"))
                
            # 2. Fetch GeoJSON geometry
            geom_req = urllib.request.Request(geojson_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(geom_req, timeout=15) as geom_res:
                geom_data = json.loads(geom_res.read().decode("utf-8"))
                
            return meta, geom_data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            raise e
    raise Exception("Max retries for alert details exhausted due to rate limits")

def parse_and_format_alert(feat, meta, geom_data):
    """Parses raw CAP metadata into our standard warning format."""
    alert_id = feat.get("properties", {}).get("alertId")
    info_list = meta.get("info", [])
    if not info_list:
        return None
    info = info_list[0]
    
    title = info.get("event", "Weather Warning")
    description = info.get("description", "")
    instruction = info.get("instruction", "")
    headline = info.get("headline", "")
    
    area_list = info.get("area", [])
    area_desc = area_list[0].get("areaDesc", "") if area_list else ""
    
    start_date = info.get("onset") or info.get("effective")
    end_date = info.get("expires")
    
    severity_str = info.get("severity", "Moderate").lower()
    certainty = info.get("certainty", "Likely").lower()
    urgency = info.get("urgency", "Expected").lower()
    
    # Map severity
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
        
    # Extract tags
    import re
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
    
    return {
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
    }

def main():
    r2 = get_r2_client()
    if r2:
        print("Connected to Cloudflare R2 successfully.")
    else:
        print("Operating in LOCAL ONLY / DRY RUN mode (no R2 credentials).")

    # Define range: April 1, 2025 to today
    start_date = datetime(2025, 4, 1, tzinfo=timezone.utc)
    end_date = datetime.now(timezone.utc)
    
    current_day = start_date
    delta = timedelta(days=1)
    
    # Cache to avoid refetching details of same alertId multiple times
    alert_cache = {} # alertId -> parsed_alert_dict (or None if filtered out)

    total_days = (end_date - start_date).days + 1
    day_count = 0

    while current_day <= end_date:
        day_str = current_day.strftime("%Y%m%d")
        day_count += 1
        print(f"[{day_count}/{total_days}] Processing archives for: {current_day.strftime('%Y-%m-%d')}...")
        
        # 12-hour windows to cover the day (under 24h limit)
        windows = [
            (current_day.strftime("%Y-%m-%dT00:00:00Z"), current_day.strftime("%Y-%m-%dT11:59:59Z")),
            (current_day.strftime("%Y-%m-%dT12:00:00Z"), current_day.strftime("%Y-%m-%dT23:59:59Z"))
        ]
        
        features_for_day = {} # alertId -> feature
        
        for w_start, w_end in windows:
            feats = query_window(w_start, w_end)
            for f in feats:
                aid = f.get("properties", {}).get("alertId")
                if aid:
                    features_for_day[aid] = f
            time.sleep(1.0) # Polite sleep between EDR queries
            
        parsed_alerts = []
        
        for aid, feat in features_for_day.items():
            if aid in alert_cache:
                parsed = alert_cache[aid]
                if parsed:
                    parsed_alerts.append(parsed)
                continue
                
            # Need to fetch details
            links = feat.get("links", [])
            json_url = None
            geojson_url = None
            for l in links:
                if l.get("type") == "application/json" and l.get("rel") == "json":
                    json_url = l.get("href")
                elif l.get("type") == "application/geo+json" and l.get("rel") == "geometry":
                    geojson_url = l.get("href")
                    
            if not json_url or not geojson_url:
                alert_cache[aid] = None
                continue
                
            print(f"    Fetching details for alert {aid}...")
            try:
                meta, geom_data = fetch_alert_details(json_url, geojson_url)
                info_list = meta.get("info", [])
                if info_list:
                    info = info_list[0]
                    raw_event = info.get("event", "")
                    if is_weather_warning(raw_event, info):
                        parsed = parse_and_format_alert(feat, meta, geom_data)
                        if parsed:
                            parsed_alerts.append(parsed)
                            alert_cache[aid] = parsed
                            time.sleep(1.0) # Polite sleep
                            continue
                alert_cache[aid] = None
            except Exception as e:
                print(f"    Error fetching details for {aid}: {e}")
                time.sleep(2.0)
                
        # Write daily archive if warnings were active during this day
        # Or always write if the file existed, to overwrite/ensure clean status
        archive_key = f"{ARCHIVE_PFX}/warnings_{day_str}.json"
        archive_local_path = f"warnings/archive/warnings_{day_str}.json"
        
        if parsed_alerts:
            # Local save
            write_local_json(archive_local_path, parsed_alerts)
            print(f"    Saved daily archive locally: {archive_local_path} ({len(parsed_alerts)} warnings)")
            
            # R2 save
            if r2:
                try:
                    r2_put_json(r2, archive_key, parsed_alerts)
                    print(f"    Uploaded daily archive to R2: {archive_key}")
                except Exception as e:
                    print(f"    Error uploading daily archive to R2: {e}")
        else:
            # If there are no warnings, ensure we don't have an old file locally/R2 with EA alerts
            if os.path.exists(archive_local_path):
                os.remove(archive_local_path)
            if r2:
                try:
                    # Check if it exists in R2 and delete it to keep it clean
                    r2.delete_object(Bucket=R2_BUCKET, Key=archive_key)
                    print(f"    Deleted empty archive from R2: {archive_key}")
                except Exception:
                    pass

        current_day += delta
        time.sleep(1.0) # Sleep between days

    print("Backfill completed successfully!")

if __name__ == "__main__":
    main()
