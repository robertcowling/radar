#!/usr/bin/env python3
"""
build_nrw_floodareas.py — Natural Resources Wales flood-area reference builder.

Unlike the EA (fetch_ea_floodareas.py), NRW has no live per-area polygon API —
the flood warning area boundaries are only published as a periodic GIS export
on DataMapWales (https://datamap.gov.wales/), catalogue record:
  https://datamap.gov.wales/catalogue/csw?request=GetRecordById&service=CSW&
  version=2.0.2&id=8d999481-b7a3-464c-91ce-9d92fe613fa3&outputschema=...

So this is a manual/occasional script, not a scheduled cron job: run it
whenever you download a fresh export (a WFS GetFeature response saved as
GeoJSON) from DataMapWales, pointing --input at that file.

Source file facts (confirmed by inspecting a real export, not assumed):
  - Single FeatureCollection, id prefix "NRW_FLOOD_WARNING" — this dataset is
    FLOOD *WARNING* areas only. NRW does not appear to publish a separate
    flood *alert* area polygon layer alongside this one (unlike EA, which has
    both). The live warnings API's "Flood Alert" severity entries will
    therefore have no matching polygon here — they still get a marker via
    the live API's own lat/lon (see fetch_nrw_warnings.py), just no shape.
  - CRS: EPSG:27700 (British National Grid) — reprojected to WGS84 here.
  - Geometry: MultiPolygon for every feature.
  - Key properties: fwd_tacode (join key vs the live API's FWACODE), fwa_name,
    descrip, river_sea, area (NRW region: Northern/Southern etc.), region
    ("Wales" for all rows in the sample).

Outputs (R2 keys, mirroring fetch_ea_floodareas.py's ea/floodareas/ layout):
  nrw/floodareas/index.json               — code, label, description, river, area, lat/long, type
  nrw/floodareas/centroids.geojson        — Point features
  nrw/floodareas/polygons/{code}.json     — per-area full-res polygon (WGS84)
  nrw/floodareas/polygons_all.min.geojson — simplified combined set (overview)

Attribution (per the NRW API portal's stated terms): "Contains Natural
Resources Wales information © Natural Resources Wales and Database Right.
All rights Reserved." Licensed for reuse (including commercial) under the
Open Government Licence for Public Sector Information.
"""
import argparse
import json
import os
from datetime import datetime, timezone

from pyproj import Transformer

# ── Cloudflare R2 config (same convention as the other fetch_*.py scripts) ────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

PFX           = "nrw/floodareas"
INDEX_KEY     = f"{PFX}/index.json"
CENTROIDS_KEY = f"{PFX}/centroids.geojson"
POLY_PFX      = f"{PFX}/polygons"
COMBINED_KEY  = f"{PFX}/polygons_all.min.geojson"

# Same simplification tolerances as fetch_ea_floodareas.py, for a visually
# consistent overview layer between the two agencies.
OVERVIEW_TOL      = 0.0015
OVERVIEW_DP       = 4
OVERVIEW_MIN_RING = 0.0008


# ── R2 helpers (identical pattern to fetch_ea_floodareas.py) ──────────────────
def get_r2_client():
    if not USE_R2:
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    except ImportError:
        print("boto3 not installed. Cloudflare R2 will be bypassed.")
        return None


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")


def write_local(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


# ── Reprojection (BNG → WGS84) ─────────────────────────────────────────────────
_to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def reproject_ring(ring):
    return [list(_to_wgs84.transform(x, y)) for x, y in ring]


def reproject_geometry(geom):
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        return {"type": "Polygon", "coordinates": [reproject_ring(r) for r in coords]}
    if gtype == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[reproject_ring(r) for r in poly] for poly in coords]}
    raise ValueError(f"Unexpected geometry type: {gtype}")


def centroid_of(geom):
    """Simple vertex-average centroid of the largest ring — good enough for
    marker placement, not a true area-weighted geometric centroid."""
    gtype = geom.get("type")
    rings = ([geom["coordinates"][0]] if gtype == "Polygon"
              else [poly[0] for poly in geom["coordinates"]])
    largest = max(rings, key=len)
    lons = [p[0] for p in largest]
    lats = [p[1] for p in largest]
    return sum(lats) / len(lats), sum(lons) / len(lons)


