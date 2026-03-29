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
import urllib.request

# Radar domain (fixed by Met Office composite coverage)
LON_MIN, LON_MAX = -11.5, 3.5
LAT_MIN, LAT_MAX = 49.0, 61.5
WIDTH, HEIGHT = 2400, 2000

# Satellite domain — 40% wider/taller than radar, same centre (-4.0°, 55.25°)
SAT_LON_MIN, SAT_LON_MAX = -14.5, 6.5
SAT_LAT_MIN, SAT_LAT_MAX = 46.5, 64.0

# S3 Config
BUCKET = "met-office-radar-obs-data"
NUM_FILES = 96
H5_DIR = "data_h5"

# R2 Config — set these as GitHub Actions secrets (never hardcode)
R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

# Use _gh dirs when uploading to R2, keeping them separate from VM-managed dirs
if USE_R2:
    PNG_DIR = "static/radar_gh"
    SAT_DIR = "static/sat_gh"
else:
    PNG_DIR = "static/radar"
    SAT_DIR = "static/sat"

# How long after an observation time before frames reliably appear on S3.
# The action runs at :10/:25/:40/:55, so the target frame is always 7+ min old.
PUBLISH_LAG_MINS = 7  # minutes after obs time before frame is reliably on S3

# Retry settings (short window since frame should already be present)
RETRY_WAIT_SECS = 90   # 1.5 min max
RETRY_INTERVAL = 15    # check every 15 seconds


