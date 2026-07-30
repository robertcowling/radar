import urllib.request
import urllib.error
import json

def test():
    # Construct query parameters exactly per the official documentation:
    # standard_name=RATE, method=comp, format=ODIM
    url = (
        "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations/0-20010-0-OPERA"
        "?standard_name=RATE&method=comp&format=ODIM"
    )
    print(f"Fetching: {url}")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
            print(f"Status Code: {status}")
            data = json.loads(response.read().decode('utf-8'))
            coverages = data.get("coverages", [])
            print(f"Found {len(coverages)} coverages.")
            if coverages:
                first = coverages[0]
                links = first.get("links", [])
                print(f"Found {len(links)} links in the first coverage.")
                
                # Check for RATE.h5 files
                rate_links = [l for l in links if l.get("href", "").endswith("RATE.h5")]
                print(f"Number of RATE.h5 links: {len(rate_links)}")
                if rate_links:
                    print("Sample RATE link:", rate_links[-1])
                    
                    # Let's download a sample to inspect
                    sample_url = rate_links[-1]["href"]
                    print(f"Downloading {sample_url}...")
                    h5_path = "scratch/opera_rate_sample.h5"
                    with urllib.request.urlopen(urllib.request.Request(sample_url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60) as r_file, open(h5_path, 'wb') as out:
                        out.write(r_file.read())
                    print("Download complete.")
                    
                    import h5py
                    with h5py.File(h5_path, 'r') as f:
                        print("Keys:", list(f.keys()))
                        if 'dataset1/data1/what' in f:
                            print("dataset1/data1/what attributes:")
                            for k, v in f['dataset1/data1/what'].attrs.items():
                                print(f"  {k}: {v}")
                        if 'dataset1/data1/data' in f:
                            arr = f['dataset1/data1/data']
                            print(f"Shape: {arr.shape}, dtype: {arr.dtype}, min: {arr[:].min()}, max: {arr[:].max()}")
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} - {e.reason}")
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    test()
