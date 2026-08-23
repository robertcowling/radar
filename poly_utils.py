"""
poly_utils.py — Shared polygon-mask and area-average utilities.

Used by both make_accum_multi.py (radar) and fetch_ukv.py (UKV forecast).
All grid-specific dimensions are passed as parameters so the same code works
for different output grids.
"""
import hashlib
import io
import json
import os

import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer

# GeoJSON layers shared by both pipelines
GEOJSON_LAYERS = {
    "regions":    ("uk_regions.geojson",    "rgn19nm"),
    "counties":   ("uk-counties.geojson",   "name"),
    "catchments": ("uk_catchments.geojson", "HA_NAME"),
    "grid":       ("uk_grid_20km.geojson",  "name"),
    "summary":    ("rainfallsummarypolygons.geojson", "NAME"),
}


def build_merc_axes(width, height, lon_min, lon_max, lat_min, lat_max):
    """Return (transformer, xmin, xmax, ymin_m, ymax_m) for a Web Mercator pixel grid."""
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xmin, ymin_m = ll_to_merc.transform(lon_min, lat_min)
    xmax, ymax_m = ll_to_merc.transform(lon_max, lat_max)
    return ll_to_merc, xmin, xmax, ymin_m, ymax_m


def _ring_to_pixels(ring, ll_to_merc, xmin, xmax, ymin_m, ymax_m, width, height):
    """Convert [[lon, lat], ...] ring to [(col, row), ...] pixel coordinates."""
    pts = []
    for lon, lat in ring:
        mx, my = ll_to_merc.transform(lon, lat)
        col = (mx - xmin) / (xmax - xmin) * (width  - 1)
        row = (ymax_m - my) / (ymax_m - ymin_m) * (height - 1)
        pts.append((col, row))
    return pts


def geojson_fingerprint(path):
    """Cheap content fingerprint so callers can detect a changed boundary file."""
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def build_pixel_masks(geojson_path, name_key, width, height, ll_to_merc, xmin, xmax, ymin_m, ymax_m):
    """Rasterise every feature in a GeoJSON into pixel index arrays.

    Returns {name: (rows_uint16, cols_uint16)}.
    """
    with open(geojson_path) as f:
        gj = json.load(f)

    masks = {}
    for feat in gj.get("features", []):
        name = feat["properties"].get(name_key, "")
        if not name:
            continue
        geom = feat["geometry"]
        img  = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(img)

        gtype = geom["type"]
        if gtype == "Polygon":
            rings = geom["coordinates"]
        elif gtype == "MultiPolygon":
            rings = [r for poly in geom["coordinates"] for r in poly]
        else:
            continue

        for ring in rings:
            pts = _ring_to_pixels(ring, ll_to_merc, xmin, xmax, ymin_m, ymax_m, width, height)
            if len(pts) >= 3:
                draw.polygon(pts, fill=255)

        arr = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(height, width)
        rows, cols = np.where(arr > 0)
        if len(rows):
            masks[name] = (rows.astype(np.uint16), cols.astype(np.uint16))

    return masks


def masks_to_npz_bytes(masks, names):
    """Pack a masks dict into compressed NPZ bytes."""
    all_rows = np.concatenate([masks[n][0] for n in names]).astype(np.uint16)
    all_cols = np.concatenate([masks[n][1] for n in names]).astype(np.uint16)
    counts   = np.array([len(masks[n][0]) for n in names], dtype=np.int32)
    offsets  = np.concatenate([[0], np.cumsum(counts[:-1])]).astype(np.int32)
    buf = io.BytesIO()
    np.savez_compressed(buf, rows=all_rows, cols=all_cols, counts=counts, offsets=offsets)
    return buf.getvalue()


def npz_bytes_to_masks(npz_bytes, names):
    """Unpack NPZ bytes + names list back to {name: (rows, cols)}."""
    data = np.load(io.BytesIO(npz_bytes))
    rows_all, cols_all = data["rows"], data["cols"]
    counts, offsets    = data["counts"], data["offsets"]
    masks = {}
    for i, name in enumerate(names):
        s = int(offsets[i])
        e = s + int(counts[i])
        masks[name] = (rows_all[s:e], cols_all[s:e])
    return masks


