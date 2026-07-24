import hashlib
import os
import h5py
import numpy as np
import time
from pyproj import Transformer
from PIL import Image
from datetime import datetime, timedelta
import json
import boto3
from botocore import UNSIGNED
from botocore.client import Config

# Radar domain (native H5 extent)
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX = 43.7009, 62.9207
WIDTH, HEIGHT = 1725, 2175

# S3 Config
BUCKET = "met-office-radar-obs-data"
NUM_FILES = 192  # 2 days × 96 frames/day (15-min intervals)
H5_DIR = "data_h5"

# R2 Config
R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

if USE_R2:
    PNG_DIR = "static/radar_parallel_gh"
else:
    PNG_DIR = "static/radar_parallel"

PUBLISH_LAG_MINS = 7
RETRY_WAIT_SECS = 90
RETRY_INTERVAL = 15


def get_mapping(h5_sample):
    with h5py.File(h5_sample, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes): proj_str = proj_str.decode()
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
    rows, cols, valid = mapping
    with h5py.File(h5_file, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])

    out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]
    data = out_raw * gain + offset

    bounds = np.array([0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64])
    colors = np.array([
        [0, 0, 0, 0], [225, 245, 254, 255], [129, 212, 250, 255],
        [3, 169, 244, 255], [1, 87, 155, 255], [46, 125, 50, 255],
        [251, 192, 45, 255], [239, 108, 0, 255], [198, 40, 40, 255],
        [106, 27, 154, 255], [233, 30, 99, 255]
    ], dtype=np.uint8)

    indices = np.digitize(data, bounds)
    transparent_mask = (out_raw == nodata) | (out_raw == undetect) | (~valid)
    indices[transparent_mask] = 0

    rgba = colors[indices]
    Image.fromarray(rgba, 'RGBA').save(png_path)


def list_s3_keys_for_date(s3, date):
    prefix = f"radar/{date.strftime('%Y/%m/%d')}/"
    keys = []
    continuation_token = None

    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        resp = s3.list_objects_v2(**kwargs)

        if 'Contents' in resp:
            for obj in resp['Contents']:
                if obj['Key'].endswith('radar_rainrate_composite_1km_UK.h5'):
                    keys.append(obj['Key'])

        if resp.get('IsTruncated'):
            continuation_token = resp['NextContinuationToken']
        else:
            break

    return keys


def get_expected_latest_key():
    now = datetime.utcnow()
    adjusted = now - timedelta(minutes=PUBLISH_LAG_MINS)
    mins = (adjusted.minute // 15) * 15
    expected_dt = adjusted.replace(minute=mins, second=0, microsecond=0)
    date_prefix = expected_dt.strftime('%Y%m%d%H%M')
    return date_prefix, expected_dt


def collect_keys_with_retry(s3):
    expected_prefix, expected_dt = get_expected_latest_key()
    deadline = time.time() + RETRY_WAIT_SECS

    while True:
        keys = []
        date = datetime.utcnow()
        for _ in range(7):
            day_keys = list_s3_keys_for_date(s3, date)
            keys.extend(day_keys)
            date -= timedelta(days=1)
            if len(keys) >= NUM_FILES * 2:
                break

        latest_available = sorted(keys)[-1] if keys else ""
        latest_basename = os.path.basename(latest_available)

        if expected_prefix in latest_basename:
            print(f"Expected frame {expected_prefix} found in S3.")
            break
            
        if latest_basename and latest_basename[:12].isdigit() and latest_basename[:12] > expected_prefix:
            print(f"Newer frame {latest_basename[:12]} already in S3 (expected {expected_prefix}). Resuming.")
            break

        if time.time() >= deadline:
            print(f"Timeout waiting for {expected_prefix}. Using latest available: {latest_basename}")
            break

        remaining = int(deadline - time.time())
        print(f"Expected frame {expected_prefix} not yet in S3. Retrying in {RETRY_INTERVAL}s ({remaining}s remaining)...")
        time.sleep(RETRY_INTERVAL)

    return keys


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
    paginator = r2.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='radar_parallel/'):
        for obj in page.get('Contents', []):
            keys.add(obj['Key'])
    print(f"R2: {len(keys)} existing parallel objects found.")
    return keys


def list_sat_r2_keys(r2):
    """Return the set of sat_gh/ keys that have been confirmed uploaded to R2."""
    keys = set()
    paginator = r2.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='sat_gh/'):
        for obj in page.get('Contents', []):
            keys.add(obj['Key'])
    print(f"R2: {len(keys)} existing sat_gh objects found.")
    return keys


def upload_to_r2(r2, local_path, r2_key, content_type, existing_keys, force=False):
    if not force and r2_key in existing_keys:
        return True
    try:
        if force:
            # Content *may* have changed — skip the Class A PUT when it hasn't.
            # R2's ETag for a single-part upload is the body MD5; head_object
            # is Class B (cheap).
            with open(local_path, 'rb') as f:
                local_md5 = hashlib.md5(f.read()).hexdigest()
            try:
                head = r2.head_object(Bucket=R2_BUCKET, Key=r2_key)
                if head.get('ETag', '').strip('"') == local_md5:
                    return True
            except Exception:
                pass
        r2.upload_file(local_path, R2_BUCKET, r2_key, ExtraArgs={'ContentType': content_type})
        print(f"  Uploaded to R2: {r2_key}")
        existing_keys.add(r2_key)
        return True
    except Exception as e:
        print(f"  R2 upload failed for {r2_key}: {e}")
        return False


