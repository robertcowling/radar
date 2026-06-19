#!/usr/bin/env python3
"""Generate 900×506 dashboard composite WebP images (CartoDB basemap + radar/sat overlay)."""

import io
import json
import math
import os
import urllib.request

import boto3
from PIL import Image

# UK-centric crop
UK_LON_MIN, UK_LON_MAX = -11.5, 2.5
UK_LAT_MIN, UK_LAT_MAX = 49.5, 61.5

OUT_W, OUT_H = 900, 506
ZOOM = 6
TILE_SIZE = 256
EARTH_RADIUS = 6378137.0

TILE_URL = "https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png"
BASEMAP_CACHE = "static/dashboard_basemap_z6.png"

# Radar overlay extent (Web Mercator-projected PNG from process_parallel.py)
RADAR_LON_MIN, RADAR_LON_MAX = -17.9739, 16.1291
RADAR_LAT_MIN, RADAR_LAT_MAX = 43.7009, 62.9207

# Satellite overlay extent (from process_latest.py WMS request)
SAT_LON_MIN, SAT_LON_MAX = -40.4, 32.4
SAT_LAT_MIN, SAT_LAT_MAX = 37.75, 67.5

R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])


# ── Geometry helpers ──────────────────────────────────────────────────────────

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
    """Web Mercator meters → world pixel coords at zoom z (y increases downward)."""
    world = (2 ** z) * TILE_SIZE
    px = (mx + math.pi * EARTH_RADIUS) / (2 * math.pi * EARTH_RADIUS) * world
    py = (math.pi * EARTH_RADIUS - my) / (2 * math.pi * EARTH_RADIUS) * world
    return px, py


# ── Tile fetching & basemap ───────────────────────────────────────────────────

def fetch_tile(z, x, y):
    url = TILE_URL.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (radar-dashboard-bot)'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return Image.open(io.BytesIO(resp.read())).convert("RGBA")


