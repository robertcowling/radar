#!/usr/bin/env python3
"""One-off: reproject rainfallsummarypolygon.json (BNG) to WGS84 and write
rainfall_summary_areas.geojson at the repo root, using the same name_key
convention (SHORT_NAME/NAME) as the other reference boundary layers."""
import json
from pyproj import Transformer

to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def reproject_ring(ring):
    return [list(to_wgs84.transform(x, y)) for x, y in ring]


def reproject_geometry(geom):
    gtype = geom["type"]
    coords = geom["coordinates"]
    if gtype == "Polygon":
        return {"type": "Polygon", "coordinates": [reproject_ring(r) for r in coords]}
    if gtype == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[reproject_ring(r) for r in poly] for poly in coords]}
    raise ValueError(gtype)


with open("rainfallsummarypolygon.json", encoding="utf-8") as f:
    src = json.load(f)

out_features = []
for feat in src["features"]:
    props = feat["properties"]
    out_features.append({
        "type": "Feature",
        "geometry": reproject_geometry(feat["geometry"]),
        "properties": {
            "name": props.get("NAME", ""),
            "short_name": props.get("SHORT_NAME", ""),
            "region": props.get("REGION", ""),
        },
    })

out = {"type": "FeatureCollection", "features": out_features}
with open("rainfall_summary_areas.geojson", "w", encoding="utf-8") as f:
    json.dump(out, f, separators=(",", ":"))

print(f"Wrote rainfall_summary_areas.geojson ({len(out_features)} zones)")
