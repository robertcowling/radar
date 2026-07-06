import io
import json
import os
import sys
from datetime import datetime, timedelta

import boto3
import h5py
import numpy as np
from botocore import UNSIGNED
from botocore.client import Config
from PIL import Image, ImageDraw
from pyproj import Transformer

from poly_utils import (
    GEOJSON_LAYERS as _GEOJSON_LAYERS,
    build_merc_axes as _build_merc_axes_util,
    build_pixel_masks as _build_pixel_masks_util,
    masks_to_npz_bytes as _masks_to_npz_bytes_util,
    npz_bytes_to_masks as _npz_bytes_to_masks_util,
    compute_poly_averages as _compute_poly_averages_util,
    geojson_fingerprint as _geojson_fingerprint_util,
)

# ── Output domain (matches process_parallel.py / parallel feed viewer) ─────────
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX =  43.7009, 62.9207
WIDTH,   HEIGHT  =  1725,    2175

# ── Source ─────────────────────────────────────────────────────────────────────
BUCKET       = "met-office-radar-obs-data"
H5_DIR       = "data_h5"
FRAMES_MAX   = 480       # 5d × 96 frames/day
INTERVAL_HRS = 0.25      # 15 min → hours; multiply rain rate by this for mm/frame

# ── Geography layers for polygon-average overlays ──────────────────────────────
GEOJSON_LAYERS = _GEOJSON_LAYERS

# ── Periods ────────────────────────────────────────────────────────────────────
PERIODS = {
    "1hr":   4,
    "3hr":   12,
    "6hr":   24,
    "12hr":  48,
    "24hr":  96,
    "48hr":  192,
    "5d":    480,
}

# ── Colour schemes ─────────────────────────────────────────────────────────────
_STANDARD_COLORS = np.array([
    [0,   0,   0,   0],
    [224, 243, 255, 255],
    [129, 212, 250, 255],
    [3,   169, 244, 255],
    [1,    87, 155, 255],
    [46,  125,  50, 255],
    [251, 192,  45, 255],
    [239, 108,   0, 255],
    [198,  40,  40, 255],
    [106,  27, 154, 255],
], dtype=np.uint8)

SCHEMES = {
    "norm": {
        "bounds": np.array([0.5,  1,   2,   5,  10,  20,  40,  80,  160]),
        "colors": _STANDARD_COLORS,
    },
    "high": {
        "bounds": np.array([2,    5,  10,  25,  50, 100, 150, 200,  350]),
        "colors": _STANDARD_COLORS,
    },
    "met": {
        "bounds": np.array([0.03, 1, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180]),
        "colors": np.array([
            [  0,   0,   0,   0],  # transparent  < 0.03
            [ 58, 108, 255, 255],  # blue         0.03–1
            [  0, 255,   0, 255],  # bright green 1–5
            [255, 255, 149, 255],  # pale yellow  5–10
            [255, 213,  99, 255],  # sand         10–20
            [255, 150,  24, 255],  # orange       20–40
            [232,  97,   0, 255],  # dark orange  40–60
            [186,  32,   0, 255],  # red          60–80
            [204,  83, 125, 255],  # rose         80–100
            [219, 146, 220, 255],  # light purple 100–120
            [255,   2, 255, 255],  # magenta      120–140
            [255, 255, 255, 255],  # white        140–160
            [200, 200, 200, 255],  # light grey   160–180
            [191, 191,   0, 255],  # olive        > 180
        ], dtype=np.uint8),
    },
}

# ── R2 ─────────────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])


def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


# ── S3 key listing ─────────────────────────────────────────────────────────────
def list_s3_keys_for_date(s3, date):
    prefix = f"radar/{date.strftime('%Y/%m/%d')}/"
    keys, cont = [], None
    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": prefix}
        if cont:
            kwargs["ContinuationToken"] = cont
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith("radar_rainrate_composite_1km_UK.h5"):
                keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            cont = resp["NextContinuationToken"]
        else:
            break
    return keys


def collect_max_keys(s3):
    keys, date = [], datetime.utcnow()
    for _ in range(7):
        keys.extend(list_s3_keys_for_date(s3, date))
        date -= timedelta(days=1)
        if len(keys) >= FRAMES_MAX * 2:
            break
    return sorted(keys)[-FRAMES_MAX:]


