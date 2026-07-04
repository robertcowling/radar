#!/usr/bin/env python3
"""
count_properties.py — Spatial join OS Open UPRN against EA/NRW flood area
polygons and write propertyCount back to R2.

The UPRN CSV can be downloaded free from:
    https://osdatahub.os.uk/downloads/open/OpenUPRN
    (requires free OS account; download "OSOPENUPRN_202XXXXX_CSV.zip", unzip,
     use the single CSV inside — typically named osopenuprn_YYYYMMDD.csv)

Usage:
    py count_properties.py --uprn path/to/osopenuprn.csv [--dry-run]

The script:
  1. Reads flood area polygons from R2 (EA + NRW, polygons_all.min.geojson)
  2. Streams UPRN CSV in 500k-row chunks to avoid loading 40M rows at once
  3. Spatial-joins each chunk against the polygons with a Shapely STRtree
  4. Writes propertyCount back to index.json, centroids.geojson, and
     polygons_all.min.geojson for both sources

A property is counted toward EVERY flood area polygon that contains it, not
just the first one matched — EA warning areas are routinely nested inside a
broader alert-area polygon (e.g. "River Don at Fishlake" sits inside "Lower
River Don catchment"), and attributing each property to only one area left
the more specific, more useful area at a misleading zero. This means area
counts no longer sum to a true national total (a property inside two
overlapping areas counts toward both) — that's an intentional trade-off in
favour of each area's own count being correct.

Dependencies:  shapely  (pip install shapely)
               boto3    (already installed)
"""

import argparse
import json
import os
import sys
import csv
from collections import defaultdict

try:
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
except ImportError:
    sys.exit("shapely is required: pip install shapely")

import boto3

# ── Load .env if present (tools/.env relative to this file) ───────────────────
def _load_env():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in [os.path.join(here, "tools", ".env"), os.path.join(here, ".env")]:
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
            break

_load_env()

sys.stdout.reconfigure(line_buffering=True)  # flush each print immediately when piped

# ── R2 config ─────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY  = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET      = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET      = os.environ.get("R2_BUCKET_NAME", "")

SOURCES = {
    "EA":  {
        "index":    "ea/floodareas/index.json",
        "centroids":"ea/floodareas/centroids.geojson",
        "combined": "ea/floodareas/polygons_all.min.geojson",
    },
    "NRW": {
        "index":    "nrw/floodareas/index.json",
        "centroids":"nrw/floodareas/centroids.geojson",
        "combined": "nrw/floodareas/polygons_all.min.geojson",
    },
}

CHUNK = 500_000


def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET,
        region_name="auto",
    )


def r2_get_json(r2, key):
    obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")


def local_read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def local_write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


def load_polygons(r2, combined_key, local_root=None):
    """Return list of (code, shapely_geom) from polygons_all.min.geojson."""
    if local_root:
        local_path = os.path.join(local_root, combined_key.replace("/", os.sep))
        print(f"  Loading polygons from local {local_path}...")
        fc = local_read_json(local_path)
    else:
        print(f"  Loading polygons from R2 {combined_key}...")
        fc = r2_get_json(r2, combined_key)
    polys = []
    for feat in fc.get("features", []):
        code = feat["properties"].get("code")
        geom = feat.get("geometry")
        if code and geom:
            try:
                polys.append((code, shape(geom)))
            except Exception:
                pass
    print(f"  {len(polys)} polygon features loaded.")
    return polys


def count_uprns(uprn_path, polys):
    """
    Stream UPRN CSV and count points-in-polygon per flood area code.
    Returns dict {code: count}.
    """
    geoms   = [g for _, g in polys]
    codes   = [c for c, _ in polys]
    tree    = STRtree(geoms)
    counts  = defaultdict(int)

    # Detect column names from header
    with open(uprn_path, newline="", encoding="utf-8") as f:
        sample = f.read(2048)
    dialect   = csv.Sniffer().sniff(sample)
    with open(uprn_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, dialect=dialect)
        headers = reader.fieldnames or []

    lat_col = next((h for h in headers if h.strip().upper() in ("LATITUDE", "LAT")), None)
    lon_col = next((h for h in headers if h.strip().upper() in ("LONGITUDE", "LON", "LONG")), None)
    if not lat_col or not lon_col:
        sys.exit(f"Cannot find LATITUDE/LONGITUDE columns in CSV. Found: {headers}")

    print(f"  UPRN columns: lat={lat_col!r}  lon={lon_col!r}")
    total = processed = 0

    with open(uprn_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, dialect=dialect)
        chunk_pts = []
        for row in reader:
            total += 1
            try:
                lat = float(row[lat_col])
                lon = float(row[lon_col])
            except (ValueError, KeyError):
                continue
            chunk_pts.append(Point(lon, lat))

            if len(chunk_pts) >= CHUNK:
                _join_chunk(chunk_pts, tree, geoms, codes, counts)
                processed += len(chunk_pts)
                chunk_pts = []
                print(f"    {processed:,} rows processed, {sum(counts.values()):,} hits so far...")

        if chunk_pts:
            _join_chunk(chunk_pts, tree, geoms, codes, counts)
            processed += len(chunk_pts)

    print(f"  Done: {total:,} UPRN rows, {processed:,} valid coords, {sum(counts.values()):,} area-hits within flood areas.")
    return dict(counts)


