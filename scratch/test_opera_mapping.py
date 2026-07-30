import h5py
import numpy as np
from pyproj import Transformer
from PIL import Image
import os

# UK Full domain bounds (same as process_parallel.py)
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX = 43.7009, 62.9207
WIDTH, HEIGHT = 1725, 2175

def get_mapping(h5_file):
    with h5py.File(h5_file, "r") as f:
        where = f['where'].attrs
        xsize, ysize = int(where['xsize']), int(where['ysize'])
        xscale, yscale = float(where['xscale']), float(where['yscale'])
        proj_str = where['projdef']
        if isinstance(proj_str, bytes): proj_str = proj_str.decode()
        ul_lon, ul_lat = float(where['UL_lon']), float(where['UL_lat'])

    print(f"OPERA H5 Grid: xsize={xsize}, ysize={ysize}, xscale={xscale}, yscale={yscale}")
    print(f"OPERA H5 Projection: {proj_str}")
    print(f"OPERA H5 UL Corner: lon={ul_lon}, lat={ul_lat}")

    # Create coordinate transformers
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_radar = Transformer.from_crs("EPSG:3857", proj_str, always_xy=True)

    # 1. Transform our target bounds to Web Mercator meters
    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)

    # 2. Build the Mercator destination grid
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    # 3. Transform the Mercator grid back to the radar's native projected coordinate system
    X_radar, Y_radar = merc_to_radar.transform(MERC_X, MERC_Y)

    # 4. Project the UL corner to find the origin in the radar's projected system
    ll_to_radar = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
    ul_x, ul_y = ll_to_radar.transform(ul_lon, ul_lat)
    print(f"Origin UL in radar projection: x={ul_x}, y={ul_y}")

    # 5. Map radar projected coordinates to pixel columns and rows
    # Note: ODIM standard y-axis goes downwards from the top, so we subtract from ul_y
    cols = np.round((X_radar - ul_x) / xscale).astype(np.int32)
    rows = np.round((ul_y - Y_radar) / yscale).astype(np.int32)
    
    valid = (cols >= 0) & (cols < xsize) & (rows >= 0) & (rows < ysize)
    print(f"Valid pixels mapped in target grid: {np.sum(valid)} / {WIDTH * HEIGHT} ({np.sum(valid)/(WIDTH*HEIGHT)*100:.2f}%)")
    return rows, cols, valid

def test():
    h5_file = "scratch/opera_test.h5"
    rows, cols, valid = get_mapping(h5_file)
    
    # Let's render a test PNG
    with h5py.File(h5_file, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])

    # Prepare array
    out_raw = np.full((HEIGHT, WIDTH), nodata, dtype=raw.dtype)
    out_raw[valid] = raw[rows[valid], cols[valid]]
    
    # Calculate dBZ and rain rate
    valid_out = (out_raw != nodata) & (out_raw != undetect)
    dbz = out_raw * gain + offset
    
    r = np.zeros((HEIGHT, WIDTH))
    calc_mask = valid_out & (dbz > -20.0)
    z = 10 ** (dbz[calc_mask] / 10.0)
    r[calc_mask] = (z / 200.0) ** 0.625

    # Match process_parallel.py color bins
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
    
    png_path = "scratch/opera_test.png"
    Image.fromarray(rgba, 'RGBA').save(png_path)
    print(f"Saved test image to {png_path}")

if __name__ == "__main__":
    test()
