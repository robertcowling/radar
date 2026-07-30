import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

METEOGATE_API_KEY = "6a52afd3ac0e491f265f331a52de29eb04a559787e8ff2777682f3ca88619bc8"

def check_window(start_str, end_str):
    url = f"https://api.meteogate.eu/warnings/collections/warnings/locations/UK?datetime={start_str}/{end_str}"
    headers = {
        'apikey': METEOGATE_API_KEY,
        'User-Agent': 'Mozilla/5.0'
    }
    
    print(f"Querying: {url}")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            if response.status != 200:
                print(f"  Non-200 response: {response.status}")
                return []
            data = json.loads(response.read().decode("utf-8"))
            features = data.get("features", [])
            print(f"  Found {len(features)} features.")
            for f in features:
                props = f.get("properties", {})
                alert_id = props.get("alertId")
                onset = props.get("onset")
                expires = props.get("expires")
                event = props.get("event")
                # Wait, EUMETNET sometimes has event in nested info or in properties. Let's print properties
                print(f"    - AlertID: {alert_id}")
                print(f"      Onset: {onset}, Expires: {expires}")
                print(f"      Properties: {json.dumps({k: v for k, v in props.items() if k not in ['geometry', 'coordinates']}, indent=2)}")
                # Fetch detailed JSON if links exist
                for link in f.get("links", []):
                    if link.get("type") == "application/json" and link.get("rel") == "json":
                        try:
                            meta_req = urllib.request.Request(link.get("href"), headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(meta_req, timeout=10) as meta_res:
                                meta = json.loads(meta_res.read().decode("utf-8"))
                                info = meta.get("info", [{}])[0]
                                print(f"      Detail Title: {info.get('event')}")
                                print(f"      Detail Headline: {info.get('headline')}")
                                print(f"      Detail Severity: {info.get('severity')}")
                                print(f"      Detail Area: {info.get('area', [{}])[0].get('areaDesc')}")
                        except Exception as e:
                            print(f"      Error fetching detail: {e}")
            return features
    except Exception as e:
        print(f"  Error: {e}")
        return []

def main():
    now = datetime.now(timezone.utc)
    # Query past 36 hours in 12-hour chunks to avoid EDR 24-hour limit
    for i in range(3):
        start = now - timedelta(hours=12 * (i + 1))
        end = now - timedelta(hours=12 * i)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"\n--- Checking chunk: {start_str} to {end_str} ---")
        check_window(start_str, end_str)

if __name__ == "__main__":
    main()
