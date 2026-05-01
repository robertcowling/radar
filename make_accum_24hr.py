import os
import h5py
import numpy as np
from pyproj import Transformer
from PIL import Image
from datetime import datetime, timedelta
import json
import boto3
from botocore import UNSIGNED
from botocore.client import Config

# Radar domain — same as process_latest.py
LON_MIN, LON_MAX = -17.97, 16.13
LAT_MIN, LAT_MAX = 43.70, 63.83
WIDTH, HEIGHT = 2400, 2458

BUCKET = "met-office-radar-obs-data"
H5_DIR = "data_h5"
FRAMES_24H = 96        # 24 hours × 4 frames/hr
INTERVAL_HRS = 0.25    # 15 min in hours — multiply rate by this to get mm per frame

R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

# Accumulation thresholds (mm/24hr) and RGBA colour table
ACCUM_BOUNDS = np.array([0.5, 1, 2, 5, 10, 20, 40, 80, 160])
ACCUM_COLORS = np.array([
    [0,   0,   0,   0],    # transparent — no measurable rain
    [224, 243, 255, 255],  # 0.5–1 mm   — very pale blue
    [129, 212, 250, 255],  # 1–2 mm     — light blue
    [3,   169, 244, 255],  # 2–5 mm     — sky blue
    [1,    87, 155, 255],  # 5–10 mm    — dark blue
    [46,  125,  50, 255],  # 10–20 mm   — green
    [251, 192,  45, 255],  # 20–40 mm   — amber
    [239, 108,   0, 255],  # 40–80 mm   — orange
    [198,  40,  40, 255],  # 80–160 mm  — red
    [106,  27, 154, 255],  # 160+ mm    — purple
], dtype=np.uint8)


def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )


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


def collect_24h_keys(s3):
    """Return the latest FRAMES_24H H5 keys from S3."""
    keys = []
    date = datetime.utcnow()
    for _ in range(4):  # search up to 4 days back to find 96 frames
        day_keys = list_s3_keys_for_date(s3, date)
        keys.extend(day_keys)
        date -= timedelta(days=1)
        if len(keys) >= FRAMES_24H * 2:
            break
    keys = sorted(keys)
    if len(keys) < FRAMES_24H:
        print(f"Warning: only {len(keys)} frames available (wanted {FRAMES_24H})")
    return keys[-FRAMES_24H:]


def get_mapping(h5_sample):
    """Build pixel→radar coordinate mapping for the output overlay grid."""
    with h5py.File(h5_sample, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes):
            proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

    ll_to_merc   = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)
    ll_to_radar   = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)

    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)

    cols = np.round((X_radar - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Y_radar) / yscale).astype(np.int32)
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    return rows, cols, valid


def read_rain_rate(h5_file, mapping):
    """Return a float32 rain-rate array (mm/hr) projected onto the overlay grid.
    Pixels with no data or undetected echoes are returned as 0.0."""
    rows, cols, valid = mapping
    with h5py.File(h5_file, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])

    out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]

    rate = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    good = valid & (out_raw != nodata) & (out_raw != undetect)
    rate[good] = (out_raw[good].astype(np.float32) * gain + offset).clip(min=0)
    return rate


def render_accum_png(accum_mm, output_path):
    """Render the 24hr accumulation array to a coloured RGBA PNG."""
    indices = np.digitize(accum_mm, ACCUM_BOUNDS)
    # Pixels that never had any rain remain transparent
    indices[accum_mm == 0] = 0
    rgba = ACCUM_COLORS[indices]
    Image.fromarray(rgba, 'RGBA').save(output_path)
    print(f"Saved accumulation PNG: {output_path}")


def main():
    os.makedirs(H5_DIR, exist_ok=True)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2_client() if USE_R2 else None

    keys = collect_24h_keys(s3)
    if not keys:
        print("No H5 keys found. Aborting.")
        return

    print(f"Accumulating {len(keys)} frames ({keys[0].split('/')[-1][:12]} to {keys[-1].split('/')[-1][:12]})")

    accum = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    mapping = None
    downloaded = []

    for i, key in enumerate(keys):
        h5_name = os.path.basename(key)
        h5_path = os.path.join(H5_DIR, h5_name)
        print(f"  [{i+1}/{len(keys)}] {h5_name[:12]}", end=" ", flush=True)

        if not os.path.exists(h5_path):
            s3.download_file(BUCKET, key, h5_path)
            downloaded.append(h5_path)
            print("downloaded", end=" ", flush=True)

        if mapping is None:
            mapping = get_mapping(h5_path)

        rate = read_rain_rate(h5_path, mapping)
        accum += rate * INTERVAL_HRS
        print(f"acc_max={accum.max():.1f}mm")

    # Render and upload
    png_path = "accum_24hr.png"
    render_accum_png(accum, png_path)

    # Build metadata
    def frame_ts(key):
        base = os.path.basename(key)
        try:
            return datetime.strptime(base[:12], "%Y%m%d%H%M").strftime("%a %d %b %Y %H:%M UTC")
        except Exception:
            return base[:12]

    meta = {
        "url": f"{R2_PUBLIC_URL}/accum_24hr.png" if USE_R2 else "accum_24hr.png",
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "period_start": frame_ts(keys[0]),
        "period_end": frame_ts(keys[-1]),
        "frames_used": len(keys),
    }

    with open("accum_24hr_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Metadata written: {meta}")

    if USE_R2:
        r2.upload_file(png_path, R2_BUCKET, "accum_24hr.png",
                       ExtraArgs={'ContentType': 'image/png'})
        print("Uploaded accum_24hr.png to R2")

        r2.upload_file("accum_24hr_meta.json", R2_BUCKET, "accum_24hr_meta.json",
                       ExtraArgs={'ContentType': 'application/json'})
        print("Uploaded accum_24hr_meta.json to R2")

    # Clean up downloaded H5 files to save runner disk space
    for path in downloaded:
        try:
            os.remove(path)
        except OSError:
            pass
    if downloaded:
        print(f"Cleaned up {len(downloaded)} downloaded H5 files")


if __name__ == "__main__":
    main()
