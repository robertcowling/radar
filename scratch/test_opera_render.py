import h5py
import numpy as np

def test():
    h5_path = "scratch/opera_test.h5"
    with h5py.File(h5_path, "r") as f:
        raw = f['dataset1/data1/data'][:]
        dwhat = f['dataset1/data1/what'].attrs
        gain, offset = float(dwhat['gain']), float(dwhat['offset'])
        nodata, undetect = float(dwhat['nodata']), float(dwhat['undetect'])
        
        print("Raw min/max:", raw.min(), raw.max())
        
        # In ODIM, raw values are packed.
        # unpacked dBZ = raw * gain + offset
        valid_mask = (raw != nodata) & (raw != undetect)
        dbz = np.full(raw.shape, np.nan)
        dbz[valid_mask] = raw[valid_mask] * gain + offset
        
        print("Unpacked dBZ min/max:", np.nanmin(dbz), np.nanmax(dbz))
        
        # Marshall-Palmer: Z = 200 * R^1.6
        # R = (Z / 200) ^ (1 / 1.6)
        # Z = 10 ^ (dBZ / 10)
        # R = (10 ^ (dBZ / 10) / 200) ^ 0.625
        # Let's compute R
        r = np.full(raw.shape, 0.0)
        
        # To avoid overflow, only compute for valid dBZ values.
        # Let's say dBZ between -32 and 95
        calc_mask = valid_mask & (dbz > -20)
        
        z = 10 ** (dbz[calc_mask] / 10.0)
        r[calc_mask] = (z / 200.0) ** 0.625
        
        print("Rain rate (mm/hr) min/max:", r.min(), r.max())
        print("Number of pixels with rain (>0.1 mm/hr):", np.sum(r > 0.1))
        print("Number of pixels with rain (>1.0 mm/hr):", np.sum(r > 1.0))
        print("Number of pixels with rain (>8.0 mm/hr):", np.sum(r > 8.0))

if __name__ == "__main__":
    test()
