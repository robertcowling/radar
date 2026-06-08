import os
import json
import boto3
import h5py
import numpy as np
import urllib.request
import io
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore import UNSIGNED
from botocore.client import Config
from pyproj import Transformer
from PIL import Image

# Radar domain (native H5 extent) - Standard UK bounds (same as Claudia)
LON_MIN, LON_MAX = -11.5, 3.5
LAT_MIN, LAT_MAX = 49.0, 61.5
WIDTH, HEIGHT = 2400, 2000

# Satellite domain - Matching Claudia's satBounds [[37.75, -32.0], [72.75, 24.0]]
SAT_LON_MIN, SAT_LON_MAX = -32.0, 24.0
SAT_LAT_MIN, SAT_LAT_MAX = 37.75, 72.75

BUCKET = "met-office-radar-obs-data"

# Setup directories
BASE_DIR = "bram"
H5_CACHE_DIR = "data_h5" # Cache H5s in the root data_h5 folder to avoid re-downloading if already present
RADAR_OUT_DIR = os.path.join(BASE_DIR, "static", "radar")
SAT_OUT_DIR = os.path.join(BASE_DIR, "static", "sat")

os.makedirs(RADAR_OUT_DIR, exist_ok=True)
os.makedirs(SAT_OUT_DIR, exist_ok=True)

# Storm Bram boundaries
STORM_START = datetime(2025, 12, 8, 12, 0)
STORM_END = datetime(2025, 12, 10, 18, 0)

# Setup transformers
ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
m_xmin, m_ymin = ll_to_merc.transform(SAT_LON_MIN, SAT_LAT_MIN)
m_xmax, m_ymax = ll_to_merc.transform(SAT_LON_MAX, SAT_LAT_MAX)

def get_mapping(h5_sample):
    with h5py.File(h5_sample, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes): proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

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

def download_sat_image(iso_time, layer_name, sat_path, style="", grayscale=False):
    wms_url = (f"https://view.eumetsat.int/geoserver/ows?service=WMS&request=GetMap&version=1.3.0"
               f"&layers={layer_name}&styles={style}&format=image/jpeg&crs=EPSG:3857"
               f"&bbox={m_xmin},{m_ymin},{m_xmax},{m_ymax}&width=1600&height=1600&time={iso_time}")
    try:
        req = urllib.request.Request(wms_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read()
            content_type = response.headers.get('Content-Type', '')
            if 'xml' in content_type or len(data) < 5000:
                return False
            
            # Re-encode to optimize size
            is_png = data[:4] == b'\x89PNG'
            if is_png or grayscale:
                mode = "L" if grayscale else "RGB"
                img = Image.open(io.BytesIO(data)).convert(mode)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75, optimize=True)
                data = buf.getvalue()
                
            with open(sat_path, 'wb') as out_file:
                out_file.write(data)
        return True
    except Exception as e:
        print(f"Failed sat WMS download for {iso_time} ({layer_name}): {e}")
        return False

def list_s3_keys(s3, date):
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

def process_frame(s3_key, mapping):
    h5_name = os.path.basename(s3_key)
    date_str = h5_name.split('_')[0]
    
    dt = datetime.strptime(date_str, "%Y%m%d%H%M")
    iso_time = dt.strftime("%Y-%m-%dT%H:%M:00.000Z")
    timestamp = dt.strftime("%a %d %b %Y %H:%M UTC")
    
    png_name = h5_name.replace(".h5", ".png")
    png_path = os.path.join(RADAR_OUT_DIR, png_name)
    h5_path = os.path.join(H5_CACHE_DIR, h5_name)
    
    # 1. Process Radar H5
    try:
        if not os.path.exists(png_path):
            if not os.path.exists(h5_path):
                s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
                s3.download_file(BUCKET, s3_key, h5_path)
            render_png(h5_path, png_path, mapping)
    except Exception as e:
        print(f"Failed processing radar for {h5_name}: {e}")
        return None

    # 2. Download Sat Images
    sat_name_bw = h5_name.replace(".h5", "_sat_bw.jpg")
    sat_name_vis = h5_name.replace(".h5", "_sat_vis.jpg")
    sat_name_ir = h5_name.replace(".h5", "_sat_ir.jpg")
    
    sat_path_bw = os.path.join(SAT_OUT_DIR, sat_name_bw)
    sat_path_vis = os.path.join(SAT_OUT_DIR, sat_name_vis)
    sat_path_ir = os.path.join(SAT_OUT_DIR, sat_name_ir)
    
    # GeoColour
    if not os.path.exists(sat_path_bw):
        download_sat_image(iso_time, "mtg_fd:rgb_geocolour", sat_path_bw)
    # B&W Visible
    if not os.path.exists(sat_path_vis):
        download_sat_image(iso_time, "mtg_fd:vis06_hrfi", sat_path_vis, grayscale=True)
    # IR
    if not os.path.exists(sat_path_ir):
        download_sat_image(iso_time, "mtg_fd:ir105_hrfi", sat_path_ir, style="mtg_fd_ir105_hrfi_style_01")
        
    frame_data = {
        "time": timestamp,
        "url": f"static/radar/{png_name}"
    }
    if os.path.exists(sat_path_bw):
        frame_data["sat_url_bw"] = f"static/sat/{sat_name_bw}"
    if os.path.exists(sat_path_vis):
        frame_data["sat_url_vis"] = f"static/sat/{sat_name_vis}"
    if os.path.exists(sat_path_ir):
        frame_data["sat_url_ir"] = f"static/sat/{sat_name_ir}"
        
    return frame_data

def main():
    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    
    print("Collecting S3 keys...")
    all_keys = []
    current_date = STORM_START.date()
    while current_date <= STORM_END.date():
        all_keys.extend(list_s3_keys(s3, current_date))
        current_date += timedelta(days=1)
        
    print(f"Total keys listed: {len(all_keys)}")
    
    # Filter within STORM_START and STORM_END
    storm_keys = []
    for key in all_keys:
        h5_name = os.path.basename(key)
        date_str = h5_name.split('_')[0]
        dt = datetime.strptime(date_str, "%Y%m%d%H%M")
        if STORM_START <= dt <= STORM_END:
            storm_keys.append(key)
            
    storm_keys = sorted(storm_keys)
    print(f"Keys within storm period: {len(storm_keys)}")
    
    if not storm_keys:
        print("No keys found in storm range.")
        return
        
    # Get mapping from a sample file
    sample_key = storm_keys[0]
    sample_name = os.path.basename(sample_key)
    sample_path = os.path.join(H5_CACHE_DIR, sample_name)
    if not os.path.exists(sample_path):
        print("Downloading sample H5 for mapping...")
        s3.download_file(BUCKET, sample_key, sample_path)
        
    mapping = get_mapping(sample_path)
    
    print("Processing frames in parallel...")
    frames = []
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process_frame, key, mapping): key for key in storm_keys}
        
        processed = 0
        for future in as_completed(futures):
            res = future.result()
            if res:
                frames.append(res)
            processed += 1
            if processed % 10 == 0:
                print(f"Processed {processed}/{len(storm_keys)} frames...")
                
    # Sort frames chronologically by parsing the time strings
    frames = sorted(frames, key=lambda f: datetime.strptime(f['time'], "%a %d %b %Y %H:%M UTC"))
    
    manifest_path = os.path.join(BASE_DIR, "frames.json")
    with open(manifest_path, "w") as f:
        json.dump(frames, f, indent=2)
        
    print(f"Storm Bram backfill complete! {len(frames)} frames written to {manifest_path}")

if __name__ == "__main__":
    main()
