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
import io

# Output width in pixels — height is derived from the H5 Mercator aspect ratio
WIDTH = 2400

# Satellite domain — same centre (-4.0°, 55.25°); 2× taller than before
SAT_LON_MIN, SAT_LON_MAX = -40.4, 32.4
SAT_LAT_MIN, SAT_LAT_MAX = 37.75, 67.5

# S3 Config
BUCKET = "met-office-radar-obs-data"
NUM_FILES = 192  # 2 days × 96 frames/day (15-min intervals)
SAT_MAX_AGE_HOURS = 24  # give up retrying sat downloads after this long
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
        # Derive output extent from H5 grid corners so the PNG covers the full data
        lat_min = min(float(where['LL_lat']), float(where['LR_lat']))
        lat_max = max(float(where['UL_lat']), float(where['UR_lat']))
        lon_min = min(float(where['LL_lon']), float(where['UL_lon']))
        lon_max = max(float(where['LR_lon']), float(where['UR_lon']))

    # Build the pixel grid in Web Mercator (EPSG:3857) space so that
    # pixel spacing matches Leaflet's internal Mercator projection.
    # Without this, a linear lat/lon grid causes a northward shift
    # because Mercator stretches the vertical axis at higher latitudes.
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)

    merc_xmin, merc_ymin = ll_to_merc.transform(lon_min, lat_min)
    merc_xmax, merc_ymax = ll_to_merc.transform(lon_max, lat_max)
    # Height derived from Mercator aspect ratio so pixels are square in map space
    height = round(WIDTH * (merc_ymax - merc_ymin) / (merc_xmax - merc_xmin))

    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, height)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)

    ll_to_radar = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)

    cols = np.round((X_radar - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Y_radar) / yscale).astype(np.int32)
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    return rows, cols, valid, height, (lat_min, lat_max, lon_min, lon_max)


def render_png(h5_file, png_path, mapping):
    rows, cols, valid, height, _ = mapping
    with h5py.File(h5_file, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])

    out_raw = np.full((height, WIDTH), nodata, dtype=raw.dtype)
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


def download_sat_image(iso_time, layer_name, sat_path, fmt="image/jpeg", width=3200, height=3200, style="", grayscale=False):
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
            # Re-encode to optimised JPEG when source is PNG, or for
            # grayscale layers where dropping to single-channel saves space
            is_png = data[:4] == b'\x89PNG'
            if is_png or grayscale:
                from PIL import Image
                mode = "L" if grayscale else "RGB"
                img = Image.open(io.BytesIO(data)).convert(mode)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75, optimize=True)
                reencoded = buf.getvalue()
                # Always use re-encoded for PNG sources; for JPEG only if smaller
                if is_png or len(reencoded) < len(data):
                    tag = "PNG→" if is_png else ""
                    print(f"  [{layer_name}] {tag}Optimised JPEG ({len(data)} → {len(reencoded)} bytes)")
                    data = reencoded
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


def list_r2_keys(r2):
    """Return a set of all existing keys in radar_gh/ and sat_gh/."""
    keys = set()
    paginator = r2.get_paginator('list_objects_v2')
    for prefix in ('radar_gh/', 'sat_gh/'):
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                keys.add(obj['Key'])
    print(f"R2: {len(keys)} existing objects found.")
    return keys


def upload_to_r2(r2, local_path, r2_key, content_type, existing_keys, force=False):
    if not force and r2_key in existing_keys:
        return True  # already in R2, skip
    try:
        r2.upload_file(local_path, R2_BUCKET, r2_key, ExtraArgs={'ContentType': content_type})
        print(f"  Uploaded to R2: {r2_key}")
        existing_keys.add(r2_key)
        return True
    except Exception as e:
        print(f"  R2 upload failed for {r2_key}: {e}")
        return False


def cleanup_r2(r2, retention_days=14):
    """Delete R2 objects in radar_gh/ and sat_gh/ older than retention_days.
    Also purges all objects in legacy sat/ and radar/ prefixes (no longer written to)."""
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime('%Y%m%d%H%M')
    deleted = 0

    # Purge current folders by retention window
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

    # Purge legacy folders entirely (sat/ and radar/ are no longer used)
    for prefix in ('sat/', 'radar/'):
        paginator = r2.get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            keys.extend(obj['Key'] for obj in page.get('Contents', []))
        for i in range(0, len(keys), 1000):
            batch = [{'Key': k} for k in keys[i:i+1000]]
            r2.delete_objects(Bucket=R2_BUCKET, Delete={'Objects': batch})
            deleted += len(batch)
            print(f"  Purged {len(batch)} legacy objects from {prefix}")

    print(f"R2 cleanup: removed {deleted} objects total.")


