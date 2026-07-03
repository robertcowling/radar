#!/usr/bin/env python3
"""
fetch_ea_floodareas.py — Environment Agency flood-area reference builder.

Flood *areas* (the geographic alert/warning polygons) are stable, long-lived
reference data, so we fetch them rarely (run weekly) and mirror them to R2 once.
The frequent warnings fetcher (fetch_ea_warnings.py) then only has to write tiny
state files, keeping per-run storage cost negligible.

Outputs (R2 keys, also mirrored as noted):
  ea/floodareas/index.json           — all areas: code, label, county, river, lat/long, type, eaArea
  ea/floodareas/centroids.geojson    — Point features (for icon display)
  ea/floodareas/polygons/{code}.json — per-area full-res polygon (fetched once, incremental)
  ea/floodareas/polygons_all.min.geojson — simplified combined set (overview / all-polys page)

Source API: https://environment.data.gov.uk/flood-monitoring (Open Government Licence).
Attribution: this uses Environment Agency flood and river level data from the
real-time data API (Beta).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

from geo_membership import MembershipIndex, classify_category

# ── Cloudflare R2 config (mirrors fetch_warnings.py) ──────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

API_ROOT      = "https://environment.data.gov.uk/flood-monitoring"
PFX           = "ea/floodareas"
INDEX_KEY     = f"{PFX}/index.json"
CENTROIDS_KEY = f"{PFX}/centroids.geojson"
POLY_PFX      = f"{PFX}/polygons"
COMBINED_KEY  = f"{PFX}/polygons_all.min.geojson"

# Simplified-overview geometry: Douglas-Peucker tolerance in degrees (~0.0015 ≈
# 150 m) to cut vertex *count*, then round coords to OVERVIEW_DP places. Rings
# whose bounding box is smaller than OVERVIEW_MIN_RING are dropped from the overview.
OVERVIEW_TOL     = 0.0015
OVERVIEW_DP      = 4
OVERVIEW_MIN_RING = 0.0008

UA = {"User-Agent": "floodforecast-radar/1.0 (+https://floodforecast.co.uk)"}

FORCE = "--force" in sys.argv  # re-fetch every polygon even if already on R2


# ── R2 helpers ────────────────────────────────────────────────────────────────
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


def r2_list_existing_polys(r2):
    """Return the set of area codes whose polygon is already on R2."""
    existing = set()
    try:
        paginator = r2.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=POLY_PFX + "/"):
            for obj in page.get("Contents", []):
                fname = obj["Key"].rsplit("/", 1)[-1]
                if fname.endswith(".json"):
                    existing.add(fname[:-5])
    except Exception as e:
        print(f"  Warning: could not list existing polygons: {e}")
    return existing


# ── HTTP helper ────────────────────────────────────────────────────────────────
def http_get_json(url, timeout=60, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


# ── Geometry simplification (pure stdlib) ────────────────────────────────────
def _rdp(points, tol):
    """Ramer-Douglas-Peucker on a polyline of [x, y] points (iterative)."""
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
    """RDP-simplify, round, and drop consecutive duplicates; keep ring closed."""
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
    """Simplify a (Multi)Polygon for the lightweight overview layer."""
    if not geom:
        return None
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


def area_type(code):
    """Flood Warning Area vs Flood Alert Area, from the TA code letters."""
    # codes like 053FWFPUWI06 (warning), 053WAF113LWA / 121FAG... (alert)
    return "warning" if code[3:5].upper() == "FW" else "alert"


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    r2 = get_r2_client()
    if r2:
        print("Connected to Cloudflare R2.")
    else:
        print("R2 not configured — local dry run (polygons will not be uploaded).")

    # 1. Fetch the full flood-area listing
    print("Fetching flood-area listing…")
    listing = http_get_json(f"{API_ROOT}/id/floodAreas?_limit=100000")
    items = listing.get("items", [])
    print(f"  {len(items)} flood areas.")

    # Optional test cap (build a small subset without fetching every polygon)
    _cap = os.environ.get("EA_AREAS_LIMIT")
    if _cap and _cap.isdigit():
        items = items[: int(_cap)]
        print(f"  EA_AREAS_LIMIT set — restricting to {len(items)} areas.")

    index = []
    for it in items:
        code = it.get("notation") or it.get("fwdCode")
        if not code:
            continue
        typ = area_type(code)
        rec = {
            "code": code,
            "label": it.get("label", ""),
            "description": it.get("description", ""),
            "county": it.get("county", ""),
            "river": it.get("riverOrSea", ""),
            "eaArea": it.get("eaAreaName", ""),
            "lat": it.get("lat"),
            "long": it.get("long"),
            "type": typ,
            "quickDial": it.get("quickDialNumber", ""),
            "parent": (it.get("floodWatchArea", "") or "").rsplit("/", 1)[-1],
        }
        index.append(rec)

    print("Loading county / rainfall-zone reference layers...")
    counties_idx = MembershipIndex("uk-counties.geojson", "name")
    rainzones_idx = MembershipIndex("rainfall_summary_areas.geojson", "short_name")

    # 2. Per-area polygons — incremental: only fetch what R2 doesn't already have.
    # Also where this enrichment (category / counties / rainfallZones) happens,
    # since it needs each area's actual geometry, which is only available once
    # fetched/cached here (the static listing above has no polygon).
    existing = set() if (FORCE or not r2) else r2_list_existing_polys(r2)
    print(f"  {len(existing)} polygons already on R2; fetching the rest"
          f"{' (FORCE refetch all)' if FORCE else ''}…")

    combined_features = []
    fetched = skipped = failed = 0
    for i, rec in enumerate(index):
        code = rec["code"]
        need_fetch = FORCE or code not in existing
        geom = None
        if need_fetch:
            try:
                fc = http_get_json(f"{API_ROOT}/id/floodAreas/{code}/polygon", timeout=45)
                feats = fc.get("features", [])
                geom = feats[0].get("geometry") if feats else None
                if geom and r2:
                    # store the raw single-feature polygon per area
                    r2_put_json(r2, f"{POLY_PFX}/{code}.json", {
                        "type": "Feature",
                        "geometry": geom,
                        "properties": {"code": code, "label": rec["label"],
                                       "type": rec["type"]},
                    })
                fetched += 1
            except Exception as e:
                failed += 1
                print(f"    ! {code}: polygon fetch failed: {e}")
        else:
            skipped += 1
            # Pull the geometry we already stored so we can build the combined file
            try:
                obj = r2.get_object(Bucket=R2_BUCKET, Key=f"{POLY_PFX}/{code}.json")
                geom = json.loads(obj["Body"].read()).get("geometry")
            except Exception:
                geom = None

        category = classify_category(rec["label"], rec["description"], rec["river"])
        counties = counties_idx.overlaps(geom) if geom else ([rec["county"]] if rec["county"] else [])
        rain_zones = rainzones_idx.overlaps(geom) if geom else []
        rec["category"] = category
        rec["counties"] = counties
        rec["rainfallZones"] = rain_zones

        if geom:
            sgeom = simplify_geometry(geom)
            if sgeom:
                combined_features.append({
                    "type": "Feature",
                    "geometry": sgeom,
                    "properties": {"code": code, "label": rec["label"],
                                   "county": rec["county"], "river": rec["river"],
                                   "eaArea": rec["eaArea"],
                                   "type": rec["type"], "category": category,
                                   "counties": counties, "rainfallZones": rain_zones},
                })
        if (i + 1) % 250 == 0:
            print(f"    …{i+1}/{len(index)} (fetched {fetched}, cached {skipped}, failed {failed})")

    print(f"  Polygons: fetched {fetched}, cached {skipped}, failed {failed}.")

    # 1b. Now that enrichment is computed, build+upload the index/centroids.
    index_obj = {"generated_at": now.isoformat(), "count": len(index), "areas": index}
    centroid_features = []
    for rec in index:
        if rec["lat"] is None or rec["long"] is None:
            continue
        centroid_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [rec["long"], rec["lat"]]},
            "properties": {"code": rec["code"], "label": rec["label"],
                           "county": rec["county"], "river": rec["river"],
                           "eaArea": rec["eaArea"],
                           "type": rec["type"], "category": rec["category"],
                           "counties": rec["counties"], "rainfallZones": rec["rainfallZones"]},
        })
    centroids_obj = {"type": "FeatureCollection", "features": centroid_features}

    write_local("ea/floodareas/index.json", index_obj)
    write_local("ea/floodareas/centroids.geojson", centroids_obj)
    if r2:
        r2_put_json(r2, INDEX_KEY, index_obj)
        r2_put_json(r2, CENTROIDS_KEY, centroids_obj)
        print("  Uploaded index.json + centroids.geojson.")

    # 3. Combined simplified overview
    combined = {"type": "FeatureCollection",
                "generated_at": now.isoformat(),
                "features": combined_features}
    write_local("ea/floodareas/polygons_all.min.geojson", combined)
    if r2:
        r2_put_json(r2, COMBINED_KEY, combined)
        body = json.dumps(combined, separators=(",", ":")).encode("utf-8")
        print(f"  Uploaded combined overview ({len(combined_features)} features, "
              f"{len(body)/1_048_576:.1f} MB).")

    print("Flood-area reference build complete.")


def write_local(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
