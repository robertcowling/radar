import os
import json
import boto3
from pyproj import Transformer

# Load R2 config from tools/.env or env
env_vars = {}
try:
    if os.path.exists("tools/.env"):
        with open("tools/.env", "r") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env_vars[k.strip()] = v.strip()
except Exception as e:
    print(f"Error reading .env: {e}")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID") or env_vars.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID") or env_vars.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY") or env_vars.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET_NAME") or env_vars.get("R2_BUCKET_NAME", "")

print("R2 Config:")
print(f"  Account ID: {R2_ACCOUNT_ID[:6]}... (length: {len(R2_ACCOUNT_ID)})")
print(f"  Access Key ID: {R2_ACCESS_KEY_ID[:6]}... (length: {len(R2_ACCESS_KEY_ID)})")
print(f"  Bucket: {R2_BUCKET}")

# Create Transformer
transformer = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)

def project_coords(coords, geom_type):
    if geom_type == "Polygon":
        new_coords = []
        for ring in coords:
            new_ring = []
            for pt in ring:
                if len(pt) >= 2:
                    lon, lat = transformer.transform(pt[0], pt[1])
                    new_ring.append([round(lon, 6), round(lat, 6)])
            new_coords.append(new_ring)
        return new_coords
    elif geom_type == "MultiPolygon":
        new_coords = []
        for poly in coords:
            new_poly = []
            for ring in poly:
                new_ring = []
                for pt in ring:
                    if len(pt) >= 2:
                        lon, lat = transformer.transform(pt[0], pt[1])
                        new_ring.append([round(lon, 6), round(lat, 6)])
                new_poly.append(new_ring)
            new_coords.append(new_poly)
        return new_coords
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")

# Convert
with open("rainfallsummarypolygon.json") as f:
    gj = json.load(f)

for feat in gj.get("features", []):
    geom = feat["geometry"]
    geom_type = geom["type"]
    coords = geom["coordinates"]
    geom["coordinates"] = project_coords(coords, geom_type)

out_file = "rainfallsummarypolygons.geojson"
with open(out_file, "w") as f:
    json.dump(gj, f, separators=(',', ':'))

print(f"Successfully converted and saved to {out_file}")

# Upload to R2 if credentials present
if all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET]):
    r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )
    
    r2_key = "geo/rainfallsummarypolygons.geojson"
    print(f"Uploading to R2: {r2_key}...")
    r2.upload_file(out_file, R2_BUCKET, r2_key)
    print("Upload complete!")
else:
    print("Warning: Missing some R2 credentials. Locally saved file only.")
