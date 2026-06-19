#!/usr/bin/env python3
"""
Generate 900×506 dashboard composite WebP images (CartoDB basemap + radar/sat overlay).

Per-frame composites are uploaded as:
  radar/dashboard_radar_YYYYMMDDHHMM.webp   (last HISTORY_FRAMES frames)
  radar/dashboard_sat_YYYYMMDDHHMM.webp     (last HISTORY_FRAMES frames, where sat exists)

Static aliases for external consumers:
  radar/dashboard_radar_latest.webp
  radar/dashboard_sat_latest.webp

frames_parallel.json is updated with dashboard_radar_url / dashboard_sat_url per frame.
"""

import io
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timedelta

import boto3
from PIL import Image

# UK-centric crop — wide enough that the Web Mercator extent is naturally 16:9
# (-33 to +5 lon, 49.5 to 61.5 lat → merc aspect ratio = 1.778 = 16:9 exactly)
# The UK sits in the right ~40% of the frame; Atlantic fills the left.
UK_LON_MIN, UK_LON_MAX = -33.0, 5.0
UK_LAT_MIN, UK_LAT_MAX = 49.5, 61.5

OUT_W, OUT_H = 900, 506
ZOOM = 6
TILE_SIZE = 256
EARTH_RADIUS = 6378137.0

TILE_URL = "https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png"
# Filename encodes crop bounds so a changed crop invalidates the cache
BASEMAP_CACHE = "static/dashboard_basemap_z6_wide.png"

# How many recent frames to keep composites for (6h = 24 × 15-min frames)
HISTORY_FRAMES = 24

# Retention: delete per-frame composites older than this many hours
RETENTION_HOURS = 48

# Radar overlay extent (Web Mercator-projected PNG from process_parallel.py)
RADAR_LON_MIN, RADAR_LON_MAX = -17.9739, 16.1291
RADAR_LAT_MIN, RADAR_LAT_MAX = 43.7009, 62.9207

# Satellite overlay extent (EUMETSAT WMS extent from process_latest.py)
SAT_LON_MIN, SAT_LON_MAX = -40.4, 32.4
SAT_LAT_MIN, SAT_LAT_MAX = 37.75, 67.5

R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])


# ── Geometry ──────────────────────────────────────────────────────────────────

def ll_to_merc(lon, lat):
    lat_r = math.radians(lat)
    x = EARTH_RADIUS * math.radians(lon)
    y = EARTH_RADIUS * math.log(math.tan(math.pi / 4 + lat_r / 2))
    return x, y


def lon_to_tx(lon, z):
    return int((lon + 180.0) / 360.0 * (2 ** z))


def lat_to_ty(lat, z):
    lat_r = math.radians(lat)
    return int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * (2 ** z))


def merc_to_world_px(mx, my, z):
    world = (2 ** z) * TILE_SIZE
    px = (mx + math.pi * EARTH_RADIUS) / (2 * math.pi * EARTH_RADIUS) * world
    py = (math.pi * EARTH_RADIUS - my) / (2 * math.pi * EARTH_RADIUS) * world
    return px, py


# ── Basemap ───────────────────────────────────────────────────────────────────

def fetch_tile(z, x, y):
    url = TILE_URL.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (radar-dashboard-bot)'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return Image.open(io.BytesIO(resp.read())).convert("RGBA")


def build_basemap():
    tx_min = lon_to_tx(UK_LON_MIN, ZOOM)
    tx_max = lon_to_tx(UK_LON_MAX, ZOOM)
    ty_min = lat_to_ty(UK_LAT_MAX, ZOOM)
    ty_max = lat_to_ty(UK_LAT_MIN, ZOOM)

    nx = tx_max - tx_min + 1
    ny = ty_max - ty_min + 1
    print(f"Fetching {nx}×{ny} basemap tiles at zoom {ZOOM}…")

    stitched = Image.new("RGBA", (nx * TILE_SIZE, ny * TILE_SIZE))
    for iy, ty in enumerate(range(ty_min, ty_max + 1)):
        for ix, tx in enumerate(range(tx_min, tx_max + 1)):
            stitched.paste(fetch_tile(ZOOM, tx, ty), (ix * TILE_SIZE, iy * TILE_SIZE))

    uk_mx_min, uk_my_min = ll_to_merc(UK_LON_MIN, UK_LAT_MIN)
    uk_mx_max, uk_my_max = ll_to_merc(UK_LON_MAX, UK_LAT_MAX)
    ox = tx_min * TILE_SIZE
    oy = ty_min * TILE_SIZE
    left,  top    = merc_to_world_px(uk_mx_min, uk_my_max, ZOOM)
    right, bottom = merc_to_world_px(uk_mx_max, uk_my_min, ZOOM)
    box = (
        int(max(0, left  - ox)), int(max(0, top   - oy)),
        int(min(stitched.width,  right  - ox)),
        int(min(stitched.height, bottom - oy)),
    )
    return stitched.crop(box).resize((OUT_W, OUT_H), Image.LANCZOS)


