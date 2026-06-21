#!/usr/bin/env python3
"""
Generate 812×1000 dashboard composite WebP images (CartoDB basemap + radar/sat overlay).
Composite style version is auto-detected from this script's content hash; any change
to this file triggers a full re-build of all composites on the next workflow run.
"""

import hashlib
import io
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timedelta

import boto3
from PIL import Image, ImageDraw, ImageFilter

try:
    import cairosvg
    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False

# ~18% Mercator zoom vs original extents; center preserved at lon -4.25, Mercator cy ~7296 km.
# Shetland (60.15°N) included; southern limit 48°N (N Brittany / S Ireland).
UK_LON_MIN, UK_LON_MAX = -13.0, 4.5
UK_LAT_MIN, UK_LAT_MAX = 48.0, 60.5

OUT_W, OUT_H = 812, 1000
ZOOM = 8
TILE_SIZE = 256
EARTH_RADIUS = 6378137.0

TILE_URL = "https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png"
BASEMAP_CACHE = "static/dashboard_basemap_uk_z8.png"
BASEMAP_META  = "static/dashboard_basemap_uk_z8.meta"  # stores bounds so stale cache is detected

# R2-hosted GeoJSON files downloaded on first run and cached locally.
# Not committed to git; persisted via GitHub Actions cache.
EUROPE_GEO_URL    = "https://pub-96089466ef9841fb90f34b6f89f0a090.r2.dev/geo/Europe%20geo.json"
EUROPE_GEO_CACHE  = "europe_geo.json"
UK_REGIONS_URL    = "https://pub-96089466ef9841fb90f34b6f89f0a090.r2.dev/geo/uk_regions.geojson"
UK_REGIONS_CACHE  = "uk_regions_r2.geojson"

# Boundary layers drawn bottom-to-top.
# Each entry: (filepath, rgb_color, line_width_px, halo_opacity 0-1)
BOUNDARY_LAYERS = [
    (EUROPE_GEO_CACHE,   (80, 100, 130),  1, 0.55),  # European country outlines
    (UK_REGIONS_CACHE,   (0,   0,   0),   2, 0.60),  # UK regions, black + highlight
]

HISTORY_FRAMES = 24
RETENTION_HOURS = 48

MSLP_META_FILE = "mslp_meta.json"
MSLP_HISTORY   = 24   # last 24 hourly frames (= 24 h)

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


# ── Boundary overlays ─────────────────────────────────────────────────────────

_geojson_cache = {}

def _load_geojson(filepath):
    """Load a GeoJSON file, normalising FeatureCollection or single Feature to a list."""
    if filepath not in _geojson_cache:
        with open(filepath) as f:
            data = json.load(f)
        if data.get("type") == "FeatureCollection":
            _geojson_cache[filepath] = data.get("features", [])
        elif data.get("type") == "Feature":
            _geojson_cache[filepath] = [data]
        else:
            _geojson_cache[filepath] = []
    return _geojson_cache[filepath]


def _crop_extents():
    """Return (crop_left, crop_top, crop_w, crop_h) in world-pixel space at ZOOM."""
    uk_mx_min, uk_my_min = ll_to_merc(UK_LON_MIN, UK_LAT_MIN)
    uk_mx_max, uk_my_max = ll_to_merc(UK_LON_MAX, UK_LAT_MAX)
    left,  top    = merc_to_world_px(uk_mx_min, uk_my_max, ZOOM)
    right, bottom = merc_to_world_px(uk_mx_max, uk_my_min, ZOOM)
    return left, top, right - left, bottom - top


def _ll_to_composite_px(lon, lat, crop_left, crop_top, crop_w, crop_h):
    mx, my = ll_to_merc(lon, lat)
    wpx, wpy = merc_to_world_px(mx, my, ZOOM)
    return (
        int((wpx - crop_left) / crop_w * OUT_W),
        int((wpy - crop_top)  / crop_h * OUT_H),
    )


