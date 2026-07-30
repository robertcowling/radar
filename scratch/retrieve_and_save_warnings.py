import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import re

METEOGATE_API_KEY = "6a52afd3ac0e491f265f331a52de29eb04a559787e8ff2777682f3ca88619bc8"

def find_features_in_chunks():
    chunks = [
        ("2026-05-27T12:41:24Z", "2026-05-28T00:41:24Z"),
        ("2026-05-27T00:41:24Z", "2026-05-27T12:41:24Z")
    ]
    
    headers = {
        'apikey': METEOGATE_API_KEY,
        'User-Agent': 'Mozilla/5.0'
    }
    
    found_feats = {}
    for start_str, end_str in chunks:
        url = f"https://api.meteogate.eu/warnings/collections/warnings/locations/UK?datetime={start_str}/{end_str}"
        print(f"Querying: {url}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    print(f"  Non-200 response: {response.status}")
                    continue
                data = json.loads(response.read().decode("utf-8"))
                features = data.get("features", [])
                for f in features:
                    alert_id = f.get("properties", {}).get("alertId")
                    if alert_id:
                        found_feats[alert_id] = f
        except Exception as e:
            print(f"  Error: {e}")
            
    return found_feats

def parse_and_fetch_details(feat):
    alert_id = feat.get("properties", {}).get("alertId")
    links = feat.get("links", [])
    
    json_url = None
    geojson_url = None
    
    for l in links:
        if l.get("type") == "application/json" and l.get("rel") == "json":
            json_url = l.get("href")
        elif l.get("type") == "application/geo+json" and l.get("rel") == "geometry":
            geojson_url = l.get("href")
            
    if not json_url or not geojson_url:
        print(f"  Missing json_url or geojson_url links for {alert_id}!")
        return None
        
    print(f"  Fetching JSON metadata from: {json_url}")
    meta_req = urllib.request.Request(json_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(meta_req, timeout=15) as meta_res:
        meta = json.loads(meta_res.read().decode("utf-8"))
        
    print(f"  Fetching GeoJSON geometry from: {geojson_url}")
    geom_req = urllib.request.Request(geojson_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(geom_req, timeout=15) as geom_res:
        geom_data = json.loads(geom_res.read().decode("utf-8"))
        
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
        
    # Tags extraction
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
    found_feats = find_features_in_chunks()
    print(f"Found {len(found_feats)} unique alerts on API.")
    
    parsed_alerts = []
    for alert_id, feat in found_feats.items():
        parsed = parse_and_fetch_details(feat)
        if parsed:
            parsed_alerts.append(parsed)
            print(f"Successfully processed {alert_id}!")
            print(f"  Title: {parsed['title']}")
            print(f"  Headline: {parsed['headline']}")
            print(f"  Start: {parsed['start_date']}")
            print(f"  End: {parsed['end_date']}")
                
    if parsed_alerts:
        # Load the existing archive for yesterday (20260527)
        archive_path = "warnings/archive/warnings_20260527.json"
        try:
            with open(archive_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
            
        merged = {a["alert_id"]: a for a in existing}
        for a in parsed_alerts:
            merged[a["alert_id"]] = a
            
        final_list = list(merged.values())
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(final_list, f, separators=(",", ":"))
            
        print(f"\nMerged warnings. Yesterday's archive now has {len(final_list)} warnings total.")
        print(f"Updated local file: {archive_path}")

if __name__ == "__main__":
    main()
