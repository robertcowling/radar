#!/usr/bin/env python3
"""
fetch_opera.py — Incremental 5-minute EUMETNET OPERA composite radar fetcher.

Queries the MeteoGate EDR API, downloads H5 composite files, projects them
to the standard Web Mercator grid for the UK "Full domain", renders PNGs,
and updates frames_5m.json.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import io
from datetime import datetime, timedelta, timezone
import numpy as np
import h5py
from pyproj import Transformer
from PIL import Image
import boto3

# Radar domain (same as process_parallel.py for pixel alignment)
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX = 43.7009, 62.9207
WIDTH, HEIGHT = 1725, 2175

NUM_FILES = 288  # 24 hours of 5-minute data (288 frames)
H5_DIR = "data_h5_opera"

# R2 Config
R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

if USE_R2:
    PNG_DIR = "static/radar5_gh"
else:
    PNG_DIR = "static/radar5"

# MeteoGate API Config
METEOGATE_API_KEY = os.environ.get("METEOGATE_API_KEY", "").strip()


def make_meteogate_request(url, timeout=30):
    """
    Perform a GET request to the MeteoGate API.
    Injects the apikey header if present, and implements a retry loop
    with exponential backoff on HTTP 429 (Too Many Requests).
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    if METEOGATE_API_KEY:
        headers['apikey'] = METEOGATE_API_KEY

    backoff = 2.0
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                print(f"MeteoGate request to {url} rate limited (429). Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2.0
                continue
            raise


def get_mapping(h5_sample):
    """Generate EPSG:3857 to OPERA radar laea mapping."""
    with h5py.File(h5_sample, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes):
            proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)

    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)

    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)

    ll_to_radar = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)

    cols = np.round((X_radar - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Y_radar) / yscale).astype(np.int32)
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    return rows, cols, valid


def render_png(h5_file, png_path, mapping):
    """Render OPERA dBZ radar to rainrate color indices PNG."""
    rows, cols, valid = mapping
    with h5py.File(h5_file, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])

    # Project raw dBZ grid
    out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]
    
    # Calculate dBZ and rain rate using Marshall-Palmer
    valid_out = (out_raw != nodata) & (out_raw != undetect) & (out_raw != 0)
    dbz = out_raw * gain + offset
    
    r = np.zeros((HEIGHT, WIDTH))
    # Threshold below -20 dBZ to be zero to avoid tiny values / noise
    calc_mask = valid_out & (dbz > -20.0)
    
    # Marshall-Palmer: Z = 200 * R^1.6  =>  R = (10^(dBZ/10) / 200)^0.625
    z = 10.0 ** (dbz[calc_mask] / 10.0)
    r[calc_mask] = (z / 200.0) ** 0.625

    # Define color thresholds (identical to process_parallel.py)
    bounds = np.array([0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64])
    colors = np.array([
        [0, 0, 0, 0],            # Transparent / No rain
        [225, 245, 254, 255],    # 0.1 - 0.25
        [129, 212, 250, 255],    # 0.25 - 0.5
        [3, 169, 244, 255],      # 0.5 - 1.0
        [1, 87, 155, 255],       # 1.0 - 2.0
        [46, 125, 50, 255],      # 2.0 - 4.0
        [251, 192, 45, 255],     # 4.0 - 8.0
        [239, 108, 0, 255],      # 8.0 - 16.0
        [198, 40, 40, 255],      # 16.0 - 32.0
        [106, 27, 154, 255],     # 32.0 - 64.0
        [233, 30, 99, 255]       # > 64.0
    ], dtype=np.uint8)

    indices = np.digitize(r, bounds)
    indices[~valid_out] = 0

    rgba = colors[indices]
    Image.fromarray(rgba, 'RGBA').save(png_path)


def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )


def list_r2_keys(r2):
    keys = set()
    try:
        paginator = r2.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='radar5/'):
            for obj in page.get('Contents', []):
                keys.add(obj['Key'])
        print(f"R2: {len(keys)} existing 5-minute radar objects found.")
    except Exception as e:
        print(f"Error querying R2: {e}")
    return keys


def upload_to_r2(r2, local_path, r2_key, content_type, existing_keys, force=False):
    if not force and r2_key in existing_keys:
        return True
    try:
        r2.upload_file(local_path, R2_BUCKET, r2_key, ExtraArgs={'ContentType': content_type})
        print(f"  Uploaded to R2: {r2_key}")
        existing_keys.add(r2_key)
        return True
    except Exception as e:
        print(f"  R2 upload failed for {r2_key}: {e}")
        return False


def cleanup_r2(r2, retention_days=2):
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime('%Y%m%d%H%M')
    deleted = 0

    try:
        paginator = r2.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='radar5/'):
            for obj in page.get('Contents', []):
                key = obj['Key']
                basename = os.path.basename(key)
                ts = basename[:12]
                if ts.isdigit() and ts < cutoff_str:
                    r2.delete_object(Bucket=R2_BUCKET, Key=key)
                    print(f"  Deleted from R2: {key}")
                    deleted += 1
        print(f"R2 5m cleanup: removed {deleted} objects total.")
    except Exception as e:
        print(f"Warning: R2 cleanup failed: {e}")


