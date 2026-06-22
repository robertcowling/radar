#!/usr/bin/env python3
"""
make_accum_hourly.py

Generates 24 hourly 1-hour rainfall accumulation composite WebP images
covering the last 24 complete clock hours. Each image uses the Met Office
colour scheme and includes UK/Euro boundary overlays, a colour key, and a
datetime stamp. Images and manifest are uploaded to Cloudflare R2.

R2 outputs:
  composites/accum_hourly_{YYYYMMDDHHMM}.webp   (one per hour, 24 total)
  composites/frames_accum_hourly.json            (manifest)
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
from PIL import Image, ImageDraw, ImageFont
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
MET_S3_BUCKET = "met-office-radar-obs-data"
H5_DIR        = "data_h5"
FRAMES_PER_HR = 4      # 4 × 15-min frames = 1 clock hour
RETENTION_HRS = 48

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

LEGEND_LABELS = [
    "0.03–1 mm",
    "1–5 mm",
    "5–10 mm",
    "10–20 mm",
    "20–40 mm",
    "40–60 mm",
    "60–80 mm",
    "80–100 mm",
    "100–120 mm",
    "120–140 mm",
    "140–160 mm",
    "160–180 mm",
    "> 180 mm",
]

# ── Fonts ──────────────────────────────────────────────────────────────────────
try:
    _FONT      = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    _FONT_BOLD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
except Exception:
    _FONT = _FONT_BOLD = ImageFont.load_default()


# ── R2 helpers ─────────────────────────────────────────────────────────────────

def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
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


def collect_keys(s3, hours_back=27):
    """Return sorted H5 keys covering at least hours_back hours."""
    keys, date = [], datetime.utcnow()
    for _ in range(3):
        keys.extend(list_s3_keys_for_date(s3, date))
        date -= timedelta(days=1)
    return sorted(keys)


def group_by_clock_hour(keys):
    """Return {YYYYMMDDHHMM: [keys]} where YYYYMMDDHHMM is the start of the clock hour."""
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


def draw_legend(img_rgba):
    """Draw colour key in bottom-left corner; returns the modified image."""
    pad       = 10
    swatch_w  = 18
    swatch_h  = 15
    row_gap   = 2
    text_pad  = 5
    row_h     = swatch_h + row_gap
    n         = len(LEGEND_LABELS)
    box_w     = pad + swatch_w + text_pad + 88 + pad
    box_h     = pad + n * row_h + pad

    x0 = pad
    y0 = OUT_H - box_h - pad

    overlay = Image.new("RGBA", img_rgba.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    draw.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(20, 20, 20, 190))

    for i, label in enumerate(LEGEND_LABELS):
        color = tuple(MET_COLORS[i + 1])
        sx = x0 + pad
        sy = y0 + pad + i * row_h
        draw.rectangle([sx, sy, sx + swatch_w - 1, sy + swatch_h - 1], fill=color)
        draw.text((sx + swatch_w + text_pad, sy + 1), label,
                  fill=(255, 255, 255, 255), font=_FONT)

    return Image.alpha_composite(img_rgba, overlay)


def draw_timestamp(img_rgba, hour_ts):
    """Draw datetime stamp in top-right corner; returns the modified image."""
    try:
        dt = datetime.strptime(hour_ts, "%Y%m%d%H%M")
        end_dt = dt + timedelta(hours=1)
        line1 = f"1hr accumulation"
        line2 = f"{dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')} UTC"
        line3 = dt.strftime("%a %d %b %Y")
    except Exception:
        line1, line2, line3 = "1hr accumulation", hour_ts, ""

    pad     = 8
    line_h  = 16
    lines   = [line1, line2, line3]
    box_h   = pad + len(lines) * line_h + pad
    box_w   = 170

    x0 = OUT_W - box_w - pad
    y0 = pad

    overlay = Image.new("RGBA", img_rgba.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    draw.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(20, 20, 20, 190))
    for i, line in enumerate(lines):
        font = _FONT_BOLD if i == 0 else _FONT
        draw.text((x0 + pad, y0 + pad + i * line_h), line,
                  fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(img_rgba, overlay)


def make_hourly_composite(basemap_rgba, accum_mm, hour_ts):
    """Composite basemap + accumulation overlay + boundaries + legend + timestamp."""
    overlay = render_accum_overlay(accum_mm)

    comp = basemap_rgba.copy()
    comp.alpha_composite(overlay)

    rgb = comp.convert("RGB")
    draw_boundary_layers(rgb)

    rgba = rgb.convert("RGBA")
    rgba = draw_legend(rgba)
    rgba = draw_timestamp(rgba, hour_ts)
    return rgba


# ── R2 cleanup ─────────────────────────────────────────────────────────────────

def cleanup_r2(r2, keep_keys):
    cutoff = (datetime.utcnow() - timedelta(hours=RETENTION_HRS)).strftime("%Y%m%d%H%M")
    deleted = 0
    for page in r2.get_paginator("list_objects_v2").paginate(
            Bucket=R2_BUCKET, Prefix="composites/accum_hourly_"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key in keep_keys:
                continue
            ts = os.path.basename(key).replace("accum_hourly_", "").replace(".webp", "")
            if ts.isdigit() and ts < cutoff:
                r2.delete_object(Bucket=R2_BUCKET, Key=key)
                deleted += 1
    if deleted:
        print(f"  Deleted {deleted} expired R2 composites.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    r2 = get_r2() if USE_R2 else None
    if r2:
        print("R2 available — images will be uploaded.")
    else:
        print("R2 not configured — local only.")

    ensure_geo_files()
    print("Loading basemap...")
    basemap = get_basemap()

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))

    print("Collecting S3 keys...")
    all_keys = collect_keys(s3)
    if not all_keys:
        print("No H5 keys found — aborting.")
        return

    buckets = group_by_clock_hour(all_keys)

    # Take the last 24 complete hourly buckets (exactly 4 frames each)
    complete = {ts: sorted(ks) for ts, ks in buckets.items() if len(ks) == FRAMES_PER_HR}
    hour_slots = sorted(complete.keys())[-24:]

    if not hour_slots:
        print("No complete hourly buckets found — aborting.")
        return

    print(f"Processing {len(hour_slots)} hours: {hour_slots[0]} → {hour_slots[-1]}")

    # Accumulate all needed frames in one pass to minimise downloads
    needed_keys = {key for ts in hour_slots for key in complete[ts]}
    os.makedirs(H5_DIR, exist_ok=True)
    mapping   = None
    frame_mm  = {}   # key → mm/frame array

    print(f"Downloading and reading {len(needed_keys)} H5 frames...")
    downloaded = []
    for key in sorted(needed_keys):
        h5_name = os.path.basename(key)
        h5_path = os.path.join(H5_DIR, h5_name)
        if not os.path.exists(h5_path):
            s3.download_file(MET_S3_BUCKET, key, h5_path)
            downloaded.append(h5_path)
            print(f"  dl  {h5_name[:12]}")
        else:
            print(f"  hit {h5_name[:12]}")
        if mapping is None:
            mapping = get_h5_mapping(h5_path)
        try:
            rate = read_rain_rate(h5_path, mapping)
            frame_mm[key] = rate * 0.25   # mm/hr × 0.25 hr = mm per 15-min frame
        except Exception as e:
            print(f"  Error reading {h5_name}: {e}")

    # Delete downloaded H5s to save disk space
    for path in downloaded:
        try:
            os.remove(path)
        except OSError:
            pass
    if downloaded:
        print(f"  Cleaned up {len(downloaded)} H5 files.")

    # Render one composite per hour
    manifest    = []
    keep_r2_keys = set()

    for hour_ts in hour_slots:
        accum = np.zeros((OUT_H, OUT_W), dtype=np.float32)
        for key in complete[hour_ts]:
            if key in frame_mm:
                accum += frame_mm[key]

        print(f"  {hour_ts}: max={accum.max():.1f} mm")
        comp = make_hourly_composite(basemap, accum, hour_ts)

        r2_key = f"composites/accum_hourly_{hour_ts}.webp"
        keep_r2_keys.add(r2_key)

        buf = io.BytesIO()
        comp.convert("RGB").save(buf, "WEBP", quality=90)
        if r2:
            r2.put_object(
                Bucket=R2_BUCKET, Key=r2_key,
                Body=buf.getvalue(),
                ContentType="image/webp",
                CacheControl="public, max-age=86400",
            )

        dt = datetime.strptime(hour_ts, "%Y%m%d%H%M")
        manifest.append({
            "time": dt.strftime("%Y-%m-%dT%H:%M:00Z"),
            "url":  f"{R2_PUBLIC_URL}/{r2_key}",
        })

    # Upload manifest
    manifest_key = "composites/frames_accum_hourly.json"
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    if r2:
        r2.put_object(
            Bucket=R2_BUCKET, Key=manifest_key,
            Body=manifest_bytes,
            ContentType="application/json; charset=utf-8",
            CacheControl="no-cache, max-age=0",
        )
    with open("frames_accum_hourly.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {len(manifest)} frames")

    # Prune old R2 objects
    if r2:
        cleanup_r2(r2, keep_r2_keys)

    print("Done.")


if __name__ == "__main__":
    main()