# ── Coordinate mapping (built once, reused across all frames) ──────────────────
def get_mapping(h5_path):
    with h5py.File(h5_path, "r") as f:
        where = f["where"].attrs
        xsize, ysize = int(where["xsize"]), int(where["ysize"])
        xscale, yscale = float(where["xscale"]), float(where["yscale"])
        proj_str = where["projdef"]
        if isinstance(proj_str, bytes):
            proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where["UL_lon"]), float(where["UL_lat"])

    ll_to_merc    = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str,    always_xy=True)
    ll_to_radar   = Transformer.from_crs("EPSG:4326", proj_str,    always_xy=True)

    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MX, MY = np.meshgrid(merc_xs, merc_ys)

    Xr, Yr = merc_to_radar.transform(MX, MY)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)

    cols = np.round((Xr - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Yr) / yscale).astype(np.int32)
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    return rows, cols, valid


def read_rain_rate(h5_path, mapping):
    """Return float32 mm/hr array projected onto the output grid (0 for no-data)."""
    rows, cols, valid = mapping
    with h5py.File(h5_path, "r") as f:
        raw = f["dataset1/data1/data"][:]
        dwhat = f["dataset1/data1/what"].attrs
        gain, offset = float(dwhat["gain"]), float(dwhat["offset"])
        nodata, undetect = float(dwhat["nodata"]), float(dwhat["undetect"])

    out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]

    rate = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    good = valid & (out_raw != nodata) & (out_raw != undetect)
    rate[good] = (out_raw[good].astype(np.float32) * gain + offset).clip(min=0)
    return rate


# ── R2 NPZ / JSON helpers ──────────────────────────────────────────────────────
def npz_to_r2(r2, key, array):
    buf = io.BytesIO()
    np.savez_compressed(buf, data=array)
    buf.seek(0)
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=buf.read(),
                  ContentType="application/octet-stream")


def npz_from_r2(r2, key):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return np.load(io.BytesIO(obj["Body"].read()))["data"]
    except Exception:
        return None