def get_basemap():
    if os.path.exists(BASEMAP_CACHE):
        print("Using cached basemap.")
        return Image.open(BASEMAP_CACHE).convert("RGBA")
    os.makedirs(os.path.dirname(BASEMAP_CACHE), exist_ok=True)
    bm = build_basemap()
    bm.save(BASEMAP_CACHE, "PNG")
    print(f"Basemap cached: {BASEMAP_CACHE}")
    return bm


# ── Overlay compositing ───────────────────────────────────────────────────────

def crop_overlay_to_uk(img_rgba, ov_lon_min, ov_lon_max, ov_lat_min, ov_lat_max):
    ov_mx_min, ov_my_min = ll_to_merc(ov_lon_min, ov_lat_min)
    ov_mx_max, ov_my_max = ll_to_merc(ov_lon_max, ov_lat_max)
    uk_mx_min, uk_my_min = ll_to_merc(UK_LON_MIN, UK_LAT_MIN)
    uk_mx_max, uk_my_max = ll_to_merc(UK_LON_MAX, UK_LAT_MAX)

    ov_w, ov_h = img_rgba.size
    span_x = ov_mx_max - ov_mx_min
    span_y = ov_my_max - ov_my_min

    left   = (uk_mx_min - ov_mx_min) / span_x * ov_w
    right  = (uk_mx_max - ov_mx_min) / span_x * ov_w
    top    = (ov_my_max - uk_my_max) / span_y * ov_h
    bottom = (ov_my_max - uk_my_min) / span_y * ov_h

    box = (
        int(max(0, left)), int(max(0, top)),
        int(min(ov_w, right)), int(min(ov_h, bottom)),
    )
    return img_rgba.crop(box)


def download_image(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (radar-dashboard-bot)'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return Image.open(io.BytesIO(resp.read()))


def make_composite(basemap_rgba, overlay_url, ov_lon_min, ov_lon_max, ov_lat_min, ov_lat_max):
    overlay = download_image(overlay_url).convert("RGBA")
    cropped = crop_overlay_to_uk(overlay, ov_lon_min, ov_lon_max, ov_lat_min, ov_lat_max)
    resized = cropped.resize((OUT_W, OUT_H), Image.LANCZOS)
    result = basemap_rgba.copy()
    result.paste(resized, (0, 0), resized)
    return result.convert("RGB")


# ── R2 ────────────────────────────────────────────────────────────────────────

def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )


def upload_webp(r2, img, r2_key):
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85)
    r2.put_object(
        Bucket=R2_BUCKET, Key=r2_key,
        Body=buf.getvalue(),
        ContentType="image/webp",
        CacheControl="no-cache, max-age=0",
    )
    url = f"{R2_PUBLIC_URL}/{r2_key}"
    print(f"  Uploaded {r2_key} ({buf.tell()} bytes)")
    return url


def cleanup_old_composites(r2, keep_keys):
    """Delete timestamped composites outside the current window."""
    cutoff_str = (datetime.utcnow() - timedelta(hours=RETENTION_HOURS)).strftime('%Y%m%d%H%M')
    paginator = r2.get_paginator('list_objects_v2')
    deleted = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='radar_gh/dashboard_'):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key in keep_keys:
                continue
            m = re.search(r'_(\d{12})\.webp$', key)
            if m and m.group(1) < cutoff_str:
                r2.delete_object(Bucket=R2_BUCKET, Key=key)
                print(f"  Deleted old composite: {key}")
                deleted += 1
    if deleted:
        print(f"Cleanup: removed {deleted} old composites.")


