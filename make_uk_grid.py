"""
Generate uk_grid_20km.geojson — a 20km × 20km grid covering the UK land area.

Cells are aligned to the British National Grid (OSGB36 EPSG:27700) and clipped
to the UK land boundary (derived from uk_regions.geojson union), so only cells
that intersect land are included.

Run once: py make_uk_grid.py
"""
import json
from pyproj import Transformer
from shapely.geometry import shape, box
from shapely.ops import unary_union

# Load UK land boundary from regions GeoJSON
with open("uk_regions.geojson") as f:
    regions = json.load(f)

from shapely.validation import make_valid
uk_boundary = unary_union([make_valid(shape(feat["geometry"])) for feat in regions["features"]])
print(f"UK boundary loaded ({len(regions['features'])} regions)")

# BNG → WGS84
to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
# WGS84 → BNG (for boundary reprojection)
to_bng = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

# Reproject UK boundary to BNG for intersection test
from shapely.ops import transform as shp_transform
uk_bng = shp_transform(lambda x, y: to_bng.transform(x, y), uk_boundary)

STEP = 20_000  # 20 km in metres

E_MIN, E_MAX = 0,       700_000
N_MIN, N_MAX = 0,     1_300_000

features = []
skipped = 0
for e in range(E_MIN, E_MAX, STEP):
    for n in range(N_MIN, N_MAX, STEP):
        cell_bng = box(e, n, e + STEP, n + STEP)
        if not uk_bng.intersects(cell_bng):
            skipped += 1
            continue

        corners_bng = [
            (e,        n),
            (e+STEP,   n),
            (e+STEP,   n+STEP),
            (e,        n+STEP),
            (e,        n),
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

gj = {"type": "FeatureCollection", "features": features}
path = "uk_grid_20km.geojson"
with open(path, "w") as f:
    json.dump(gj, f, separators=(",", ":"))

print(f"Written {len(features)} land cells to {path}  ({skipped} sea cells excluded)")
