#!/usr/bin/env python3
"""
make_accum_hourly.py

Generates two sets of rainfall accumulation composites from Met Office H5 radar:

  Hourly (24 frames):
    composites/accum_hourly_{YYYYMMDDHHMM}.webp  — last 24 complete clock hours
    composites/frames_accum_hourly.json

  Daily (5 frames):
    composites/accum_daily_{YYYYMMDD}.webp  — today-so-far + 4 previous full days
    composites/frames_accum_daily.json

Both use the Met Office colour scheme. No legend or timestamp is baked into the
images; the consuming UI renders those.
"""

import io
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

import boto3
import h5py
import numpy as np
from botocore import UNSIGNED
from botocore.client import Config
from PIL import Image
from pyproj import Transformer

from make_dashboard_composites import (
    draw_boundary_layers,
    ensure_geo_files,
    get_basemap,
    UK_LON_MIN, UK_LON_MAX, UK_LAT_MIN, UK_LAT_MAX,
    OUT_W, OUT_H,
)

# ── R2 ─────────────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

# ── Source ─────────────────────────────────────────────────────────────────────
MET_S3_BUCKET      = "met-office-radar-obs-data"
H5_DIR             = "data_h5"
FRAMES_PER_HR      = 4    # 4 × 15-min frames = 1 clock hour
RETENTION_HRS      = 48   # hourly images kept for 48 h
DAILY_FRAMES       = 5    # today-so-far + 4 previous complete days
RETENTION_DAILY_DAYS = 7

# ── Met Office colour scheme ───────────────────────────────────────────────────
MET_BOUNDS = np.array([0.03, 1, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180])
MET_COLORS = np.array([
    [  0,   0,   0,   0],   # transparent  < 0.03 mm
    [ 58, 108, 255, 255],   # blue         0.03–1 mm
    [  0, 255,   0, 255],   # bright green 1–5 mm
    [255, 255, 149, 255],   # pale yellow  5–10 mm
    [255, 213,  99, 255],   # sand         10–20 mm
    [255, 150,  24, 255],   # orange       20–40 mm
    [232,  97,   0, 255],   # dark orange  40–60 mm
    [186,  32,   0, 255],   # red          60–80 mm
    [204,  83, 125, 255],   # rose         80–100 mm
    [219, 146, 220, 255],   # light purple 100–120 mm
    [255,   2, 255, 255],   # magenta      120–140 mm
    [255, 255, 255, 255],   # white        140–160 mm
    [200, 200, 200, 255],   # light grey   160–180 mm
    [191, 191,   0, 255],   # olive        > 180 mm
], dtype=np.uint8)


# ── R2 helpers ─────────────────────────────────────────────────────────────────

def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def r2_upload(r2, buf_bytes, r2_key, force=False):
    if not force:
        try:
            r2.head_object(Bucket=R2_BUCKET, Key=r2_key)
            return  # already exists and stable
        except Exception:
            pass
    r2.put_object(
        Bucket=R2_BUCKET, Key=r2_key,
        Body=buf_bytes,
        ContentType="image/webp",
        CacheControl="public, max-age=86400",
    )


def r2_put_json(r2, data, r2_key):
    body = json.dumps(data, separators=(",", ":")).encode()
    r2.put_object(
        Bucket=R2_BUCKET, Key=r2_key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache, max-age=0",
    )


# ── Met Office S3 listing ──────────────────────────────────────────────────────

def list_s3_keys_for_date(s3, date):
    prefix = f"radar/{date.strftime('%Y/%m/%d')}/"
    keys, cont = [], None
    while True:
        kwargs = {"Bucket": MET_S3_BUCKET, "Prefix": prefix}
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


def collect_keys(s3, days_back=6):
    """Return sorted H5 keys covering days_back calendar days."""
    keys, date = [], datetime.utcnow()
    for _ in range(days_back):
        keys.extend(list_s3_keys_for_date(s3, date))
        date -= timedelta(days=1)
    return sorted(keys)


