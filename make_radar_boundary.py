#!/usr/bin/env python3
"""
Extract the radar coverage boundary from a recent radar PNG and save as GeoJSON.
Run once: python make_radar_boundary.py
The resulting radar_boundary.geojson should be committed to the repo.
"""
import json, urllib.request, io
import numpy as np
from PIL import Image
from pyproj import Transformer
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

LON_MIN, LON_MAX = -11.5, 3.5
LAT_MIN, LAT_MAX = 49.0, 61.5
WIDTH, HEIGHT = 2400, 2000

with open('frames.json') as f:
    frames = json.load(f)
latest_url = frames[-1]['url']
print(f"Fetching: {latest_url}")
req = urllib.request.Request(latest_url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req) as r:
    data = r.read()

img = Image.open(io.BytesIO(data)).convert('RGBA')
alpha = np.array(img)[:, :, 3].astype(float)  # shape (2000, 2400)

fig, ax = plt.subplots()
cs = ax.contour(alpha, levels=[127])
plt.close()

segs = cs.allsegs[0]  # list of (N, 2) arrays for level 0
if not segs:
    print("No contour paths found — is the PNG fully transparent?")
    raise SystemExit(1)

# Take the longest segment — that's the outer coverage boundary
vertices = max(segs, key=len)  # (col, row) pairs

ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
merc_to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)  # row 0 = north

# Downsample to ~500 points
step = max(1, len(vertices) // 500)
sampled = vertices[::step]

coords = []
for col, row in sampled:
    col_i = int(round(col))
    row_i = int(round(row))
    col_i = max(0, min(col_i, WIDTH - 1))
    row_i = max(0, min(row_i, HEIGHT - 1))
    lon, lat = merc_to_ll.transform(merc_xs[col_i], merc_ys[row_i])
    coords.append([round(lon, 5), round(lat, 5)])

# Close the ring
if coords[0] != coords[-1]:
    coords.append(coords[0])

geojson = {
    "type": "Feature",
    "properties": {},
    "geometry": {
        "type": "Polygon",
        "coordinates": [coords]
    }
}

with open('radar_boundary.geojson', 'w') as f:
    json.dump(geojson, f)
print(f"Saved radar_boundary.geojson ({len(coords)} points)")
