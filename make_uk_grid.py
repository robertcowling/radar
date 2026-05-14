"""
Generate uk_grid_20km.geojson — a 20km × 20km grid covering the UK
in British National Grid (OSGB36 EPSG:27700), exported as WGS84 GeoJSON.

Each cell is named by its SW corner in km: "E020N060" etc.
Run once: python make_uk_grid.py
"""
import json
from pyproj import Transformer

# BNG → WGS84
to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)

STEP = 20_000  # 20 km in metres

# UK bounding box in OSGB36 metres (covers mainland + islands)
E_MIN, E_MAX = 0,       700_000
N_MIN, N_MAX = 0,     1_300_000

features = []
col = 0
for e in range(E_MIN, E_MAX, STEP):
    row = 0
    for n in range(N_MIN, N_MAX, STEP):
        # four corners: SW, SE, NE, NW → close ring
        corners_bng = [
            (e,        n),
            (e+STEP,   n),
            (e+STEP,   n+STEP),
            (e,        n+STEP),
            (e,        n),          # close
        ]
        ring = []
        for ex, nx in corners_bng:
            lon, lat = to_wgs84.transform(ex, nx)
            ring.append([round(lon, 6), round(lat, 6)])

        e_km = e // 1000
        n_km = n // 1000
        name = f"E{e_km:03d}N{n_km:04d}"
        features.append({
            "type": "Feature",
            "properties": {"name": name, "e_km": e_km, "n_km": n_km},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
        row += 1
    col += 1

gj = {"type": "FeatureCollection", "features": features}
path = "uk_grid_20km.geojson"
with open(path, "w") as f:
    json.dump(gj, f, separators=(",", ":"))

print(f"Written {len(features)} cells to {path}")