def group_by_clock_hour(keys):
    """Return {YYYYMMDDHHMM: [keys]} keyed by clock-hour start."""
    buckets = defaultdict(list)
    for key in keys:
        base = os.path.basename(key)
        try:
            dt = datetime.strptime(base[:12], "%Y%m%d%H%M")
            bucket_ts = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y%m%d%H%M")
            buckets[bucket_ts].append(key)
        except Exception:
            pass
    return buckets


def group_by_date(keys):
    """Return {YYYYMMDD: [keys]} keyed by UTC calendar date."""
    buckets = defaultdict(list)
    for key in keys:
        base = os.path.basename(key)
        try:
            dt = datetime.strptime(base[:12], "%Y%m%d%H%M")
            buckets[dt.strftime("%Y%m%d")].append(key)
        except Exception:
            pass
    return buckets


# ── Coordinate mapping (H5 radar grid → output pixel grid) ────────────────────

_mapping_cache = None

def get_h5_mapping(h5_path):
    global _mapping_cache
    if _mapping_cache is not None:
        return _mapping_cache

    with h5py.File(h5_path, "r") as f:
        where = f["where"].attrs
        xsize, ysize = int(where["xsize"]), int(where["ysize"])
        xscale, yscale = float(where["xscale"]), float(where["yscale"])
        proj_str = where["projdef"]
        if isinstance(proj_str, bytes):
            proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where["UL_lon"]), float(where["UL_lat"])

    ll_to_merc_t    = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar_t = Transformer.from_crs("EPSG:3857", proj_str,    always_xy=True)
    ll_to_radar_t   = Transformer.from_crs("EPSG:4326", proj_str,    always_xy=True)

    merc_xmin, _ = ll_to_merc_t.transform(UK_LON_MIN, UK_LAT_MIN)
    merc_xmax, _ = ll_to_merc_t.transform(UK_LON_MAX, UK_LAT_MIN)
    _, merc_ymin  = ll_to_merc_t.transform(UK_LON_MIN, UK_LAT_MIN)
    _, merc_ymax  = ll_to_merc_t.transform(UK_LON_MIN, UK_LAT_MAX)

    merc_xs = np.linspace(merc_xmin, merc_xmax, OUT_W)
    merc_ys = np.linspace(merc_ymax, merc_ymin, OUT_H)
    MX, MY  = np.meshgrid(merc_xs, merc_ys)

    Xr, Yr  = merc_to_radar_t.transform(MX, MY)
    ul_x, ul_y = ll_to_radar_t.transform(ul_lon, ul_lat)

    cols = np.round((Xr - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Yr) / yscale).astype(np.int32)
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)

    _mapping_cache = (rows, cols, valid)
    return _mapping_cache


def read_rain_rate(h5_path, mapping):
    rows, cols, valid = mapping
    with h5py.File(h5_path, "r") as f:
        raw = f["dataset1/data1/data"][:]
        dwhat = f["dataset1/data1/what"].attrs
        gain, offset = float(dwhat["gain"]), float(dwhat["offset"])
        nodata, undetect = float(dwhat["nodata"]), float(dwhat["undetect"])

    out_raw = np.full((OUT_H, OUT_W), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]

    rate = np.zeros((OUT_H, OUT_W), dtype=np.float32)
    good = valid & (out_raw != nodata) & (out_raw != undetect)
    rate[good] = (out_raw[good].astype(np.float32) * gain + offset).clip(min=0)
    return rate


# ── Rendering ──────────────────────────────────────────────────────────────────

def render_accum_overlay(accum_mm):
    """Return RGBA image of accumulation in Met Office colour scheme."""
    indices = np.digitize(accum_mm, MET_BOUNDS)
    indices[accum_mm == 0] = 0
    return Image.fromarray(MET_COLORS[indices], "RGBA")


def make_composite(basemap_rgba, accum_mm):
    """Basemap + accumulation overlay + boundary lines. Returns RGB."""
    overlay = render_accum_overlay(accum_mm)
    comp = basemap_rgba.copy()
    comp.alpha_composite(overlay)
    rgb = comp.convert("RGB")
    draw_boundary_layers(rgb)
    return rgb