def build_basemap():
    """Fetch CartoDB tiles for UK crop, stitch, crop to exact box, resize to OUT_W×OUT_H."""
    tx_min = lon_to_tx(UK_LON_MIN, ZOOM)
    tx_max = lon_to_tx(UK_LON_MAX, ZOOM)
    ty_min = lat_to_ty(UK_LAT_MAX, ZOOM)   # northernmost lat → smallest tile y
    ty_max = lat_to_ty(UK_LAT_MIN, ZOOM)   # southernmost lat → largest tile y

    nx = tx_max - tx_min + 1
    ny = ty_max - ty_min + 1
    print(f"Fetching {nx}×{ny} tiles at zoom {ZOOM}...")

    stitched = Image.new("RGBA", (nx * TILE_SIZE, ny * TILE_SIZE))
    for iy, ty in enumerate(range(ty_min, ty_max + 1)):
        for ix, tx in enumerate(range(tx_min, tx_max + 1)):
            stitched.paste(fetch_tile(ZOOM, tx, ty), (ix * TILE_SIZE, iy * TILE_SIZE))

    # Convert UK corners to world pixel space then subtract stitch origin
    uk_mx_min, uk_my_min = ll_to_merc(UK_LON_MIN, UK_LAT_MIN)
    uk_mx_max, uk_my_max = ll_to_merc(UK_LON_MAX, UK_LAT_MAX)

    ox = tx_min * TILE_SIZE
    oy = ty_min * TILE_SIZE

    left,  top    = merc_to_world_px(uk_mx_min, uk_my_max, ZOOM)  # NW corner
    right, bottom = merc_to_world_px(uk_mx_max, uk_my_min, ZOOM)  # SE corner

    box = (
        int(max(0, left  - ox)),
        int(max(0, top   - oy)),
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
    """Extract the UK sub-region from a Web-Mercator-projected overlay image."""
    ov_mx_min, ov_my_min = ll_to_merc(ov_lon_min, ov_lat_min)
    ov_mx_max, ov_my_max = ll_to_merc(ov_lon_max, ov_lat_max)
    uk_mx_min, uk_my_min = ll_to_merc(UK_LON_MIN, UK_LAT_MIN)
    uk_mx_max, uk_my_max = ll_to_merc(UK_LON_MAX, UK_LAT_MAX)

    ov_w, ov_h = img_rgba.size
    span_x = ov_mx_max - ov_mx_min
    span_y = ov_my_max - ov_my_min  # positive (max > min)

    # top-left = NW corner (max lat = max merc y)
    left   = (uk_mx_min - ov_mx_min) / span_x * ov_w
    right  = (uk_mx_max - ov_mx_min) / span_x * ov_w
    top    = (ov_my_max - uk_my_max) / span_y * ov_h
    bottom = (ov_my_max - uk_my_min) / span_y * ov_h

    box = (
        int(max(0, left)),
        int(max(0, top)),
        int(min(ov_w, right)),
        int(min(ov_h, bottom)),
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


# ── R2 upload ─────────────────────────────────────────────────────────────────

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
        Bucket=R2_BUCKET,
        Key=r2_key,
        Body=buf.getvalue(),
        ContentType="image/webp",
        CacheControl="no-cache, max-age=0",
    )
    url = f"{R2_PUBLIC_URL}/{r2_key}"
    print(f"Uploaded {r2_key} ({buf.tell()} bytes)")
    return url


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open("frames_parallel.json") as f:
        frames = json.load(f)
    if not frames:
        print("frames_parallel.json is empty, nothing to do.")
        return

    latest = frames[-1]
    radar_url = latest.get("url")
    sat_url   = latest.get("sat_url_bw")

    if not radar_url:
        print("No radar URL in latest frame, exiting.")
        return

    basemap = get_basemap()
    r2 = get_r2_client() if USE_R2 else None

    dashboard_radar_url = None
    dashboard_sat_url   = None

    print(f"Compositing radar: {radar_url}")
    try:
        comp = make_composite(basemap, radar_url, RADAR_LON_MIN, RADAR_LON_MAX, RADAR_LAT_MIN, RADAR_LAT_MAX)
        if USE_R2:
            dashboard_radar_url = upload_webp(r2, comp, "radar/dashboard_radar_latest.webp")
        else:
            comp.save("dashboard_radar_latest.webp", "WEBP", quality=85)
            dashboard_radar_url = "dashboard_radar_latest.webp"
        print("Radar composite done.")
    except Exception as e:
        print(f"Radar composite failed: {e}")

    if sat_url:
        print(f"Compositing satellite: {sat_url}")
        try:
            comp = make_composite(basemap, sat_url, SAT_LON_MIN, SAT_LON_MAX, SAT_LAT_MIN, SAT_LAT_MAX)
            if USE_R2:
                dashboard_sat_url = upload_webp(r2, comp, "radar/dashboard_sat_latest.webp")
            else:
                comp.save("dashboard_sat_latest.webp", "WEBP", quality=85)
                dashboard_sat_url = "dashboard_sat_latest.webp"
            print("Satellite composite done.")
        except Exception as e:
            print(f"Satellite composite failed: {e}")
    else:
        print("No sat URL in latest frame, skipping satellite composite.")

    # Stamp dashboard URLs onto the latest frame
    if dashboard_radar_url:
        frames[-1]["dashboard_radar_url"] = dashboard_radar_url
    if dashboard_sat_url:
        frames[-1]["dashboard_sat_url"] = dashboard_sat_url

    with open("frames_parallel.json", "w") as f:
        json.dump(frames, f, indent=2)
    print("frames_parallel.json updated with dashboard URLs.")

    # Re-upload updated frames_parallel.json to R2 so CDN is consistent
    if USE_R2:
        with open("frames_parallel.json", "rb") as fh:
            r2.put_object(
                Bucket=R2_BUCKET,
                Key="frames_parallel.json",
                Body=fh.read(),
                ContentType="application/json",
                CacheControl="no-cache, max-age=0",
            )
        print("frames_parallel.json re-uploaded to R2.")


if __name__ == "__main__":
    main()