def json_from_r2(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default


def json_to_r2(r2, key, data):
    r2.put_object(Bucket=R2_BUCKET, Key=key,
                  Body=json.dumps(data).encode(),
                  ContentType="application/json; charset=utf-8")


# ── Polygon mask helpers (delegated to poly_utils) ────────────────────────────
def _build_merc_axes():
    return _build_merc_axes_util(WIDTH, HEIGHT, LON_MIN, LON_MAX, LAT_MIN, LAT_MAX)


def load_or_build_masks(r2, layer_name, geojson_path, name_key):
    """Load cached pixel masks from R2, or build and upload them if missing."""
    npz_key   = f"accum_masks/{layer_name}.npz"
    names_key = f"accum_masks/{layer_name}_names.json"
    fp_key    = f"accum_masks/{layer_name}_fp.json"

    current_fp = _geojson_fingerprint_util(geojson_path)
    cached_fp  = json_from_r2(r2, fp_key)
    names_data = json_from_r2(r2, names_key)
    if names_data is not None and cached_fp == current_fp:
        try:
            obj       = r2.get_object(Bucket=R2_BUCKET, Key=npz_key)
            npz_bytes = obj["Body"].read()
            print(f"  [{layer_name}] loaded masks from R2 ({len(names_data)} polygons)")
            return _npz_bytes_to_masks_util(npz_bytes, names_data)
        except Exception:
            pass

    print(f"  [{layer_name}] building pixel masks from {geojson_path}...")
    ll_to_merc, xmin, xmax, ymin_m, ymax_m = _build_merc_axes()
    masks = _build_pixel_masks_util(geojson_path, name_key, WIDTH, HEIGHT,
                                    ll_to_merc, xmin, xmax, ymin_m, ymax_m)
    names = list(masks.keys())
    print(f"  [{layer_name}] {len(names)} polygons rasterised")

    npz_bytes = _masks_to_npz_bytes_util(masks, names)
    r2.put_object(Bucket=R2_BUCKET, Key=npz_key, Body=npz_bytes,
                  ContentType="application/octet-stream")
    json_to_r2(r2, names_key, names)
    json_to_r2(r2, fp_key, current_fp)
    print(f"  [{layer_name}] masks uploaded to R2")
    return masks


def compute_poly_averages(sums, masks):
    """Return {period: {name: mean_mm}} for all periods and polygons.

    Thin wrapper kept for backward compatibility; delegates to poly_utils.
    sums:  {period_str: np.ndarray}
    masks: {name: (rows, cols)}
    """
    # poly_utils.compute_poly_averages expects {layer: masks}; here we have a
    # single layer's masks dict, so wrap and unwrap accordingly.
    result_nested = _compute_poly_averages_util(sums, {"_": masks})
    return result_nested["_"]


def upload_poly_snapshot(r2, ts, poly_snap):
    json_to_r2(r2, f"accum_poly/{ts}.json", poly_snap)


# ── Frame NPZ cache ────────────────────────────────────────────────────────────
def list_frame_keys_in_r2(r2):
    """Return set of 12-char frame keys ('YYYYMMDDHHMM') present in accum_frames/."""
    keys = set()
    for page in r2.get_paginator("list_objects_v2").paginate(
            Bucket=R2_BUCKET, Prefix="accum_frames/"):
        for obj in page.get("Contents", []):
            name = os.path.basename(obj["Key"])
            if name.endswith(".npz") and len(name) == 16:
                keys.add(name[:12])
    return keys


def process_new_frames(s3, r2, s3_keys, existing_fks):
    """For each frame not yet in R2: download H5, compute mm/frame contribution, upload NPZ.
    Returns dict fk -> np.array so update_sums can use in-memory values without re-downloading."""
    os.makedirs(H5_DIR, exist_ok=True)
    new_data, downloaded_h5, mapping = {}, [], None

    for key in s3_keys:
        h5_name = os.path.basename(key)
        fk = h5_name[:12]
        if fk in existing_fks:
            continue

        h5_path = os.path.join(H5_DIR, h5_name)
        print(f"  {fk}", end=" ", flush=True)

        if not os.path.exists(h5_path):
            s3.download_file(BUCKET, key, h5_path)
            downloaded_h5.append(h5_path)
            print("dl", end=" ", flush=True)

        if mapping is None:
            mapping = get_mapping(h5_path)

        contribution = read_rain_rate(h5_path, mapping) * INTERVAL_HRS
        npz_to_r2(r2, f"accum_frames/{fk}.npz", contribution)
        new_data[fk] = contribution
        existing_fks.add(fk)
        print(f"max={contribution.max():.3f}mm")

    for path in downloaded_h5:
        try:
            os.remove(path)
        except OSError:
            pass
    if downloaded_h5:
        print(f"  Cleaned up {len(downloaded_h5)} downloaded H5 files")

    return new_data


# ── Running sum management ─────────────────────────────────────────────────────
def load_frame(r2, fk, new_data):
    """Return frame contribution array — in-memory first, then R2."""
    if fk in new_data:
        return new_data[fk]
    return npz_from_r2(r2, f"accum_frames/{fk}.npz")


def update_period_sum(r2, period, n_frames, all_frame_keys, new_data):
    """Load, incrementally update, and save the running sum for one period.
    Triggers a full rebuild from frame NPZs when the sum is missing or too stale."""
    target_keys = all_frame_keys[-n_frames:]
    target_set  = set(target_keys)

    current_index = json_from_r2(r2, f"accum_sum/{period}_index.json", default=[])
    current_set   = set(current_index)
    current_sum   = npz_from_r2(r2, f"accum_sum/{period}.npz")

    to_add    = [k for k in target_keys if k not in current_set]
    to_remove = [k for k in current_index if k not in target_set]

    # Rebuild when sum is absent or more than half the window has changed
    if current_sum is None or len(to_add) + len(to_remove) > n_frames // 2:
        print(f"  [{period}] rebuilding from {len(target_keys)} frames...")
        current_sum = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
        for fk in target_keys:
            arr = load_frame(r2, fk, new_data)
            if arr is not None:
                current_sum += arr
    else:
        print(f"  [{period}] +{len(to_add)} -{len(to_remove)} frames")
        for fk in to_remove:
            arr = load_frame(r2, fk, new_data)
            if arr is not None:
                current_sum -= arr
        for fk in to_add:
            arr = load_frame(r2, fk, new_data)
            if arr is not None:
                current_sum += arr

    current_sum = np.clip(current_sum, 0, None)
    npz_to_r2(r2, f"accum_sum/{period}.npz", current_sum)
    json_to_r2(r2, f"accum_sum/{period}_index.json", target_keys)
    return current_sum


# ── Rendering and upload ───────────────────────────────────────────────────────
def frame_label(fk):
    try:
        return datetime.strptime(fk, "%Y%m%d%H%M").strftime("%a %d %b %Y %H:%M UTC")
    except Exception:
        return fk


HIST_RETENTION = 192   # 48hr × 4 runs/hr


def render_and_upload(r2, sums, all_frame_keys):
    snap_ts   = all_frame_keys[-1]
    snap_time = frame_label(snap_ts)

    existing_meta = json_from_r2(r2, "accum_multi_meta.json", default={})
    snapshots = [s for s in existing_meta.get("snapshots", []) if s.get("ts") != snap_ts]

    snap_entry = {"ts": snap_ts, "time": snap_time}

    for period, n_frames in PERIODS.items():
        target_keys  = all_frame_keys[-n_frames:]
        # period_start: one frame interval (15 min) before the first frame label,
        # because each frame label is the END of its 15-min observation window.
        if target_keys:
            start_dt = datetime.strptime(target_keys[0], "%Y%m%d%H%M") - timedelta(minutes=15)
            period_start = start_dt.strftime("%a %d %b %Y %H:%M UTC")
        else:
            period_start = ""
        period_end = frame_label(target_keys[-1]) if target_keys else ""
        arr = sums[period]

        period_data = {"period_start": period_start, "period_end": period_end}
        for scheme_name, scheme in SCHEMES.items():
            indices = np.digitize(arr, scheme["bounds"])
            indices[arr == 0] = 0
            img = Image.fromarray(scheme["colors"][indices], "RGBA")

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            r2_key   = f"accum_{period}_{scheme_name}.png"
            hist_key = f"accum_hist/{snap_ts}_{period}_{scheme_name}.png"
            r2.put_object(Bucket=R2_BUCKET, Key=r2_key,   Body=png_bytes, ContentType="image/png")
            r2.put_object(Bucket=R2_BUCKET, Key=hist_key, Body=png_bytes, ContentType="image/png")
            period_data[scheme_name] = f"{R2_PUBLIC_URL}/{hist_key}"
            print(f"  {r2_key}  max={arr.max():.1f}mm")

        snap_entry[period] = period_data

    snapshots.append(snap_entry)
    snapshots = snapshots[-HIST_RETENTION:]

    meta = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "snapshots": snapshots,
    }
    with open("accum_multi_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    json_to_r2(r2, "accum_multi_meta.json", meta)
    print(f"Meta written ({len(snapshots)} snapshots)")
    return {s["ts"] for s in snapshots}


# ── Cleanup ────────────────────────────────────────────────────────────────────
def cleanup_old_hist(r2, retention_days=14):
    """Remove accum_hist/ PNGs and accum_poly/ JSONs older than retention_days."""
    cutoff_str = (datetime.utcnow() - timedelta(days=retention_days)).strftime("%Y%m%d%H%M")
    deleted = 0
    for prefix in ("accum_hist/", "accum_poly/"):
        for page in r2.get_paginator("list_objects_v2").paginate(
                Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                ts = os.path.basename(obj["Key"])[:12]
                if ts.isdigit() and ts < cutoff_str:
                    r2.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])
                    deleted += 1
    if deleted:
        print(f"  Deleted {deleted} expired objects from R2")