def _iter_rings(features, crop_left, crop_top, crop_w, crop_h):
    """Yield closed pixel-coordinate point lists for every ring in a feature list."""
    for feature in features:
        geom = feature.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        else:
            continue
        for poly in polys:
            for ring in poly:
                pts = [_ll_to_composite_px(c[0], c[1], crop_left, crop_top, crop_w, crop_h)
                       for c in ring]
                if len(pts) >= 2:
                    yield pts + [pts[0]]


def draw_boundary_layers(img_rgb):
    """Draw all BOUNDARY_LAYERS with a per-layer white halo for night-sat contrast."""
    crop_left, crop_top, crop_w, crop_h = _crop_extents()

    for filepath, line_color, line_width, halo_opacity in BOUNDARY_LAYERS:
        if not os.path.exists(filepath):
            continue
        features = _load_geojson(filepath)
        if not features:
            continue
        rings = list(_iter_rings(features, crop_left, crop_top, crop_w, crop_h))
        if not rings:
            continue

        if halo_opacity > 0:
            halo_w = line_width + 3
            glow = Image.new("L", img_rgb.size, 0)
            gd = ImageDraw.Draw(glow)
            for pts in rings:
                gd.line(pts, fill=255, width=halo_w)
            glow = glow.filter(ImageFilter.GaussianBlur(radius=1.5))
            white_layer = Image.new("RGBA", img_rgb.size, (255, 255, 255, 0))
            white_layer.putalpha(glow.point(lambda x: int(x * halo_opacity)))
            img_rgba = img_rgb.convert("RGBA")
            img_rgba = Image.alpha_composite(img_rgba, white_layer)
            img_rgb.paste(img_rgba.convert("RGB"))

        draw = ImageDraw.Draw(img_rgb)
        for pts in rings:
            draw.line(pts, fill=line_color, width=line_width)

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
    # Validate cached basemap against current bounds so a stale cache (built
    # with different extents) is never silently used — that causes visible
    # misalignment between the basemap tiles and GeoJSON boundary overlays.
    expected_meta = f"{UK_LON_MIN},{UK_LON_MAX},{UK_LAT_MIN},{UK_LAT_MAX},{ZOOM}"
    cache_valid = False
    if os.path.exists(BASEMAP_CACHE) and os.path.exists(BASEMAP_META):
        with open(BASEMAP_META) as f:
            cache_valid = f.read().strip() == expected_meta
    if cache_valid:
        print("Using cached basemap.")
        return Image.open(BASEMAP_CACHE).convert("RGBA")
    os.makedirs(os.path.dirname(BASEMAP_CACHE), exist_ok=True)
    bm = build_basemap()
    bm.save(BASEMAP_CACHE, "PNG")
    with open(BASEMAP_META, "w") as f:
        f.write(expected_meta)
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


def _overlay(url, lon_min, lon_max, lat_min, lat_max):
    """Download, crop, and resize an overlay; return the RGBA image."""
    raw = download_image(url).convert("RGBA")
    cropped = crop_overlay_to_uk(raw, lon_min, lon_max, lat_min, lat_max)
    return cropped.resize((OUT_W, OUT_H), Image.LANCZOS)


def make_composite(basemap_rgba, overlay_url, ov_lon_min, ov_lon_max, ov_lat_min, ov_lat_max):
    ov = _overlay(overlay_url, ov_lon_min, ov_lon_max, ov_lat_min, ov_lat_max)
    result = basemap_rgba.copy()
    result.paste(ov, (0, 0), ov)
    img_rgb = result.convert("RGB")
    draw_boundary_layers(img_rgb)
    return img_rgb


def make_combined_composite(basemap_rgba, radar_url, sat_url):
    """Satellite background + radar colour overlay, matching the main /radar map."""
    sat   = _overlay(sat_url,
                     SAT_LON_MIN, SAT_LON_MAX, SAT_LAT_MIN, SAT_LAT_MAX)
    radar = _overlay(radar_url,
                     RADAR_LON_MIN, RADAR_LON_MAX, RADAR_LAT_MIN, RADAR_LAT_MAX)

    result = basemap_rgba.copy()
    result.paste(sat, (0, 0), sat)
    result.paste(radar, (0, 0), radar)

    # Mild unsharp mask on the blended image before boundaries (sharpens sat pixelation)
    img_rgb = result.convert("RGB")
    img_rgb = img_rgb.filter(ImageFilter.UnsharpMask(radius=0.8, percent=40, threshold=3))
    draw_boundary_layers(img_rgb)
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