def ts_from_url(url):
    """Extract 12-digit timestamp from a radar_parallel PNG URL."""
    m = re.search(r'/(\d{12})_', url)
    return m.group(1) if m else None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open("frames_parallel.json") as f:
        frames = json.load(f)
    if not frames:
        print("frames_parallel.json is empty.")
        return

    # Work on last HISTORY_FRAMES frames that have a radar URL
    radar_frames = [fr for fr in frames if fr.get("url")][-HISTORY_FRAMES:]
    if not radar_frames:
        print("No radar frames found.")
        return

    basemap = get_basemap()
    r2 = get_r2_client() if USE_R2 else None
    keep_keys = set()

    for fr in radar_frames:
        ts = ts_from_url(fr["url"])
        if not ts:
            continue

        # Radar composite — always regenerate so projection changes take effect immediately
        radar_key = f"radar_gh/dashboard_radar_{ts}.webp"
        keep_keys.add(radar_key)
        print(f"Generating radar composite {ts}…")
        try:
            comp = make_composite(basemap, fr["url"],
                                  RADAR_LON_MIN, RADAR_LON_MAX,
                                  RADAR_LAT_MIN, RADAR_LAT_MAX)
            if USE_R2:
                fr["dashboard_radar_url"] = upload_webp(r2, comp, radar_key)
            else:
                comp.save(f"dashboard_radar_{ts}.webp", "WEBP", quality=85)
                fr["dashboard_radar_url"] = f"dashboard_radar_{ts}.webp"
        except Exception as e:
            print(f"  Radar failed for {ts}: {e}")

        # Satellite composite (only where sat URL is available)
        sat_url = fr.get("sat_url_bw")
        if sat_url:
            sat_key = f"radar_gh/dashboard_sat_{ts}.webp"
            keep_keys.add(sat_key)
            print(f"Generating sat composite {ts}…")
            try:
                comp = make_composite(basemap, sat_url,
                                      SAT_LON_MIN, SAT_LON_MAX,
                                      SAT_LAT_MIN, SAT_LAT_MAX)
                if USE_R2:
                    fr["dashboard_sat_url"] = upload_webp(r2, comp, sat_key)
                else:
                    comp.save(f"dashboard_sat_{ts}.webp", "WEBP", quality=85)
                    fr["dashboard_sat_url"] = f"dashboard_sat_{ts}.webp"
            except Exception as e:
                print(f"  Sat failed for {ts}: {e}")

    # Upload static _latest aliases from the newest frame
    newest = radar_frames[-1]
    if USE_R2 and newest.get("dashboard_radar_url"):
        try:
            latest_comp = make_composite(basemap, newest["url"],
                                         RADAR_LON_MIN, RADAR_LON_MAX,
                                         RADAR_LAT_MIN, RADAR_LAT_MAX)
            upload_webp(r2, latest_comp, "radar_gh/dashboard_radar_latest.webp")
            keep_keys.add("radar_gh/dashboard_radar_latest.webp")
        except Exception as e:
            print(f"  _latest radar failed: {e}")

    if USE_R2 and newest.get("dashboard_sat_url"):
        try:
            sat_comp = make_composite(basemap, newest["sat_url_bw"],
                                      SAT_LON_MIN, SAT_LON_MAX,
                                      SAT_LAT_MIN, SAT_LAT_MAX)
            upload_webp(r2, sat_comp, "radar_gh/dashboard_sat_latest.webp")
            keep_keys.add("radar_gh/dashboard_sat_latest.webp")
        except Exception as e:
            print(f"  _latest sat failed: {e}")

    # Write updated manifest
    with open("frames_parallel.json", "w") as f:
        json.dump(frames, f, indent=2)
    print("frames_parallel.json updated.")

    if USE_R2:
        with open("frames_parallel.json", "rb") as fh:
            r2.put_object(
                Bucket=R2_BUCKET, Key="frames_parallel.json",
                Body=fh.read(),
                ContentType="application/json",
                CacheControl="no-cache, max-age=0",
            )
        print("frames_parallel.json re-uploaded to R2.")
        cleanup_old_composites(r2, keep_keys)


if __name__ == "__main__":
    main()
