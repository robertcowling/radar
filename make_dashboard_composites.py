#!/usr/bin/env python3
"""
Generate 900×506 dashboard composite WebP images (CartoDB basemap + radar/sat overlay).

Per-frame composites are uploaded as:
  radar_parallel/dashboard_radar_YYYYMMDDHHMM.webp  (last HISTORY_FRAMES frames)
  sat_gh/dashboard_sat_YYYYMMDDHHMM.webp            (last HISTORY_FRAMES frames)

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
from PIL import Image, ImageDraw

# Tight UK-centred crop on GB + Ireland (centre ≈ -4.25°E). Portrait, because
# the British Isles are naturally tall; OUT dims match the Mercator aspect so
# the map isn't stretched.
UK_LON_MIN, UK_LON_MAX = -11.0, 2.5
UK_LAT_MIN, UK_LAT_MAX = 49.7, 59.3

OUT_W, OUT_H = 812, 1000
ZOOM = 8
TILE_SIZE = 256
EARTH_RADIUS = 6378137.0

TILE_URL = "https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png"
BASEMAP_CACHE = "static/dashboard_basemap_uk_z8.png"
COUNTIES_GEOJSON = "uk-counties.geojson"
COUNTY_COLOR = (50, 50, 80)   # dark navy on light basemap / satellite
COUNTY_WIDTH = 2

HISTORY_FRAMES = 24
RETENTION_HOURS = 48

# Composites are saved here and committed to the repo for GitHub Pages serving
COMPOSITE_DIR = "radarmicro/composites"

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


# ── County outlines ───────────────────────────────────────────────────────────

_counties_geojson = None

def _load_counties():
    global _counties_geojson
    if _counties_geojson is None:
        with open(COUNTIES_GEOJSON) as f:
            _counties_geojson = json.load(f)
    return _counties_geojson


def _ll_to_composite_px(lon, lat, crop_left, crop_top, crop_w, crop_h):
    mx, my = ll_to_merc(lon, lat)
    wpx, wpy = merc_to_world_px(mx, my, ZOOM)
    return (
        int((wpx - crop_left) / crop_w * OUT_W),
        int((wpy - crop_top)  / crop_h * OUT_H),
    )


def draw_county_outlines(img_rgb):
    gj = _load_counties()

    uk_mx_min, uk_my_min = ll_to_merc(UK_LON_MIN, UK_LAT_MIN)
    uk_mx_max, uk_my_max = ll_to_merc(UK_LON_MAX, UK_LAT_MAX)
    crop_left, crop_top     = merc_to_world_px(uk_mx_min, uk_my_max, ZOOM)
    crop_right, crop_bottom = merc_to_world_px(uk_mx_max, uk_my_min, ZOOM)
    crop_w = crop_right - crop_left
    crop_h = crop_bottom - crop_top

    draw = ImageDraw.Draw(img_rgb)

    for feature in gj.get('features', []):
        geom = feature.get('geometry', {})
        geom_type = geom.get('type')
        raw_coords = geom.get('coordinates', [])

        if geom_type == 'Polygon':
            polygons = [raw_coords]
        elif geom_type == 'MultiPolygon':
            polygons = raw_coords
        else:
            continue

        for polygon in polygons:
            for ring in polygon:
                pts = [
                    _ll_to_composite_px(lon, lat, crop_left, crop_top, crop_w, crop_h)
                    for lon, lat in ring
                ]
                if len(pts) >= 2:
                    draw.line(pts + [pts[0]], fill=COUNTY_COLOR, width=COUNTY_WIDTH)

    return img_rgb


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
    img_rgb = result.convert("RGB")
    draw_county_outlines(img_rgb)
    return img_rgb


def make_combined_composite(basemap_rgba, radar_url, sat_url):
    """Satellite background + radar colour overlay, like the main map view."""
    sat = download_image(sat_url).convert("RGBA")
    sat_crop = crop_overlay_to_uk(sat, SAT_LON_MIN, SAT_LON_MAX, SAT_LAT_MIN, SAT_LAT_MAX)
    sat_resize = sat_crop.resize((OUT_W, OUT_H), Image.LANCZOS)

    radar = download_image(radar_url).convert("RGBA")
    radar_crop = crop_overlay_to_uk(radar, RADAR_LON_MIN, RADAR_LON_MAX, RADAR_LAT_MIN, RADAR_LAT_MAX)
    radar_resize = radar_crop.resize((OUT_W, OUT_H), Image.LANCZOS)

    result = basemap_rgba.copy()
    result.paste(sat_resize, (0, 0), sat_resize)
    result.paste(radar_resize, (0, 0), radar_resize)
    img_rgb = result.convert("RGB")
    draw_county_outlines(img_rgb)
    return img_rgb


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
    cutoff_str = (datetime.utcnow() - timedelta(hours=RETENTION_HOURS)).strftime('%Y%m%d%H%M')
    paginator = r2.get_paginator('list_objects_v2')
    deleted = 0
    for prefix in ('radar_parallel/dashboard_', 'sat_gh/dashboard_'):
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
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
    m = re.search(r'/(\d{12})_', url)
    return m.group(1) if m else None


def needs_build(path):
    """True if the composite is missing or was built at different dimensions
    (e.g. after a framing/resolution change), so it gets regenerated."""
    if not os.path.exists(path):
        return True
    try:
        with Image.open(path) as im:
            return im.size != (OUT_W, OUT_H)
    except Exception:
        return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open("frames_parallel.json") as f:
        frames = json.load(f)
    if not frames:
        print("frames_parallel.json is empty.")
        return

    radar_frames = [fr for fr in frames if fr.get("url")][-HISTORY_FRAMES:]
    if not radar_frames:
        print("No radar frames found.")
        return

    basemap = get_basemap()
    os.makedirs(COMPOSITE_DIR, exist_ok=True)
    keep_local = set()

    for fr in radar_frames:
        ts = ts_from_url(fr["url"])
        if not ts:
            continue

        radar_local = os.path.join(COMPOSITE_DIR, f"dashboard_radar_{ts}.webp")
        keep_local.add(radar_local)
        if needs_build(radar_local):
            print(f"Generating radar composite {ts}…")
            try:
                comp = make_composite(basemap, fr["url"],
                                      RADAR_LON_MIN, RADAR_LON_MAX,
                                      RADAR_LAT_MIN, RADAR_LAT_MAX)
                comp.save(radar_local, "WEBP", quality=85)
                print(f"  Saved {radar_local}")
            except Exception as e:
                print(f"  Radar failed for {ts}: {e}")
                continue
        fr["dashboard_radar_url"] = f"composites/dashboard_radar_{ts}.webp"

        sat_url = fr.get("sat_url_bw")
        if sat_url:
            sat_local = os.path.join(COMPOSITE_DIR, f"dashboard_sat_{ts}.webp")
            keep_local.add(sat_local)
            if needs_build(sat_local):
                print(f"Generating sat composite {ts}…")
                try:
                    comp = make_composite(basemap, sat_url,
                                          SAT_LON_MIN, SAT_LON_MAX,
                                          SAT_LAT_MIN, SAT_LAT_MAX)
                    comp.save(sat_local, "WEBP", quality=85)
                    print(f"  Saved {sat_local}")
                except Exception as e:
                    print(f"  Sat failed for {ts}: {e}")
                    continue
            fr["dashboard_sat_url"] = f"composites/dashboard_sat_{ts}.webp"

            combined_local = os.path.join(COMPOSITE_DIR, f"dashboard_combined_{ts}.webp")
            keep_local.add(combined_local)
            if needs_build(combined_local):
                print(f"Generating combined composite {ts}…")
                try:
                    comp = make_combined_composite(basemap, fr["url"], sat_url)
                    comp.save(combined_local, "WEBP", quality=85)
                    print(f"  Saved {combined_local}")
                except Exception as e:
                    print(f"  Combined failed for {ts}: {e}")
                    continue
            fr["dashboard_combined_url"] = f"composites/dashboard_combined_{ts}.webp"

    # Remove old composites not in the current keep set
    for fname in os.listdir(COMPOSITE_DIR):
        fpath = os.path.join(COMPOSITE_DIR, fname)
        if fname.startswith("dashboard_") and fname.endswith(".webp") and fpath not in keep_local:
            os.remove(fpath)
            print(f"  Removed old composite: {fname}")

    with open("frames_parallel.json", "w") as f:
        json.dump(frames, f, indent=2)
    print("frames_parallel.json updated.")


if __name__ == "__main__":
    main()
