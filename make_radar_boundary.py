#!/usr/bin/env python3
"""
Extract the radar coverage boundary from the ODIM H5 composite and save as GeoJSON.

The PNG alpha channel can't be used because it collapses nodata (outside coverage)
and undetect (inside coverage, no rain) both to transparent. The H5 file has them
as distinct values, so we can correctly identify the coverage area.

Run once: python make_radar_boundary.py
Commit the resulting radar_boundary.geojson to the repo.
"""
import os, json
import numpy as np
import h5py
import boto3
from botocore import UNSIGNED
from botocore.client import Config
from pyproj import Transformer
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

LON_MIN, LON_MAX = -11.5, 3.5
LAT_MIN, LAT_MAX = 49.0, 61.5
WIDTH, HEIGHT = 2400, 2000
BUCKET = "met-office-radar-obs-data"
H5_DIR = "data_h5"


def get_mapping(h5_path):
    with h5py.File(h5_path, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes): proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)
    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)
    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)
    ll_to_radar = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)
    cols = np.round((X_radar - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Y_radar) / yscale).astype(np.int32)
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    return rows, cols, valid


# --- Download latest H5 from Met Office S3 ---
s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
os.makedirs(H5_DIR, exist_ok=True)

for delta in (0, 1):
    day = datetime.utcnow() - timedelta(days=delta)
    prefix = f"radar/{day.strftime('%Y/%m/%d')}/"
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    keys = sorted(
        o['Key'] for o in resp.get('Contents', [])
        if o['Key'].endswith('radar_rainrate_composite_1km_UK.h5')
    )
    if keys:
        break

if not keys:
    print("No H5 files found on S3.")
    raise SystemExit(1)

latest_key = keys[-1]
h5_path = os.path.join(H5_DIR, os.path.basename(latest_key))
print(f"Downloading {latest_key}...")
s3.download_file(BUCKET, latest_key, h5_path)

# --- Build coverage mask in Web Mercator output space ---
rows, cols, valid = get_mapping(h5_path)

with h5py.File(h5_path, "r") as f:
    raw = f['dataset1/data1/data'][:]
    nodata = float(f['dataset1/data1/what'].attrs['nodata'])

os.remove(h5_path)

out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
out_raw[valid] = raw[rows[valid], cols[valid]]

# coverage = pixels within radar range (nodata = outside, undetect/data = inside)
coverage = ((out_raw != nodata) & valid).astype(float)

# --- Trace the outer boundary ---
fig, ax = plt.subplots()
cs = ax.contour(coverage, levels=[0.5])
plt.close()

segs = cs.allsegs[0]
if not segs:
    print("No contour found.")
    raise SystemExit(1)

vertices = max(segs, key=len)  # longest segment = outer coverage boundary
print(f"Outer boundary: {len(vertices)} raw vertices")

# --- Convert pixel (col, row) → Mercator → lat/lon ---
ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
merc_to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)

step = max(1, len(vertices) // 600)
coords = []
for col, row in vertices[::step]:
    col_i = max(0, min(int(round(col)), WIDTH - 1))
    row_i = max(0, min(int(round(row)), HEIGHT - 1))
    lon, lat = merc_to_ll.transform(merc_xs[col_i], merc_ys[row_i])
    coords.append([round(lon, 5), round(lat, 5)])

if coords[0] != coords[-1]:
    coords.append(coords[0])

geojson = {
    "type": "Feature",
    "properties": {},
    "geometry": {"type": "Polygon", "coordinates": [coords]}
}
with open('radar_boundary.geojson', 'w') as f:
    json.dump(geojson, f)
print(f"Saved radar_boundary.geojson ({len(coords)} points)")
