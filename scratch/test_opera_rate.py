import urllib.request
import urllib.error
import json

def test():
    # Pass parameter-name=RATE:comp to get the rain rate composite
    url = "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations/0-20010-0-OPERA?parameter-name=RATE:comp"
    print(f"Fetching {url}...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
            print(f"Status Code: {status}")
            data = json.loads(response.read().decode('utf-8'))
            coverages = data.get("coverages", [])
            print(f"Found {len(coverages)} coverages for RATE:comp.")
            
            if coverages:
                first = coverages[0]
                links = first.get("links", [])
                print(f"Found {len(links)} data links in the first coverage.")
                # Filter links for RATE
                rate_links = [l for l in links if "RATE.h5" in l.get("href", "")]
                print(f"Found {len(rate_links)} links containing 'RATE.h5'.")
                if rate_links:
                    print("Sample rate link:", rate_links[-1])
                    
                    # Let's download a sample RATE.h5 file to inspect its gain/offset and shape!
                    sample_url = rate_links[-1]["href"]
                    h5_path = "scratch/opera_rate_test.h5"
                    print(f"Downloading sample: {sample_url}...")
                    with urllib.request.urlopen(urllib.request.Request(sample_url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60) as r_file, open(h5_path, 'wb') as out:
                        out.write(r_file.read())
                    print("Download complete.")
                    
                    import h5py
                    with h5py.File(h5_path, 'r') as f:
                        print("Keys in H5:", list(f.keys()))
                        if 'dataset1/data1/what' in f:
                            print("Attributes of dataset1/data1/what:")
                            for k, v in f['dataset1/data1/what'].attrs.items():
                                print(f"  {k}: {v}")
                        if 'dataset1/data1/data' in f:
                            data_arr = f['dataset1/data1/data']
                            print(f"Data shape: {data_arr.shape}, dtype: {data_arr.dtype}")
                            
                else:
                    print("All links in first coverage:")
                    for l in links[:5]:
                        print(" -", l.get("href"))
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
