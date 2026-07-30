import urllib.request
import urllib.error
import json

def test():
    # Let's request the locations list. We can try to get them.
    # Note: bbox for OPERA is very large, but let's query all locations.
    url = "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations"
    print(f"Fetching {url}...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
            print(f"Status Code: {status}")
            data = json.loads(response.read().decode('utf-8'))
            print("Response keys:", data.keys())
            features = data.get("features", [])
            print(f"Found {len(features)} locations in total.")
            # Print first 20 locations
            print("First 20 locations:")
            for feat in features[:20]:
                props = feat.get("properties", {})
                loc_id = feat.get("id") or props.get("id")
                print(f"- ID: {loc_id}, Name: {props.get('name')}, Type: {props.get('type')}")
            
            # Let's look for composite locations
            comp_locs = [f for f in features if "comp" in str(f.get("id")).lower() or "opera" in str(f.get("id")).lower() or "uk" in str(f.get("id")).lower()]
            print(f"\nFound {len(comp_locs)} locations matching 'comp', 'opera', or 'uk':")
            for feat in comp_locs[:20]:
                props = feat.get("properties", {})
                loc_id = feat.get("id") or props.get("id")
                print(f"- ID: {loc_id}, Name: {props.get('name')}, Type: {props.get('type')}")
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} - {e.reason}")
        try:
            print("Error body:", e.read().decode('utf-8'))
        except Exception:
            pass
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    test()