def cleanup_r2(r2, existing_keys, retention_days=14):
    # Works from the radar_parallel/ listing already fetched at startup rather
    # than re-listing the prefix — LIST is Class A, DeleteObject is free.
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime('%Y%m%d%H%M')
    deleted = 0

    for key in sorted(existing_keys):
        if not key.startswith('radar_parallel/'):
            continue
        ts = os.path.basename(key)[:12]
        if ts.isdigit() and ts < cutoff_str:
            r2.delete_object(Bucket=R2_BUCKET, Key=key)
            print(f"  Deleted from R2: {key}")
            deleted += 1

    print(f"R2 parallel cleanup: removed {deleted} objects total.")


def main():
    os.makedirs(H5_DIR, exist_ok=True)
    os.makedirs(PNG_DIR, exist_ok=True)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2_client() if USE_R2 else None
    r2_keys = list_r2_keys(r2) if USE_R2 else None
    sat_r2_keys = list_sat_r2_keys(r2) if USE_R2 else set()

    keys = collect_keys_with_retry(s3)
    latest_keys = sorted(keys)[-NUM_FILES:]

    mapping = None
    frames = []

    for key in latest_keys:
        h5_name = os.path.basename(key)
        h5_path = os.path.join(H5_DIR, h5_name)
        png_name = h5_name.replace(".h5", ".png")
        png_path = os.path.join(PNG_DIR, png_name)

        date_str = h5_name.split('_')[0]
        iso_time = None
        try:
            dt = datetime.strptime(date_str, "%Y%m%d%H%M")
            timestamp = dt.strftime("%a %d %b %Y %H:%M UTC")
        except Exception:
            timestamp = "Unknown"

        r2_key_radar = f"radar_parallel/{png_name}"
        if USE_R2 and r2_key_radar in r2_keys:
            print(f"Skip (already in R2): {png_name}")
        else:
            if not os.path.exists(png_path):
                if not os.path.exists(h5_path):
                    print(f"Downloading {h5_name}...")
                    s3.download_file(BUCKET, key, h5_path)
                print(f"Rendering {png_name}...")
                if mapping is None:
                    mapping = get_mapping(h5_path)
                render_png(h5_path, png_path, mapping)
            if USE_R2:
                upload_to_r2(r2, png_path, r2_key_radar, "image/png", r2_keys)

        sat_url_bw = None
        sat_url_vis = None
        sat_url_ir = None
        sat_url_ir_grey = None

        if timestamp != "Unknown":
            sat_name_bw      = h5_name.replace(".h5", "_sat_bw.jpg")
            sat_name_vis     = h5_name.replace(".h5", "_sat_vis.jpg")
            sat_name_ir      = h5_name.replace(".h5", "_sat_ir.jpg")
            sat_name_ir_grey = h5_name.replace(".h5", "_sat_ir_grey.jpg")
            
            if USE_R2:
                if f"sat_gh/{sat_name_bw}"      in sat_r2_keys: sat_url_bw      = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_bw}"
                if f"sat_gh/{sat_name_vis}"     in sat_r2_keys: sat_url_vis     = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_vis}"
                if f"sat_gh/{sat_name_ir}"      in sat_r2_keys: sat_url_ir      = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_ir}"
                if f"sat_gh/{sat_name_ir_grey}" in sat_r2_keys: sat_url_ir_grey = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_ir_grey}"
            else:
                sat_url_bw      = f"static/sat/{sat_name_bw}"
                sat_url_vis     = f"static/sat/{sat_name_vis}"
                sat_url_ir      = f"static/sat/{sat_name_ir}"
                sat_url_ir_grey = f"static/sat/{sat_name_ir_grey}"

        radar_url = f"{R2_PUBLIC_URL}/radar_parallel/{png_name}" if USE_R2 else f"static/radar_parallel/{png_name}"
        
        frame_data = {"time": timestamp, "url": radar_url}
        if sat_url_bw:
            frame_data["sat_url_bw"] = sat_url_bw
        if sat_url_vis:
            frame_data["sat_url_vis"] = sat_url_vis
        if sat_url_ir:
            frame_data["sat_url_ir"] = sat_url_ir
        if sat_url_ir_grey:
            frame_data["sat_url_ir_grey"] = sat_url_ir_grey
            
        frames.append(frame_data)

    with open("frames_parallel.json", "w") as f:
        json.dump(frames, f, indent=2)
    print(f"Manifest updated with {len(frames)} parallel frames.")

    if USE_R2:
        upload_to_r2(r2, "frames_parallel.json", "frames_parallel.json", "application/json", r2_keys, force=True)
        cleanup_r2(r2, r2_keys)


if __name__ == "__main__":
    main()
