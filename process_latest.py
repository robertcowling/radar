import os
from pyproj import Transformer
from PIL import Image
from datetime import datetime, timedelta
import json
import boto3
from botocore import UNSIGNED
from botocore.client import Config
import urllib.request
import io

# Satellite domain — same centre (-4.0°, 55.25°); 2× taller than before
SAT_LON_MIN, SAT_LON_MAX = -40.4, 32.4
SAT_LAT_MIN, SAT_LAT_MAX = 37.75, 67.5

# UK hi-res domain — a tight box over the British Isles. Same 15-min cadence
# and 3-day retention as the wide frame, but the pixels cover ~1/30th the area,
# so effective ground resolution is ~2.4-3.4x finer (~420 m/px over the UK vs
# ~1000-1450 m/px on the wide frame). Width matches the box's Web Mercator
# aspect so pixels stay isotropic. Stacked over the wide GeoColour in the viewer.
UK_LON_MIN, UK_LON_MAX = -11.0, 2.0
UK_LAT_MIN, UK_LAT_MAX = 49.0, 61.0
UK_SAT_WIDTH, UK_SAT_HEIGHT = 1970, 3200

# S3 Config
BUCKET = "met-office-radar-obs-data"
NUM_FILES = 192  # 2 days × 96 frames/day (15-min intervals)
SAT_MAX_AGE_HOURS = 24  # give up retrying sat downloads after this long

# R2 Config — set these as GitHub Actions secrets (never hardcode)
R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

if USE_R2:
    SAT_DIR = "static/sat_gh"
else:
    SAT_DIR = "static/sat"


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


def collect_keys(s3):
    """Collect the N most recent H5 keys from Met Office S3 (for timestamp enumeration)."""
    keys = []
    date = datetime.utcnow()
    for _ in range(7):
        day_keys = list_s3_keys_for_date(s3, date)
        keys.extend(day_keys)
        date -= timedelta(days=1)
        if len(keys) >= NUM_FILES * 2:
            break
    return keys


def download_sat_image(iso_time, layer_name, sat_path, fmt="image/jpeg", width=3200, height=3200, style="", grayscale=False,
                       lon_min=None, lon_max=None, lat_min=None, lat_max=None):
    # Bounding box defaults to the wide domain; pass lon/lat to request a tighter
    # (e.g. UK hi-res) frame at the same pixel dimensions.
    lon_min = SAT_LON_MIN if lon_min is None else lon_min
    lon_max = SAT_LON_MAX if lon_max is None else lon_max
    lat_min = SAT_LAT_MIN if lat_min is None else lat_min
    lat_max = SAT_LAT_MAX if lat_max is None else lat_max
    # Convert satellite lat/lon bounds to Web Mercator (EPSG:3857) meters.
    # We must request imagery in 3857 to match the Leaflet/OSM projection perfectly.
    # WMS 1.3.0 with EPSG:4326 would return a Plate Carree image that appears shifted
    # north when stretched linearly on a Mercator map.
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    m_xmin, m_ymin = ll_to_merc.transform(lon_min, lat_min)
    m_xmax, m_ymax = ll_to_merc.transform(lon_max, lat_max)

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
            is_png = data[:4] == b'\x89PNG'
            if is_png or grayscale:
                mode = "L" if grayscale else "RGB"
                img = Image.open(io.BytesIO(data)).convert(mode)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75, optimize=True)
                reencoded = buf.getvalue()
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
    """Return a set of all existing keys under sat_gh (covers both the wide
    sat_gh/ frames and the tight sat_gh_uk/ hi-res frames in one LIST, so the
    UK layer costs no extra Class A operations for dedup or retention)."""
    keys = set()
    paginator = r2.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='sat_gh'):
        for obj in page.get('Contents', []):
            keys.add(obj['Key'])
    print(f"R2: {len(keys)} existing sat objects found.")
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


def cleanup_r2(r2, existing_keys, retention_days=3):
    """Delete R2 objects in sat_gh/ older than retention_days.

    Works from the sat_gh/ listing already fetched at startup instead of
    re-listing the prefix — LIST is Class A (the scarce free-tier budget)
    while DeleteObject is free. Legacy sat/, radar/ and radar_gh/ prefixes
    were purged long ago; scanning them every run cost 3 LISTs for nothing.
    """
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime('%Y%m%d%H%M')
    deleted = 0

    for key in sorted(existing_keys):
        ts = os.path.basename(key)[:12]
        if ts.isdigit() and ts < cutoff_str:
            r2.delete_object(Bucket=R2_BUCKET, Key=key)
            print(f"  Deleted from R2: {key}")
            deleted += 1

    print(f"R2 cleanup: removed {deleted} objects total.")


