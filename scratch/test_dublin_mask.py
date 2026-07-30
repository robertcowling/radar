import h5py
import numpy as np
from pyproj import Transformer
from PIL import Image
import os

LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX = 43.7009, 62.9207
WIDTH, HEIGHT = 1725, 2175

# Dublin Radar coordinates
DUBLIN_LAT = 53.4285
DUBLIN_LON = -6.2408
EXCLUDE_RADIUS_KM = 150.0  # Surgically mask out Dublin radar's main coverage area

def haversine_np(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat/2.0)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2.0)**2
    c = 2 * np.arcsin(np.sqrt(a))
    km = 6371.0 * c
    return km

def test():
    h5_file = "scratch/opera_test.h5"
    
    # 1. Initialize Transformers
    with h5py.File(h5_file, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes): proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)
    merc_to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    # 2. Build Mercator Grid
    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    # 3. Calculate Lat/Lon for each pixel in our target Mercator grid
    MERC_LON, MERC_LAT = merc_to_ll.transform(MERC_X, MERC_Y)
    
    # 4. Compute distance to Dublin radar for each pixel
    dist_to_dublin = haversine_np(MERC_LON, MERC_LAT, DUBLIN_LON, DUBLIN_LAT)
    
    # Create the exclusion mask
    dublin_mask = dist_to_dublin < EXCLUDE_RADIUS_KM
    print(f"Pixels in Dublin exclusion zone: {np.sum(dublin_mask)} / {WIDTH * HEIGHT} ({np.sum(dublin_mask)/(WIDTH*HEIGHT)*100:.2f}%)")

    # 5. Perform standard mapping
    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)
    ll_to_radar = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)

    cols = np.round((X_radar - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Y_radar) / yscale).astype(np.int32)
    
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    
    # 6. APPLY DUBLIN EXCLUSION MASK
    # We mark pixels inside Dublin's radius as invalid (effectively transparent/nodata)
    valid[dublin_mask] = False
    
    print(f"Valid pixels after Dublin mask: {np.sum(valid)}")

    # 7. Render test PNG
    with h5py.File(h5_file, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])

    out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]
    
    valid_out = (out_raw != nodata) & (out_raw != undetect)
    dbz = out_raw * gain + offset
    
    r = np.zeros((HEIGHT, WIDTH))
    calc_mask = valid_out & (dbz > -20.0)
    z = 10 ** (dbz[calc_mask] / 10.0)
    r[calc_mask] = (z / 200.0) ** 0.625

    bounds = np.array([0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64])
    colors = np.array([
        [0, 0, 0, 0], [225, 245, 254, 255], [129, 212, 250, 255],
        [3, 169, 244, 255], [1, 87, 155, 255], [46, 125, 50, 255],
        [251, 192, 45, 255], [239, 108, 0, 255], [198, 40, 40, 255],
        [106, 27, 154, 255], [233, 30, 99, 255]
    ], dtype=np.uint8)

    indices = np.digitize(r, bounds)
    indices[~valid_out] = 0
    rgba = colors[indices]
    
    png_path = "scratch/opera_test_masked.png"
    Image.fromarray(rgba, 'RGBA').save(png_path)
    print(f"Saved masked test image to {png_path}")

if __name__ == "__main__":
    test()