def encode_webp(img_rgb):
    buf = io.BytesIO()
    img_rgb.save(buf, "WEBP", quality=90)
    return buf.getvalue()


# ── R2 cleanup ─────────────────────────────────────────────────────────────────

def cleanup_r2(r2, keep_keys):
    hour_cutoff  = (datetime.utcnow() - timedelta(hours=RETENTION_HRS)).strftime("%Y%m%d%H%M")
    daily_cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAILY_DAYS)).strftime("%Y%m%d")
    deleted = 0
    for prefix, is_daily in [("composites/accum_hourly_", False),
                              ("composites/accum_daily_",  True)]:
        for page in r2.get_paginator("list_objects_v2").paginate(
                Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key in keep_keys:
                    continue
                stem = os.path.basename(key).split(".")[0].split("_")[-1]
                cutoff = daily_cutoff if is_daily else hour_cutoff
                if stem.isdigit() and stem < cutoff:
                    r2.delete_object(Bucket=R2_BUCKET, Key=key)
                    deleted += 1
    if deleted:
        print(f"  Deleted {deleted} expired R2 composites.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    r2 = get_r2() if USE_R2 else None
    print("R2 available." if r2 else "R2 not configured — local only.")

    ensure_geo_files()
    print("Loading basemap...")
    basemap = get_basemap()

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))

    print("Collecting S3 keys...")
    all_keys = collect_keys(s3)
    if not all_keys:
        print("No H5 keys found — aborting.")
        return

    # ── Determine which frames are needed ──────────────────────────────────────
    now       = datetime.utcnow()
    now_ts    = now.strftime("%Y%m%d%H%M")

    # Build a timestamp lookup for all keys (used for rolling-window selection)
    all_key_times = {}
    for key in all_keys:
        base = os.path.basename(key)
        try:
            all_key_times[key] = datetime.strptime(base[:12], "%Y%m%d%H%M")
        except Exception:
            pass

    # Hourly output timestamps: last 24 complete clock hours (all 4 frames present).
    # Each frame shows the rolling 24hr accumulation *ending* at that hour.
    hour_buckets = group_by_clock_hour(all_keys)
    complete_hours = {ts: sorted(ks) for ts, ks in hour_buckets.items()
                      if len(ks) == FRAMES_PER_HR}
    hour_slots = sorted(complete_hours.keys())[-24:]

    # Daily: today-so-far + 4 previous full calendar days
    date_buckets = group_by_date(all_keys)
    today = now.date()
    daily_dates = [(today - timedelta(days=i)).strftime("%Y%m%d")
                   for i in range(DAILY_FRAMES)]

    # For rolling 24hr: each output slot needs all frames in the 24hr window ending there
    needed_hourly = set()
    for ts in hour_slots:
        end_dt   = datetime.strptime(ts, "%Y%m%d%H%M")
        start_dt = end_dt - timedelta(hours=24)
        for key, dt in all_key_times.items():
            if start_dt <= dt < end_dt:
                needed_hourly.add(key)

    needed_daily  = {key for d in daily_dates for key in date_buckets.get(d, [])}
    needed_keys   = needed_hourly | needed_daily

    if not needed_keys:
        print("No frames needed — aborting.")
        return

    # ── Download and read all frames in one pass ───────────────────────────────
    os.makedirs(H5_DIR, exist_ok=True)
    mapping    = None
    frame_mm   = {}
    downloaded = []

    print(f"Downloading/reading {len(needed_keys)} H5 frames...")
    for key in sorted(needed_keys):
        h5_name = os.path.basename(key)
        h5_path = os.path.join(H5_DIR, h5_name)
        if not os.path.exists(h5_path):
            s3.download_file(MET_S3_BUCKET, key, h5_path)
            downloaded.append(h5_path)
        if mapping is None:
            mapping = get_h5_mapping(h5_path)
        try:
            rate = read_rain_rate(h5_path, mapping)
            frame_mm[key] = rate * 0.25   # mm/hr × 0.25 hr = mm per 15-min frame
        except Exception as e:
            print(f"  Error reading {h5_name}: {e}")

    for path in downloaded:
        try:
            os.remove(path)
        except OSError:
            pass
    if downloaded:
        print(f"  Cleaned up {len(downloaded)} H5 files.")

    keep_r2_keys = set()

    # ── Hourly composites (rolling 24hr accumulation ending at each slot) ────────
    hourly_manifest = []
    if hour_slots:
        print(f"Rendering {len(hour_slots)} rolling-24hr frames: {hour_slots[0]} → {hour_slots[-1]}")
    for hour_ts in hour_slots:
        end_dt   = datetime.strptime(hour_ts, "%Y%m%d%H%M")
        start_dt = end_dt - timedelta(hours=24)
        accum = np.zeros((OUT_H, OUT_W), dtype=np.float32)
        n_used = 0
        for key, dt in all_key_times.items():
            if start_dt <= dt < end_dt and key in frame_mm:
                accum += frame_mm[key]
                n_used += 1
        print(f"  {hour_ts}: {n_used}/96 frames, max={accum.max():.1f} mm")

        r2_key = f"composites/accum_hourly_{hour_ts}.webp"
        keep_r2_keys.add(r2_key)
        webp = encode_webp(make_composite(basemap, accum))
        if r2:
            # Slots within the last 3h may still be updating as radar arrives;
            # older slots are stable so skip the upload if already in R2.
            slot_dt = datetime.strptime(hour_ts, "%Y%m%d%H%M")
            recent = (datetime.utcnow() - slot_dt).total_seconds() < 3 * 3600
            r2_upload(r2, webp, r2_key, force=recent)

        dt = datetime.strptime(hour_ts, "%Y%m%d%H%M")
        hourly_manifest.append({
            "time": dt.strftime("%Y-%m-%dT%H:%M:00Z"),
            "url":  f"{R2_PUBLIC_URL}/{r2_key}",
        })

    manifest_key = "composites/frames_accum_hourly.json"
    if r2:
        r2_put_json(r2, hourly_manifest, manifest_key)
    with open("frames_accum_hourly.json", "w") as f:
        json.dump(hourly_manifest, f, indent=2)
    print(f"Hourly manifest: {len(hourly_manifest)} frames")

    # ── Daily composites ───────────────────────────────────────────────────────
    daily_manifest = []
    print(f"Rendering {len(daily_dates)} daily frames...")
    for date_str in daily_dates:
        date_keys = sorted(date_buckets.get(date_str, []))
        if not date_keys:
            print(f"  {date_str}: no data, skipping")
            continue
        # For today restrict to frames up to now
        if date_str == daily_dates[0]:
            date_keys = [k for k in date_keys if os.path.basename(k)[:12] <= now_ts]

        accum = np.zeros((OUT_H, OUT_W), dtype=np.float32)
        for key in date_keys:
            if key in frame_mm:
                accum += frame_mm[key]

        n_frames = sum(1 for k in date_keys if k in frame_mm)
        print(f"  {date_str}: {n_frames} frames, max={accum.max():.1f} mm")

        r2_key = f"composites/accum_daily_{date_str}.webp"
        keep_r2_keys.add(r2_key)
        webp = encode_webp(make_composite(basemap, accum))
        if r2:
            # Today's daily composite is always partial/updating; prior days stable.
            is_today = (date_str == daily_dates[0])
            r2_upload(r2, webp, r2_key, force=is_today)

        daily_manifest.append({
            "date": date_str,
            "url":  f"{R2_PUBLIC_URL}/{r2_key}",
        })

    daily_manifest_key = "composites/frames_accum_daily.json"
    if r2:
        r2_put_json(r2, daily_manifest, daily_manifest_key)
    with open("frames_accum_daily.json", "w") as f:
        json.dump(daily_manifest, f, indent=2)
    print(f"Daily manifest: {len(daily_manifest)} frames")

    # ── Prune old R2 composites ────────────────────────────────────────────────
    if r2:
        cleanup_r2(r2, keep_r2_keys)

    print("Done.")


if __name__ == "__main__":
    main()