def _join_chunk(points, tree, geoms, codes, counts):
    for pt in points:
        candidates = tree.query(pt)
        for idx in candidates:
            if geoms[idx].contains(pt):
                # Count toward EVERY overlapping area — EA warning areas are
                # routinely nested inside a broader alert-area polygon, and
                # attributing a property to only the first match left the
                # more specific area at a misleading zero (see module docstring).
                counts[codes[idx]] += 1


def apply_counts(r2, keys, counts, dry_run, local_root=None):
    if local_root:
        index_path     = os.path.join(local_root, keys["index"].replace("/", os.sep))
        centroids_path = os.path.join(local_root, keys["centroids"].replace("/", os.sep))
        combined_path  = os.path.join(local_root, keys["combined"].replace("/", os.sep))
        index_obj      = local_read_json(index_path)
        centroids_obj  = local_read_json(centroids_path)
        combined_obj   = local_read_json(combined_path)
    else:
        index_obj     = r2_get_json(r2, keys["index"])
        centroids_obj = r2_get_json(r2, keys["centroids"])
        combined_obj  = r2_get_json(r2, keys["combined"])

    # index.json — list under "areas"
    hit = 0
    for area in index_obj.get("areas", []):
        c = counts.get(area["code"])
        if c is not None:
            area["propertyCount"] = c
            hit += 1
        else:
            area.setdefault("propertyCount", 0)
    print(f"  index.json:    {hit} / {len(index_obj.get('areas', []))} areas updated.")

    # centroids.geojson
    for feat in centroids_obj.get("features", []):
        code = feat["properties"].get("code")
        feat["properties"]["propertyCount"] = counts.get(code, 0)

    # polygons_all.min.geojson
    for feat in combined_obj.get("features", []):
        code = feat["properties"].get("code")
        feat["properties"]["propertyCount"] = counts.get(code, 0)

    if dry_run:
        print("  Dry run — not uploading.")
        return

    if local_root:
        local_write_json(index_path,     index_obj)
        local_write_json(centroids_path, centroids_obj)
        local_write_json(combined_path,  combined_obj)
        print(f"  Written locally: {index_path}, {centroids_path}, {combined_path}")

    r2_put_json(r2, keys["index"],     index_obj)
    r2_put_json(r2, keys["centroids"], centroids_obj)
    r2_put_json(r2, keys["combined"],  combined_obj)
    body_size = len(json.dumps(combined_obj, separators=(",", ":")).encode())
    print(f"  Uploaded index + centroids + combined ({body_size/1_048_576:.1f} MB).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uprn", required=True, help="Path to OS Open UPRN CSV file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", choices=["EA", "NRW", "both"], default="both")
    ap.add_argument("--local", action="store_true",
                    help="Read polygon/index files from local disk instead of R2 "
                         "(still uploads results to R2 unless --dry-run)")
    args = ap.parse_args()

    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET, R2_BUCKET]):
        sys.exit("R2 credentials not set in environment.")

    uprn_path = args.uprn
    if not os.path.isfile(uprn_path):
        sys.exit(f"UPRN file not found: {uprn_path}")

    local_root = os.path.dirname(os.path.abspath(__file__)) if args.local else None

    r2 = get_r2()
    sources = ["EA", "NRW"] if args.source == "both" else [args.source]

    for src in sources:
        keys = SOURCES[src]
        print(f"\n== {src} ==")
        polys = load_polygons(r2, keys["combined"], local_root=local_root)
        if not polys:
            print("  No polygons — skipping.")
            continue

        print(f"  Counting UPRNs within {len(polys)} {src} flood areas...")
        counts = count_uprns(uprn_path, polys)

        top = sorted(counts.items(), key=lambda x: -x[1])[:5]
        print(f"  Top 5 by count: {top}")

        print(f"  Applying counts to {'local + R2' if local_root else 'R2'} files...")
        apply_counts(r2, keys, counts, args.dry_run, local_root=local_root)

    print("\nDone.")


if __name__ == "__main__":
    main()