def cleanup_old_frames(r2, all_frame_keys):
    """Remove accum_frames/ NPZs for timestamps no longer in the 48hr window."""
    keep = set(all_frame_keys)
    deleted = 0
    for page in r2.get_paginator("list_objects_v2").paginate(
            Bucket=R2_BUCKET, Prefix="accum_frames/"):
        for obj in page.get("Contents", []):
            name = os.path.basename(obj["Key"])
            if name.endswith(".npz") and name[:12] not in keep:
                r2.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])
                deleted += 1
    if deleted:
        print(f"  Deleted {deleted} expired frame NPZs from R2")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2()

    print("Collecting S3 keys (5-day window)...")
    s3_keys = collect_max_keys(s3)
    if not s3_keys:
        print("No S3 keys found — aborting.")
        sys.exit(1)
    all_frame_keys = [os.path.basename(k)[:12] for k in s3_keys]
    print(f"  {len(s3_keys)} frames  {all_frame_keys[0]} → {all_frame_keys[-1]}")

    print("Listing existing frame NPZs in R2...")
    existing_fks = list_frame_keys_in_r2(r2)
    new_count = sum(1 for fk in all_frame_keys if fk not in existing_fks)
    print(f"  {len(existing_fks)} cached, {new_count} new")

    print("Processing new frames...")
    new_data = process_new_frames(s3, r2, s3_keys, existing_fks)
    if not new_data:
        print("  No new frames to process")

    print("Updating running sums...")
    sums = {}
    for period, n_frames in PERIODS.items():
        sums[period] = update_period_sum(r2, period, n_frames, all_frame_keys, new_data)

    print("Rendering and uploading images...")
    snap_ts_set = render_and_upload(r2, sums, all_frame_keys)

    print("Computing polygon averages...")
    snap_ts = all_frame_keys[-1]
    poly_snap = {"ts": snap_ts}
    for layer_name, (geojson_path, name_key) in GEOJSON_LAYERS.items():
        if not os.path.exists(geojson_path):
            print(f"  [{layer_name}] {geojson_path} not found — skipping")
            continue
        masks = load_or_build_masks(r2, layer_name, geojson_path, name_key)
        poly_snap[layer_name] = compute_poly_averages(sums, masks)
    upload_poly_snapshot(r2, snap_ts, poly_snap)
    print(f"  Polygon snapshot uploaded: accum_poly/{snap_ts}.json")

    print("Cleaning up...")
    cleanup_old_frames(r2, all_frame_keys)
    cleanup_old_hist(r2)

    print("Done.")


if __name__ == "__main__":
    main()
