import urllib.request
import urllib.error
import json

def test():
    url = "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations/0-20010-0-OPERA"
    print(f"Fetching {url}...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
            data = json.loads(response.read().decode('utf-8'))
            print("Response keys:", list(data.keys()))
            coverages = data.get("coverages", [])
            print(f"Found {len(coverages)} coverages.")
            if coverages:
                first = coverages[0]
                print("\nKeys in first coverage item:", list(first.keys()))
                print("First coverage ID:", first.get("id"))
                print("First coverage type:", first.get("type"))
                
                # Check domain
                domain = first.get("domain", {})
                print("Domain keys:", list(domain.keys()))
                axes = domain.get("axes", {})
                print("Axes keys:", list(axes.keys()))
                for ax_name, ax_val in axes.items():
                    print(f"  Axis '{ax_name}': values length={len(ax_val.get('values', []))}, dataType={ax_val.get('dataType')}")
                    if ax_name == 't':
                        print(f"    Sample times: {ax_val.get('values')[:5]}")
                
                # Check parameters
                params = first.get("parameters", {})
                print("Parameters in coverage:", list(params.keys()))
                
                # Check ranges
                ranges = first.get("ranges", {})
                print("Ranges keys:", list(ranges.keys()))
                for param_name, r_val in ranges.items():
                    print(f"  Range '{param_name}': type={type(r_val)}, keys={list(r_val.keys()) if isinstance(r_val, dict) else 'not-dict'}")
                    if isinstance(r_val, dict):
                        print(f"    Values (first 10): {r_val.get('values')[:10] if r_val.get('values') else 'none'}")
                        
                # Let's see if there are links or files in the coverage item
                print("Links in first coverage:", first.get("links", []))
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