def compute_poly_averages(arrays_by_key, all_layer_masks):
    """Compute mean mm value per polygon for each layer and each array key.

    Args:
        arrays_by_key:    {key: np.ndarray}  e.g. {'accum_1h': arr, 'accum_12h': arr}
                          OR {'1hr': arr, '12hr': arr} — any string keys work.
        all_layer_masks:  {layer_name: {poly_name: (rows, cols)}}

    Returns:
        {layer_name: {key: {poly_name: mean_mm}}}
    """
    result = {}
    for layer_name, masks in all_layer_masks.items():
        layer_result = {}
        for key, arr in arrays_by_key.items():
            period_data = {}
            for name, (rows, cols) in masks.items():
                if len(rows) == 0:
                    period_data[name] = None
                else:
                    period_data[name] = round(float(arr[rows, cols].mean()), 2)
            layer_result[key] = period_data
        result[layer_name] = layer_result
    return result


def load_or_build_masks(r2, bucket, prefix, width, height,
                        lon_min, lon_max, lat_min, lat_max, layers=None):
    """Load cached pixel masks for one output grid from R2, building them if absent.

    Grid-agnostic version of the per-pipeline loaders in make_accum_multi.py
    (`accum_masks/`) and fetch_ukv.py (`ukv_masks/`). Those two are left alone
    deliberately — both are long scheduled scripts, and rewriting them for a
    feature about a third page is regression risk for no gain.

    `prefix` names the cache, and each pipeline must own its prefix even when
    two grids happen to be identical. The cache holds an npz, a names list and
    a fingerprint of the source geojson; two callers sharing a prefix would
    silently invalidate each other's cache the moment one of them changed what
    goes into that fingerprint, and the symptom is masks quietly rebuilt on
    every run rather than anything visibly broken.

    Returns {layer_name: {poly_name: (rows, cols)}} for the layers whose
    geojson is present; a missing file is skipped, not an error.
    """
    ll_to_merc, xmin, xmax, ymin_m, ymax_m = build_merc_axes(
        width, height, lon_min, lon_max, lat_min, lat_max
    )

    all_masks = {}
    for layer_name, (geojson_path, name_key) in (layers or GEOJSON_LAYERS).items():
        if not os.path.exists(geojson_path):
            print(f"  [{prefix}/{layer_name}] {geojson_path} not found — skipping")
            continue

        npz_key   = f"{prefix}/{layer_name}.npz"
        names_key = f"{prefix}/{layer_name}_names.json"
        fp_key    = f"{prefix}/{layer_name}_fp.json"
        current_fp = geojson_fingerprint(geojson_path)

        try:
            cached_fp = json.loads(r2.get_object(Bucket=bucket, Key=fp_key)["Body"].read())
            if cached_fp != current_fp:
                raise ValueError("stale mask cache — source geojson changed")
            names = json.loads(r2.get_object(Bucket=bucket, Key=names_key)["Body"].read())
            npz_bytes = r2.get_object(Bucket=bucket, Key=npz_key)["Body"].read()
            all_masks[layer_name] = npz_bytes_to_masks(npz_bytes, names)
            print(f"  [{prefix}/{layer_name}] loaded from R2 ({len(names)} polygons)")
            continue
        except Exception:
            pass

        print(f"  [{prefix}/{layer_name}] building from {geojson_path}...")
        masks = build_pixel_masks(geojson_path, name_key, width, height,
                                  ll_to_merc, xmin, xmax, ymin_m, ymax_m)
        names = list(masks.keys())
        print(f"  [{prefix}/{layer_name}] {len(names)} polygons rasterised")

        r2.put_object(Bucket=bucket, Key=npz_key,
                      Body=masks_to_npz_bytes(masks, names),
                      ContentType="application/octet-stream")
        r2.put_object(Bucket=bucket, Key=names_key,
                      Body=json.dumps(names).encode(),
                      ContentType="application/json; charset=utf-8")
        r2.put_object(Bucket=bucket, Key=fp_key,
                      Body=json.dumps(current_fp).encode(),
                      ContentType="application/json; charset=utf-8")
        print(f"  [{prefix}/{layer_name}] cached to R2")
        all_masks[layer_name] = masks

    return all_masks