def r2_upload_file(r2, local_path, r2_key):
    with open(local_path, 'rb') as f:
        data = f.read()
    r2.put_object(
        Bucket=R2_BUCKET, Key=r2_key,
        Body=data,
        ContentType='image/webp',
        CacheControl='public, max-age=86400',
    )
    print(f"  Uploaded to R2: {r2_key} ({len(data):,} bytes)")


def cleanup_old_composites(r2, keep_keys):
    cutoff_str = (datetime.utcnow() - timedelta(hours=RETENTION_HOURS)).strftime('%Y%m%d%H%M')
    paginator = r2.get_paginator('list_objects_v2')
    deleted = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='composites/'):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key in keep_keys:
                continue
            m = re.search(r'_(\d{12})\.webp$', key)
            if m and m.group(1) < cutoff_str:
                r2.delete_object(Bucket=R2_BUCKET, Key=key)
                print(f"  Deleted old composite from R2: {key}")
                deleted += 1
    if deleted:
        print(f"Cleanup: removed {deleted} old composites from R2.")


def ts_from_url(url):
    m = re.search(r'/(\d{12})_', url)
    return m.group(1) if m else None




def make_mslp_composite(basemap_rgba, mslp_svg_url, sat_url=None):
    """Basemap + sat background + MSLP isobar SVG overlay + boundary layers."""
    result = basemap_rgba.copy()

    if sat_url:
        sat = _overlay(sat_url, SAT_LON_MIN, SAT_LON_MAX, SAT_LAT_MIN, SAT_LAT_MAX)
        result.paste(sat, (0, 0), sat)

    req = urllib.request.Request(mslp_svg_url, headers={'User-Agent': 'Mozilla/5.0 (radar-dashboard-bot)'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        svg_bytes = resp.read()
    png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=1800)
    mslp_raw = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    mslp_uk  = crop_overlay_to_uk(mslp_raw, SAT_LON_MIN, SAT_LON_MAX, SAT_LAT_MIN, SAT_LAT_MAX)
    mslp_uk  = mslp_uk.resize((OUT_W, OUT_H), Image.LANCZOS)
    result.paste(mslp_uk, (0, 0), mslp_uk)

    img_rgb = result.convert("RGB")
    if sat_url:
        img_rgb = img_rgb.filter(ImageFilter.UnsharpMask(radius=0.8, percent=40, threshold=3))
    draw_boundary_layers(img_rgb)
    return img_rgb


def needs_build(path):
    """True if the composite is missing or was built at different dimensions."""
    if not os.path.exists(path):
        return True
    try:
        with Image.open(path) as im:
            return im.size != (OUT_W, OUT_H)
    except Exception:
        return True


def _script_hash():
    with open(__file__, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _fetch_if_missing(url, local_path, label):
    if os.path.exists(local_path):
        return
    print(f"Downloading {label} from R2…")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (radar-dashboard-bot)'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(local_path, 'wb') as f:
        f.write(data)
    print(f"  Saved {local_path} ({len(data):,} bytes)")


def ensure_geo_files():
    _fetch_if_missing(EUROPE_GEO_URL,   EUROPE_GEO_CACHE,  "Europe boundary")
    _fetch_if_missing(UK_REGIONS_URL,   UK_REGIONS_CACHE,  "UK regions")


def purge_on_style_change():
    """Purge all composites when this script has changed since the last build.

    Stores the script's content hash in COMPOSITE_DIR/.version so the check
    survives across git checkouts and GitHub Actions cache restores.
    """
    os.makedirs(COMPOSITE_DIR, exist_ok=True)
    version_file = os.path.join(COMPOSITE_DIR, ".version")
    current_hash = _script_hash()
    if os.path.exists(version_file):
        with open(version_file) as f:
            if f.read().strip() == current_hash:
                return  # no change
    print(f"Script changed (hash {current_hash}), purging all composites for full rebuild…")
    for fname in os.listdir(COMPOSITE_DIR):
        if fname.endswith(".webp"):
            try:
                os.remove(os.path.join(COMPOSITE_DIR, fname))
            except OSError:
                pass
    with open(version_file, "w") as f:
        f.write(current_hash)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ensure_geo_files()
    purge_on_style_change()

    with open("frames_parallel.json") as f:
        frames = json.load(f)
    if not frames:
        print("frames_parallel.json is empty.")
        return

    radar_frames = [fr for fr in frames if fr.get("url")][-HISTORY_FRAMES:]
    if not radar_frames:
        print("No radar frames found.")
        return

    r2 = get_r2_client() if USE_R2 else None
    if r2:
        print("R2 available — composites will be uploaded to R2.")
    else:
        print("R2 not configured — composites stored locally only.")

    basemap = get_basemap()
    os.makedirs(COMPOSITE_DIR, exist_ok=True)
    keep_local = set()
    keep_r2_keys = set()

    for fr in radar_frames:
        ts = ts_from_url(fr["url"])
        if not ts:
            continue

        radar_local = os.path.join(COMPOSITE_DIR, f"dashboard_radar_{ts}.webp")
        radar_r2_key = f"composites/dashboard_radar_{ts}.webp"
        keep_local.add(radar_local)
        if r2:
            keep_r2_keys.add(radar_r2_key)
        if needs_build(radar_local):
            print(f"Generating radar composite {ts}…")
            try:
                comp = make_composite(basemap, fr["url"],
                                      RADAR_LON_MIN, RADAR_LON_MAX,
                                      RADAR_LAT_MIN, RADAR_LAT_MAX)
                comp.save(radar_local, "WEBP", quality=90)
                if r2:
                    r2_upload_file(r2, radar_local, radar_r2_key)
            except Exception as e:
                print(f"  Radar failed for {ts}: {e}")
                continue
        fr["dashboard_radar_url"] = (
            f"{R2_PUBLIC_URL}/{radar_r2_key}" if r2
            else f"composites/dashboard_radar_{ts}.webp"
        )

        sat_url = fr.get("sat_url_bw")
        if sat_url:
            sat_local = os.path.join(COMPOSITE_DIR, f"dashboard_sat_{ts}.webp")
            sat_r2_key = f"composites/dashboard_sat_{ts}.webp"
            keep_local.add(sat_local)
            if r2:
                keep_r2_keys.add(sat_r2_key)
            if needs_build(sat_local):
                print(f"Generating sat composite {ts}…")
                try:
                    comp = make_composite(basemap, sat_url,
                                          SAT_LON_MIN, SAT_LON_MAX,
                                          SAT_LAT_MIN, SAT_LAT_MAX)
                    comp.save(sat_local, "WEBP", quality=90)
                    if r2:
                        r2_upload_file(r2, sat_local, sat_r2_key)
                except Exception as e:
                    print(f"  Sat failed for {ts}: {e}")
                    continue
            fr["dashboard_sat_url"] = (
                f"{R2_PUBLIC_URL}/{sat_r2_key}" if r2
                else f"composites/dashboard_sat_{ts}.webp"
            )

            combined_local = os.path.join(COMPOSITE_DIR, f"dashboard_combined_{ts}.webp")
            combined_r2_key = f"composites/dashboard_combined_{ts}.webp"
            keep_local.add(combined_local)
            if r2:
                keep_r2_keys.add(combined_r2_key)
            if needs_build(combined_local):
                print(f"Generating combined composite {ts}…")
                try:
                    comp = make_combined_composite(basemap, fr["url"], sat_url)
                    comp.save(combined_local, "WEBP", quality=90)
                    if r2:
                        r2_upload_file(r2, combined_local, combined_r2_key)
                except Exception as e:
                    print(f"  Combined failed for {ts}: {e}")
                    continue
            fr["dashboard_combined_url"] = (
                f"{R2_PUBLIC_URL}/{combined_r2_key}" if r2
                else f"composites/dashboard_combined_{ts}.webp"
            )

        sat_url_ir = fr.get("sat_url_ir")
        if sat_url_ir:
            ir_local = os.path.join(COMPOSITE_DIR, f"dashboard_ir_{ts}.webp")
            ir_r2_key = f"composites/dashboard_ir_{ts}.webp"
            keep_local.add(ir_local)
            if r2:
                keep_r2_keys.add(ir_r2_key)
            if needs_build(ir_local):
                print(f"Generating IR sat composite {ts}…")
                try:
                    comp = make_composite(basemap, sat_url_ir,
                                          SAT_LON_MIN, SAT_LON_MAX,
                                          SAT_LAT_MIN, SAT_LAT_MAX)
                    comp.save(ir_local, "WEBP", quality=90)
                    if r2:
                        r2_upload_file(r2, ir_local, ir_r2_key)
                except Exception as e:
                    print(f"  IR sat failed for {ts}: {e}")
                    continue
            fr["dashboard_ir_url"] = (
                f"{R2_PUBLIC_URL}/{ir_r2_key}" if r2
                else f"composites/dashboard_ir_{ts}.webp"
            )

    # ── MSLP composites (isobars on basemap only, no sat underlay, hourly, last 24 h) ──
    if HAS_CAIROSVG and os.path.exists(MSLP_META_FILE):
        with open(MSLP_META_FILE) as f:
            mslp_meta = json.load(f)

        # Exclude GFS forecast frames (valid_time in the future) by comparing timestamps
        now_ts = datetime.utcnow().strftime('%Y%m%d%H%M')
        mslp_all = mslp_meta.get("frames", [])
        mslp_analysis = [mf for mf in mslp_all
                         if ts_from_url(mf.get("url", "")) and ts_from_url(mf["url"]) <= now_ts]
        mslp_recent = mslp_analysis[-MSLP_HISTORY:]

        mslp_out = []
        for mf in mslp_recent:
            mslp_ts = ts_from_url(mf["url"])
            if not mslp_ts:
                continue
            mslp_local = os.path.join(COMPOSITE_DIR, f"dashboard_mslp_{mslp_ts}.webp")
            mslp_r2_key = f"composites/dashboard_mslp_{mslp_ts}.webp"
            keep_local.add(mslp_local)
            if r2:
                keep_r2_keys.add(mslp_r2_key)
            if needs_build(mslp_local):
                print(f"Generating MSLP composite {mslp_ts}…")
                try:
                    comp = make_mslp_composite(basemap, mf["url"])
                    comp.save(mslp_local, "WEBP", quality=90)
                    if r2:
                        r2_upload_file(r2, mslp_local, mslp_r2_key)
                except Exception as e:
                    print(f"  MSLP failed for {mslp_ts}: {e}")
                    continue
            mslp_out.append({
                "time": mf["valid_time"],
                "mslp": (f"{R2_PUBLIC_URL}/{mslp_r2_key}" if r2
                         else f"composites/dashboard_mslp_{mslp_ts}.webp"),
            })

        with open("frames_mslp.json", "w") as f:
            json.dump(mslp_out, f, indent=2)
        print(f"frames_mslp.json updated with {len(mslp_out)} frames.")
    elif not HAS_CAIROSVG:
        print("cairosvg not available — MSLP composites skipped.")

    # Remove old local composites not in the current keep set
    for fname in os.listdir(COMPOSITE_DIR):
        fpath = os.path.join(COMPOSITE_DIR, fname)
        if fname.startswith("dashboard_") and fname.endswith(".webp") and fpath not in keep_local:
            os.remove(fpath)
            print(f"  Removed old local composite: {fname}")

    # Prune old composites from R2
    if r2:
        try:
            cleanup_old_composites(r2, keep_r2_keys)
        except Exception as e:
            print(f"R2 cleanup failed: {e}")

    with open("frames_parallel.json", "w") as f:
        json.dump(frames, f, indent=2)
    print("frames_parallel.json updated.")


if __name__ == "__main__":
    main()