def main():
    os.makedirs(H5_DIR, exist_ok=True)
    os.makedirs(PNG_DIR, exist_ok=True)
    os.makedirs(SAT_DIR, exist_ok=True)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2_client() if USE_R2 else None
    r2_keys = list_r2_keys(r2) if USE_R2 else None

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

        r2_key_radar = f"radar_gh/{png_name}"
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
        if iso_time:
            sat_name_bw      = h5_name.replace(".h5", "_sat_bw.jpg")
            sat_name_vis     = h5_name.replace(".h5", "_sat_vis.jpg")
            sat_name_ir      = h5_name.replace(".h5", "_sat_ir.jpg")
            sat_name_ir_grey = h5_name.replace(".h5", "_sat_ir_grey.jpg")
            sat_path_bw      = os.path.join(SAT_DIR, sat_name_bw)
            sat_path_vis     = os.path.join(SAT_DIR, sat_name_vis)
            sat_path_ir      = os.path.join(SAT_DIR, sat_name_ir)
            sat_path_ir_grey = os.path.join(SAT_DIR, sat_name_ir_grey)
            sat_too_old      = dt < datetime.utcnow() - timedelta(hours=SAT_MAX_AGE_HOURS)

            r2_key_bw      = f"sat_gh/{sat_name_bw}"
            r2_key_vis     = f"sat_gh/{sat_name_vis}"
            r2_key_ir      = f"sat_gh/{sat_name_ir}"
            r2_key_ir_grey = f"sat_gh/{sat_name_ir_grey}"

            # GeoColour RGB (MTG FCI — seamless day/night colour blend)
            if USE_R2 and r2_key_bw in r2_keys:
                sat_url_bw = f"{R2_PUBLIC_URL}/{r2_key_bw}"
            elif not sat_too_old:
                if not os.path.exists(sat_path_bw):
                    print(f"Downloading GeoColour image {sat_name_bw}...")
                    download_sat_image(iso_time, "mtg_fd:rgb_geocolour", sat_path_bw)
                if os.path.exists(sat_path_bw):
                    if USE_R2:
                        upload_to_r2(r2, sat_path_bw, r2_key_bw, "image/jpeg", r2_keys)
                        sat_url_bw = f"{R2_PUBLIC_URL}/{r2_key_bw}"
                    else:
                        sat_url_bw = f"static/sat/{sat_name_bw}"

            # HRFI VIS0.6 (MTG FCI — 0.5km B&W visible, daytime only)
            if USE_R2 and r2_key_vis in r2_keys:
                sat_url_vis = f"{R2_PUBLIC_URL}/{r2_key_vis}"
            elif not sat_too_old:
                if not os.path.exists(sat_path_vis):
                    print(f"Downloading HRFI VIS image {sat_name_vis}...")
                    download_sat_image(iso_time, "mtg_fd:vis06_hrfi", sat_path_vis, grayscale=True)
                if os.path.exists(sat_path_vis):
                    if USE_R2:
                        upload_to_r2(r2, sat_path_vis, r2_key_vis, "image/jpeg", r2_keys)
                        sat_url_vis = f"{R2_PUBLIC_URL}/{r2_key_vis}"
                    else:
                        sat_url_vis = f"static/sat/{sat_name_vis}"

            # HRFI IR10.5 (MTG FCI — 1km thermal infrared, false colour style 01)
            if USE_R2 and r2_key_ir in r2_keys:
                sat_url_ir = f"{R2_PUBLIC_URL}/{r2_key_ir}"
            elif not sat_too_old:
                if not os.path.exists(sat_path_ir):
                    print(f"Downloading HRFI IR false colour image {sat_name_ir}...")
                    download_sat_image(iso_time, "mtg_fd:ir105_hrfi", sat_path_ir,
                                       style="mtg_fd_ir105_hrfi_style_01")
                if os.path.exists(sat_path_ir):
                    if USE_R2:
                        upload_to_r2(r2, sat_path_ir, r2_key_ir, "image/jpeg", r2_keys)
                        sat_url_ir = f"{R2_PUBLIC_URL}/{r2_key_ir}"
                    else:
                        sat_url_ir = f"static/sat/{sat_name_ir}"

            # HRFI IR10.5 (MTG FCI — 1km thermal infrared, default greyscale)
            if USE_R2 and r2_key_ir_grey in r2_keys:
                sat_url_ir_grey = f"{R2_PUBLIC_URL}/{r2_key_ir_grey}"
            elif not sat_too_old:
                if not os.path.exists(sat_path_ir_grey):
                    print(f"Downloading HRFI IR greyscale image {sat_name_ir_grey}...")
                    download_sat_image(iso_time, "mtg_fd:ir105_hrfi", sat_path_ir_grey, grayscale=True)
                if os.path.exists(sat_path_ir_grey):
                    if USE_R2:
                        upload_to_r2(r2, sat_path_ir_grey, r2_key_ir_grey, "image/jpeg", r2_keys)
                        sat_url_ir_grey = f"{R2_PUBLIC_URL}/{r2_key_ir_grey}"
                    else:
                        sat_url_ir_grey = f"static/sat/{sat_name_ir_grey}"

        radar_url = f"{R2_PUBLIC_URL}/radar_gh/{png_name}" if USE_R2 else f"static/radar/{png_name}"
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

    with open("frames.json", "w") as f:
        json.dump(frames, f, indent=2)
    print(f"Manifest updated with {len(frames)} frames.")

    status = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "frame_count": len(frames),
        "latest_frame": frames[-1]["time"] if frames else None,
    }
    with open("status.json", "w") as f:
        json.dump(status, f, indent=2)
    print(f"Status written: {status}")

    if USE_R2:
        upload_to_r2(r2, "frames.json", "frames.json", "application/json", r2_keys, force=True)
        upload_to_r2(r2, "status.json", "status.json", "application/json", r2_keys, force=True)
        cleanup_r2(r2)


if __name__ == "__main__":
    main()
