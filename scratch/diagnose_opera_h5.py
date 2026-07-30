import urllib.request
import h5py
import os

def diagnose():
    url = "https://s3.waw3-1.cloudferro.com/openradar-24h/2026/05/30/OPERA/COMP/OPERA@20260530T1945@0@DBZH.h5"
    h5_path = "scratch/opera_test.h5"
    
    print(f"Downloading {url} to {h5_path}...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as response, open(h5_path, 'wb') as out:
            out.write(response.read())
        print("Download complete.")
        
        # Now let's open and inspect
        with h5py.File(h5_path, "r") as f:
            print("H5 Keys:", list(f.keys()))
            
            # Print attributes of root
            print("\nRoot Attributes:")
            for k, v in f.attrs.items():
                print(f"  {k}: {v}")
                
            # Print groups like 'where', 'how', 'what' if they exist
            for group_name in ['where', 'how', 'what']:
                if group_name in f:
                    print(f"\nGroup '{group_name}' Attributes:")
                    for k, v in f[group_name].attrs.items():
                        print(f"  {k}: {v}")
                        
            # Check dataset1
            if 'dataset1' in f:
                print("\ndataset1 Keys:", list(f['dataset1'].keys()))
                if 'data1' in f['dataset1']:
                    print("dataset1/data1 Keys:", list(f['dataset1']['data1'].keys()))
                    data = f['dataset1/data1/data']
                    print(f"data shape: {data.shape}, dtype: {data.dtype}")
                    if 'what' in f['dataset1/data1']:
                        print("dataset1/data1/what Attributes:")
                        for k, v in f['dataset1/data1/what'].attrs.items():
                            print(f"  {k}: {v}")
                            
    except Exception as e:
        print("Error:", e)
    finally:
        if os.path.exists(h5_path):
            try:
                # keep it for a moment, or we can delete it
                pass
            except Exception:
                pass

if __name__ == "__main__":
    diagnose()