def fetch_meteogate_list():
    """Retrieve list of available files from MeteoGate API."""
    url = "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations/0-20010-0-OPERA"
    print(f"Fetching metadata from {url}...")
    try:
        with make_meteogate_request(url, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
            coverages = data.get("coverages", [])
            if not coverages:
                return []
            
            h5_links = []
            for coverage in coverages:
                links = coverage.get("links", [])
                h5_links.extend([l for l in links if l.get("href", "").endswith("DBZH.h5")])
            
            print(f"Found {len(h5_links)} DBZH.h5 file links in API response.")
            return h5_links
    except Exception as e:
        print(f"Error querying MeteoGate API: {e}")
        return []


def main():
    os.makedirs(H5_DIR, exist_ok=True)
    os.makedirs(PNG_DIR, exist_ok=True)

    r2 = get_r2_client() if USE_R2 else None
    r2_keys = list_r2_keys(r2) if USE_R2 else set()

    h5_links = fetch_meteogate_list()
    if not h5_links:
        print("No radar files available. Exiting.")
        return

    # Take the latest NUM_FILES (rolling 24h)
    latest_links = sorted(h5_links, key=lambda l: l.get("href", ""))[-NUM_FILES:]

    mapping = None
    frames = []

    for item in latest_links:
        file_url = item["href"]
        h5_name = os.path.basename(file_url)
        
        # Format name is e.g. OPERA@20260530T1945@0@DBZH.h5
        # We extract timestamp: 202605301945
        try:
            parts = h5_name.split('@')
            t_part = parts[1] # "20260530T1945"
            date_str = t_part.replace('T', '') # "202605301945"
            dt = datetime.strptime(date_str, "%Y%m%d%H%M")
            timestamp = dt.strftime("%a %d %b %Y %H:%M UTC")
        except Exception as e:
            print(f"Skip file with invalid timestamp format {h5_name}: {e}")
            continue

        h5_path = os.path.join(H5_DIR, h5_name)
        png_name = f"{date_str}_OPERA_comp_5m.png"
        png_path = os.path.join(PNG_DIR, png_name)

        r2_key_radar = f"radar5/{png_name}"

        # Processing or caching check
        if USE_R2 and r2_key_radar in r2_keys:
            # File exists in R2, skip processing
            pass
        else:
            if not os.path.exists(png_path):
                # Download H5 file
                if not os.path.exists(h5_path):
                    print(f"Downloading {h5_name}...")
                    try:
                        with make_meteogate_request(file_url, timeout=60) as response, open(h5_path, 'wb') as out_file:
                            out_file.write(response.read())
                    except Exception as e:
                        print(f"Failed to download {file_url}: {e}")
                        continue
                
                # Render PNG
                print(f"Rendering {png_name}...")
                try:
                    if mapping is None:
                        mapping = get_mapping(h5_path)
                    render_png(h5_path, png_path, mapping)
                except Exception as e:
                    print(f"Failed to render {h5_name}: {e}")
                    continue
                finally:
                    # Clean up the raw H5 immediately to keep workspace lightweight
                    if os.path.exists(h5_path):
                        try:
                            os.remove(h5_path)
                        except Exception:
                            pass

            # Upload to R2 if configured
            if USE_R2:
                upload_to_r2(r2, png_path, r2_key_radar, "image/png", r2_keys)

        # Build absolute or root-relative path for manifest
        # If R2 is used, absolute public URL is great.
        # If local development, root-relative /static/radar5/... works in subfolders too.
        radar_url = f"{R2_PUBLIC_URL}/radar5/{png_name}" if USE_R2 else f"/static/radar5/{png_name}"
        
        frames.append({
            "time": timestamp,
            "url": radar_url
        })

    # Save manifest frames_5m.json
    manifest_path = "frames_5m.json"
    with open(manifest_path, "w") as f:
        json.dump(frames, f, indent=2)
    print(f"Manifest frames_5m.json updated with {len(frames)} frames.")

    if USE_R2:
        upload_to_r2(r2, manifest_path, "frames_5m.json", "application/json", r2_keys, force=True)
        cleanup_r2(r2)

    # Clean up local PNGs older than 2 days to avoid filling disk space
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    cutoff_str = cutoff.strftime('%Y%m%d%H%M')
    local_deleted = 0
    for f in os.listdir(PNG_DIR):
        if f.endswith("_OPERA_comp_5m.png"):
            ts = f.split('_')[0]
            if ts.isdigit() and ts < cutoff_str:
                try:
                    os.remove(os.path.join(PNG_DIR, f))
                    local_deleted += 1
                except Exception:
                    pass
    if local_deleted:
        print(f"Cleaned up {local_deleted} local PNG files older than 48 hours.")


if __name__ == "__main__":
    main()