# ── Geometry simplification (verbatim from fetch_ea_floodareas.py) ───────────
def _rdp(points, tol):
    n = len(points)
    if n < 3:
        return points[:]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        s, e = stack.pop()
        ax, ay = points[s]
        bx, by = points[e]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        dmax, idx = -1.0, -1
        for i in range(s + 1, e):
            px, py = points[i]
            if seg2 == 0:
                ddx, ddy = px - ax, py - ay
                d2 = ddx * ddx + ddy * ddy
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                cx, cy = ax + t * dx, ay + t * dy
                ddx, ddy = px - cx, py - cy
                d2 = ddx * ddx + ddy * ddy
            if d2 > dmax:
                dmax, idx = d2, i
        if dmax > tol2 and idx != -1:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [points[i] for i in range(n) if keep[i]]


def _ring_bbox_span(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def simplify_ring(ring, tol, dp):
    if _ring_bbox_span(ring) < OVERVIEW_MIN_RING:
        return None
    simp = _rdp(ring, tol)
    out, last = [], None
    for pt in simp:
        q = [round(pt[0], dp), round(pt[1], dp)]
        if q != last:
            out.append(q)
            last = q
    if len(out) >= 1 and out[0] != out[-1]:
        out.append(out[0][:])
    return out if len(out) >= 4 else None


def simplify_geometry(geom, tol=OVERVIEW_TOL, dp=OVERVIEW_DP):
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        rings = [r for r in (simplify_ring(ring, tol, dp) for ring in coords) if r]
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if gtype == "MultiPolygon":
        polys = []
        for poly in coords:
            rings = [r for r in (simplify_ring(ring, tol, dp) for ring in poly) if r]
            if rings:
                polys.append(rings)
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    return geom


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="features.json",
                     help="Path to the DataMapWales WFS GeoJSON export (default: features.json)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    r2 = get_r2_client()
    print("Connected to Cloudflare R2." if r2 else "R2 not configured — local dry run.")

    print(f"Loading {args.input}...")
    with open(args.input, encoding="utf-8") as f:
        src = json.load(f)
    src_features = src.get("features", [])
    print(f"  {len(src_features)} source feature(s), CRS: {src.get('crs')}")

    index = []
    centroid_features = []
    combined_features = []
    for feat in src_features:
        props = feat.get("properties", {})
        code = props.get("fwd_tacode") or props.get("fwis_code")
        if not code or not feat.get("geometry"):
            continue

        geom = reproject_geometry(feat["geometry"])
        lat, lon = centroid_of(geom)

        rec = {
            "code": code,
            "label": props.get("fwa_name", ""),
            "description": props.get("descrip", ""),
            "river": props.get("river_sea", ""),
            "area": props.get("area", ""),        # NRW region, e.g. "NRW Northern"
            "region": props.get("region", "Wales"),
            "lat": round(lat, 5),
            "long": round(lon, 5),
            "type": "warning",   # this source only contains flood WARNING areas — see module docstring
        }
        index.append(rec)

        centroid_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [rec["long"], rec["lat"]]},
            "properties": {"code": code, "label": rec["label"], "river": rec["river"],
                            "area": rec["area"], "type": rec["type"]},
        })

        if r2:
            r2_put_json(r2, f"{POLY_PFX}/{code}.json", {
                "type": "Feature", "geometry": geom,
                "properties": {"code": code, "label": rec["label"], "type": rec["type"]},
            })

        sgeom = simplify_geometry(geom)
        if sgeom:
            combined_features.append({
                "type": "Feature", "geometry": sgeom,
                "properties": {"code": code, "label": rec["label"], "river": rec["river"],
                                "area": rec["area"], "type": rec["type"]},
            })

    index_obj = {"generated_at": now.isoformat(), "count": len(index), "areas": index}
    centroids_obj = {"type": "FeatureCollection", "features": centroid_features}
    combined_obj = {"type": "FeatureCollection", "generated_at": now.isoformat(),
                     "features": combined_features}

    write_local("nrw/floodareas/index.json", index_obj)
    write_local("nrw/floodareas/centroids.geojson", centroids_obj)
    write_local("nrw/floodareas/polygons_all.min.geojson", combined_obj)

    if r2:
        r2_put_json(r2, INDEX_KEY, index_obj)
        r2_put_json(r2, CENTROIDS_KEY, centroids_obj)
        r2_put_json(r2, COMBINED_KEY, combined_obj)
        body = json.dumps(combined_obj, separators=(",", ":")).encode("utf-8")
        print(f"  Uploaded index.json + centroids.geojson + combined overview "
              f"({len(combined_features)} features, {len(body)/1_048_576:.1f} MB) "
              f"+ {len(index)} individual polygons.")

    print(f"NRW flood-area reference build complete: {len(index)} area(s).")


if __name__ == "__main__":
    main()