def get_mapping(h5_sample):
    with h5py.File(h5_sample, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes): proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

    # Build the pixel grid in Web Mercator (EPSG:3857) space so that
    # pixel spacing matches Leaflet's internal Mercator projection.
    # Without this, a linear lat/lon grid causes a northward shift
    # because Mercator stretches the vertical axis at higher latitudes.
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)

    # Convert our overlay corners to Mercator
    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)

    # Build a uniform grid in Mercator space (top to bottom = ymax to ymin)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    # Transform Mercator coords directly into the radar's native projection
    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)

    # Convert UL corner to the radar projection for pixel indexing
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
    """List all matching H5 keys for a given date, using pagination."""
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
    """Build the S3 key prefix for the latest frame that should be published by now.

    We subtract PUBLISH_LAG_MINS before rounding so that we only target frames
    that Met Office has had time to publish. This avoids ever waiting/timing-out
    on a frame that hasn't landed on S3 yet.
    """
    now = datetime.utcnow()
    # Subtract the publish lag so we target a frame that should already exist
    adjusted = now - timedelta(minutes=PUBLISH_LAG_MINS)
    mins = (adjusted.minute // 15) * 15
    expected_dt = adjusted.replace(minute=mins, second=0, microsecond=0)
    # S3 filenames follow the pattern: YYYYmmddHHMM_..._radar_rainrate_composite_1km_UK.h5
    date_prefix = expected_dt.strftime('%Y%m%d%H%M')
    return date_prefix, expected_dt


def collect_keys_with_retry(s3):
    """
    Collect the N most recent H5 keys. If the latest expected frame isn't
    in S3 yet, retry for up to RETRY_WAIT_SECS seconds.
    """
    expected_prefix, expected_dt = get_expected_latest_key()
    deadline = time.time() + RETRY_WAIT_SECS

    while True:
        keys = []
        date = datetime.utcnow()
        for _ in range(7):  # Search up to 7 days
            day_keys = list_s3_keys_for_date(s3, date)
            keys.extend(day_keys)
            date -= timedelta(days=1)
            if len(keys) >= NUM_FILES * 2:
                break

        # Check if expected latest key is present
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


def download_sat_image(iso_time, layer_name, sat_path, fmt="image/jpeg", width=1200, height=1000, style=""):
    # Convert satellite lat/lon bounds to Web Mercator (EPSG:3857) meters.
    # We must request imagery in 3857 to match the Leaflet/OSM projection perfectly.
    # WMS 1.3.0 with EPSG:4326 would return a Plate Carree image that appears shifted
    # north when stretched linearly on a Mercator map.
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    m_xmin, m_ymin = ll_to_merc.transform(SAT_LON_MIN, SAT_LAT_MIN)
    m_xmax, m_ymax = ll_to_merc.transform(SAT_LON_MAX, SAT_LAT_MAX)

    # WMS 1.3.0 BBOX for EPSG:3857 is [xmin, ymin, xmax, ymax] (Easting, Northing)
    wms_url = (f"https://view.eumetsat.int/geoserver/ows?service=WMS&request=GetMap&version=1.3.0"
               f"&layers={layer_name}&styles={style}&format={fmt}&crs=EPSG:3857"
               f"&bbox={m_xmin},{m_ymin},{m_xmax},{m_ymax}&width={width}&height={height}&time={iso_time}")
    try:
        req = urllib.request.Request(wms_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
            content_type = response.headers.get('Content-Type', '')
            if 'xml' in content_type or len(data) < 5000:
                print(f"  [{layer_name}] Sat image for {iso_time}: got error or tiny response ({len(data)} bytes, {content_type})")
                return False
            with open(sat_path, 'wb') as out_file:
                out_file.write(data)
        print(f"  [{layer_name}] Sat image saved in 3857: {sat_path} ({len(data)} bytes)")
        return True
    except Exception as e:
        print(f"Failed to download sat image for {iso_time} ({layer_name}): {e}")
        return False


def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )


def upload_to_r2(r2, local_path, r2_key, content_type):
    try:
        r2.upload_file(local_path, R2_BUCKET, r2_key, ExtraArgs={'ContentType': content_type})
        print(f"  Uploaded to R2: {r2_key}")
        return True
    except Exception as e:
        print(f"  R2 upload failed for {r2_key}: {e}")
        return False


def cleanup_r2(r2, retention_days=14):
    """Delete R2 objects in radar_gh/ and sat_gh/ older than retention_days."""
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime('%Y%m%d%H%M')
    deleted = 0
    for prefix in ('radar_gh/', 'sat_gh/'):
        paginator = r2.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                basename = os.path.basename(key)
                ts = basename[:12]
                if ts.isdigit() and ts < cutoff_str:
                    r2.delete_object(Bucket=R2_BUCKET, Key=key)
                    print(f"  Deleted from R2: {key}")
                    deleted += 1
    print(f"R2 cleanup: removed {deleted} objects older than {retention_days} days.")


def main():
    os.makedirs(H5_DIR, exist_ok=True)
    os.makedirs(PNG_DIR, exist_ok=True)
    os.makedirs(SAT_DIR, exist_ok=True)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2_client() if USE_R2 else None

    keys = collect_keys_with_retry(s3)

    # Take latest NUM_FILES, sorted chronologically
    latest_keys = sorted(keys)[-NUM_FILES:]

    mapping = None
    frames = []

    for key in latest_keys:
        h5_name = os.path.basename(key)
        h5_path = os.path.join(H5_DIR, h5_name)
        png_name = h5_name.replace(".h5", ".png")
        png_path = os.path.join(PNG_DIR, png_name)

        # Determine timestamp
        date_str = h5_name.split('_')[0]
        iso_time = None
        try:
            dt = datetime.strptime(date_str, "%Y%m%d%H%M")
            timestamp = dt.strftime("%a %d %b %Y %H:%M UTC")
            iso_time = dt.strftime("%Y-%m-%dT%H:%M:00.000Z")
        except Exception:
            timestamp = "Unknown"

        if not os.path.exists(png_path):
            if not os.path.exists(h5_path):
                print(f"Downloading {h5_name}...")
                s3.download_file(BUCKET, key, h5_path)
            print(f"Rendering {png_name}...")
            if mapping is None:
                mapping = get_mapping(h5_path)
            render_png(h5_path, png_path, mapping)
        if USE_R2:
            upload_to_r2(r2, png_path, f"radar_gh/{png_name}", "image/png")

        sat_url_bw = None
        sat_url_vis = None
        sat_url_ir = None
        if iso_time:
            sat_name_bw  = h5_name.replace(".h5", "_sat_bw.jpg")
            sat_name_vis = h5_name.replace(".h5", "_sat_vis.png")
            sat_name_ir  = h5_name.replace(".h5", "_sat_ir.jpg")
            sat_path_bw  = os.path.join(SAT_DIR, sat_name_bw)
            sat_path_vis = os.path.join(SAT_DIR, sat_name_vis)
            sat_path_ir  = os.path.join(SAT_DIR, sat_name_ir)

            # GeoColour RGB (MTG FCI — seamless day/night colour blend)
            if not os.path.exists(sat_path_bw):
                print(f"Downloading GeoColour image {sat_name_bw}...")
                download_sat_image(iso_time, "mtg_fd:rgb_geocolour", sat_path_bw)
            if os.path.exists(sat_path_bw):
                if USE_R2:
                    upload_to_r2(r2, sat_path_bw, f"sat_gh/{sat_name_bw}", "image/jpeg")
                    sat_url_bw = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_bw}"
                else:
                    sat_url_bw = f"static/sat/{sat_name_bw}"

            # HRFI VIS0.6 (MTG FCI — 0.5km B&W visible, daytime only)
            # PNG avoids JPEG compression artefacts on high-contrast greyscale data
            if not os.path.exists(sat_path_vis):
                print(f"Downloading HRFI VIS image {sat_name_vis}...")
                download_sat_image(iso_time, "mtg_fd:vis06_hrfi", sat_path_vis,
                                   fmt="image/png")
            if os.path.exists(sat_path_vis):
                if USE_R2:
                    upload_to_r2(r2, sat_path_vis, f"sat_gh/{sat_name_vis}", "image/png")
                    sat_url_vis = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_vis}"
                else:
                    sat_url_vis = f"static/sat/{sat_name_vis}"

            # HRFI IR10.5 (MTG FCI — 1km thermal infrared, style 01)
            if not os.path.exists(sat_path_ir):
                print(f"Downloading HRFI IR image {sat_name_ir}...")
                download_sat_image(iso_time, "mtg_fd:ir105_hrfi", sat_path_ir,
                                   style="mtg_fd_ir105_hrfi_style_01")
            if os.path.exists(sat_path_ir):
                if USE_R2:
                    upload_to_r2(r2, sat_path_ir, f"sat_gh/{sat_name_ir}", "image/jpeg")
                    sat_url_ir = f"{R2_PUBLIC_URL}/sat_gh/{sat_name_ir}"
                else:
                    sat_url_ir = f"static/sat/{sat_name_ir}"

        radar_url = f"{R2_PUBLIC_URL}/radar_gh/{png_name}" if USE_R2 else f"static/radar/{png_name}"
        frame_data = {"time": timestamp, "url": radar_url}
        if sat_url_bw:
            frame_data["sat_url_bw"] = sat_url_bw
        if sat_url_vis:
            frame_data["sat_url_vis"] = sat_url_vis
        if sat_url_ir:
            frame_data["sat_url_ir"] = sat_url_ir
        frames.append(frame_data)

    with open("frames.json", "w") as f:
        json.dump(frames, f, indent=2)
    print(f"Manifest updated with {len(frames)} frames.")

    if USE_R2:
        cleanup_r2(r2)


if __name__ == "__main__":
    main()