def main():
    os.makedirs(SAT_DIR, exist_ok=True)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2_client() if USE_R2 else None
    r2_keys = list_r2_keys(r2) if USE_R2 else None

    keys = collect_keys(s3)

    # Take latest NUM_FILES, sorted chronologically
    latest_keys = sorted(keys)[-NUM_FILES:]

    for key in latest_keys:
        h5_name = os.path.basename(key)

        date_str = h5_name.split('_')[0]
        try:
            dt = datetime.strptime(date_str, "%Y%m%d%H%M")
            iso_time = dt.strftime("%Y-%m-%dT%H:%M:00.000Z")
        except Exception:
            continue

        sat_too_old = dt < datetime.utcnow() - timedelta(hours=SAT_MAX_AGE_HOURS)
        if sat_too_old:
            continue

        sat_name_bw      = h5_name.replace(".h5", "_sat_bw.jpg")
        sat_name_vis     = h5_name.replace(".h5", "_sat_vis.jpg")
        sat_name_ir      = h5_name.replace(".h5", "_sat_ir.jpg")
        sat_name_ir_grey = h5_name.replace(".h5", "_sat_ir_grey.jpg")
        sat_path_bw      = os.path.join(SAT_DIR, sat_name_bw)
        sat_path_vis     = os.path.join(SAT_DIR, sat_name_vis)
        sat_path_ir      = os.path.join(SAT_DIR, sat_name_ir)
        sat_path_ir_grey = os.path.join(SAT_DIR, sat_name_ir_grey)

        r2_key_bw      = f"sat_gh/{sat_name_bw}"
        r2_key_vis     = f"sat_gh/{sat_name_vis}"
        r2_key_ir      = f"sat_gh/{sat_name_ir}"
        r2_key_ir_grey = f"sat_gh/{sat_name_ir_grey}"

        # GeoColour RGB (MTG FCI — seamless day/night colour blend)
        if not (USE_R2 and r2_key_bw in r2_keys):
            if not os.path.exists(sat_path_bw):
                print(f"Downloading GeoColour image {sat_name_bw}...")
                download_sat_image(iso_time, "mtg_fd:rgb_geocolour", sat_path_bw)
            if os.path.exists(sat_path_bw) and USE_R2:
                upload_to_r2(r2, sat_path_bw, r2_key_bw, "image/jpeg", r2_keys)

        # GeoColour RGB — UK hi-res tight domain (stacked over the wide frame in the viewer)
        sat_name_geo_uk = h5_name.replace(".h5", "_sat_geo_uk.jpg")
        sat_path_geo_uk = os.path.join(SAT_DIR, sat_name_geo_uk)
        r2_key_geo_uk   = f"sat_gh_uk/{sat_name_geo_uk}"
        if not (USE_R2 and r2_key_geo_uk in r2_keys):
            if not os.path.exists(sat_path_geo_uk):
                print(f"Downloading UK hi-res GeoColour image {sat_name_geo_uk}...")
                download_sat_image(iso_time, "mtg_fd:rgb_geocolour", sat_path_geo_uk,
                                   width=UK_SAT_WIDTH, height=UK_SAT_HEIGHT,
                                   lon_min=UK_LON_MIN, lon_max=UK_LON_MAX,
                                   lat_min=UK_LAT_MIN, lat_max=UK_LAT_MAX)
            if os.path.exists(sat_path_geo_uk) and USE_R2:
                upload_to_r2(r2, sat_path_geo_uk, r2_key_geo_uk, "image/jpeg", r2_keys)

        # HRFI VIS0.6 (MTG FCI — 0.5km B&W visible, daytime only)
        if not (USE_R2 and r2_key_vis in r2_keys):
            if not os.path.exists(sat_path_vis):
                print(f"Downloading HRFI VIS image {sat_name_vis}...")
                download_sat_image(iso_time, "mtg_fd:vis06_hrfi", sat_path_vis, grayscale=True)
            if os.path.exists(sat_path_vis) and USE_R2:
                upload_to_r2(r2, sat_path_vis, r2_key_vis, "image/jpeg", r2_keys)

        # HRFI IR10.5 (MTG FCI — 1km thermal infrared, false colour style 01)
        if not (USE_R2 and r2_key_ir in r2_keys):
            if not os.path.exists(sat_path_ir):
                print(f"Downloading HRFI IR false colour image {sat_name_ir}...")
                download_sat_image(iso_time, "mtg_fd:ir105_hrfi", sat_path_ir,
                                   style="mtg_fd_ir105_hrfi_style_01")
            if os.path.exists(sat_path_ir) and USE_R2:
                upload_to_r2(r2, sat_path_ir, r2_key_ir, "image/jpeg", r2_keys)

        # HRFI IR10.5 (MTG FCI — 1km thermal infrared, default greyscale)
        if not (USE_R2 and r2_key_ir_grey in r2_keys):
            if not os.path.exists(sat_path_ir_grey):
                print(f"Downloading HRFI IR greyscale image {sat_name_ir_grey}...")
                download_sat_image(iso_time, "mtg_fd:ir105_hrfi", sat_path_ir_grey, grayscale=True)
            if os.path.exists(sat_path_ir_grey) and USE_R2:
                upload_to_r2(r2, sat_path_ir_grey, r2_key_ir_grey, "image/jpeg", r2_keys)

    status = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "frame_count": len(latest_keys),
        "latest_frame": os.path.basename(latest_keys[-1]) if latest_keys else None,
    }
    with open("status.json", "w") as f:
        json.dump(status, f, indent=2)
    print(f"Status written: {status}")

    if USE_R2:
        upload_to_r2(r2, "status.json", "status.json", "application/json", r2_keys, force=True)
        cleanup_r2(r2, r2_keys)


if __name__ == "__main__":
    main()
