"""
fetch_ukv.py — Download Met Office UKV 2km forecast rainfall, render PNGs, upload to R2.

Met Office S3 bucket: met-office-atmospheric-model-data (eu-west-2, public)
Run prefix: uk-deterministic-2km/YYYYMMDDTHHMMZ/
File naming: {RUNTIME}-{OFFSET}-{PARAMETER}.nc

Usage: python fetch_ukv.py
Required env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
              R2_BUCKET_NAME, R2_PUBLIC_BASE_URL
"""
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import boto3
import numpy as np
import xarray as xr
from botocore import UNSIGNED
from botocore.client import Config
from PIL import Image
from pyproj import CRS, Transformer

# ── Output domain (matches make_accum_multi.py) ──────────────────────────────────
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX =  43.7009, 62.9207
WIDTH,   HEIGHT  =  1725,    2175

# ── Met Office S3 ────────────────────────────────────────────────────────────────
MET_BUCKET = "met-office-atmospheric-model-data"
MET_REGION = "eu-west-2"
UKV_PREFIX = "uk-deterministic-2km/"

# ── Colour schemes ───────────────────────────────────────────────────────────────
_STD = np.array([
    [  0,   0,   0,   0],   # transparent (index 0 — no rain)
    [224, 243, 255, 255],
    [129, 212, 250, 255],
    [  3, 169, 244, 255],
    [  1,  87, 155, 255],
    [ 46, 125,  50, 255],
    [251, 192,  45, 255],
    [239, 108,   0, 255],
    [198,  40,  40, 255],
    [106,  27, 154, 255],
], dtype=np.uint8)

_MET = np.array([
    [  0,   0,   0,   0],   # transparent < 0.03
    [ 58, 108, 255, 255],   # blue
    [  0, 255,   0, 255],   # bright green
    [255, 255, 149, 255],   # pale yellow
    [255, 213,  99, 255],   # sand
    [255, 150,  24, 255],   # orange
    [232,  97,   0, 255],   # dark orange
    [186,  32,   0, 255],   # red
    [204,  83, 125, 255],   # rose
    [219, 146, 220, 255],   # light purple
    [255,   2, 255, 255],   # magenta
    [255, 255, 255, 255],   # white
    [200, 200, 200, 255],   # light grey
    [191, 191,   0, 255],   # olive
], dtype=np.uint8)

RATE_SCHEMES = {
    "norm": {"bounds": np.array([0.1, 0.5, 1, 2, 4, 8, 16, 32, 64]),    "colors": _STD},
    "high": {"bounds": np.array([0.5, 2,   4, 8, 16, 32, 64, 100, 150]), "colors": _STD},
}

ACCUM_SCHEMES = {
    "norm": {"bounds": np.array([0.5, 1, 2, 5, 10, 20, 40, 80, 160]),   "colors": _STD},
    "high": {"bounds": np.array([2, 5, 10, 25, 50, 100, 150, 200, 350]), "colors": _STD},
    "met":  {"bounds": np.array([0.03, 1, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180]),
             "colors": _MET},
}

# ── R2 ───────────────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])


def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def get_s3():
    return boto3.client("s3", region_name=MET_REGION,
                        config=Config(signature_version=UNSIGNED))


def json_from_r2(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default


def json_to_r2(r2, key, data):
    r2.put_object(Bucket=R2_BUCKET, Key=key,
                  Body=json.dumps(data, indent=2).encode(),
                  ContentType="application/json; charset=utf-8")


def png_to_r2(r2, key, img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r2.put_object(Bucket=R2_BUCKET, Key=key,
                  Body=buf.getvalue(), ContentType="image/png")


# ── Run discovery ─────────────────────────────────────────────────────────────────
def find_latest_run(s3):
    """Find the latest run that has T+1h data available.

    Files are named {valid_time}-{offset}-{param}.nc (not {run_time}-...).
    List run folders by date (today first) and verify the T+1h rainfall_rate
    file exists under the correct valid-time filename.
    """
    now = datetime.utcnow()
    for days_back in range(7):
        date = (now - timedelta(days=days_back)).strftime("%Y%m%d")
        resp = s3.list_objects_v2(
            Bucket=MET_BUCKET,
            Prefix=f"{UKV_PREFIX}{date}",
            Delimiter="/",
        )
        run_folders = sorted(
            [p["Prefix"].rstrip("/").split("/")[-1] for p in resp.get("CommonPrefixes", [])],
            reverse=True,
        )
        print(f"  {date}: {len(run_folders)} run folders found")
        for run_ts in run_folders:
            run_dt   = parse_run_dt(run_ts)
            valid_dt = run_dt + timedelta(hours=1)
            valid_ts = valid_dt.strftime("%Y%m%dT%H%MZ")
            prefix   = f"{UKV_PREFIX}{run_ts}/{valid_ts}-PT0001H00M-rainfall_rate.nc"
            chk = s3.list_objects_v2(Bucket=MET_BUCKET, Prefix=prefix, MaxKeys=1)
            if chk.get("Contents"):
                return run_ts
            print(f"    {run_ts}: T+1h not available yet")
    return None


def parse_run_dt(run_ts):
    for fmt in ("%Y%m%dT%H%MZ", "%Y%m%dT%HZ"):
        try:
            return datetime.strptime(run_ts, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse run timestamp: {run_ts}")


def run_label_str(run_ts):
    return parse_run_dt(run_ts).strftime("%-d %b %Y %H:%M UTC")


def run_type_str(run_ts):
    return "Medium" if parse_run_dt(run_ts).hour in (3, 15) else "Short"


# ── Step discovery ────────────────────────────────────────────────────────────────
WHOLE_HOUR_RE = re.compile(r"^PT(\d{4})H00M$")


def discover_steps(s3, run_ts):
    """Return list of (offset_hours, offset_str, valid_ts) for whole-hour forecast steps.

    Filenames: {valid_time}-{offset}-{param}.nc — valid_time is NOT the run time.
    """
    prefix = f"{UKV_PREFIX}{run_ts}/"
    steps, cont = [], None
    while True:
        kwargs = {"Bucket": MET_BUCKET, "Prefix": prefix}
        if cont:
            kwargs["ContinuationToken"] = cont
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            if not obj["Key"].endswith("-rainfall_rate.nc"):
                continue
            fname = os.path.basename(obj["Key"])
            parts = fname.split("-")
            if len(parts) < 3:
                continue
            valid_ts   = parts[0]   # e.g. "20260514T0700Z"
            offset_str = parts[1]   # e.g. "PT0001H00M"
            m = WHOLE_HOUR_RE.match(offset_str)
            if not m:
                continue
            hours = int(m.group(1))
            if hours == 0:
                continue
            steps.append((hours, offset_str, valid_ts))
        if resp.get("IsTruncated"):
            cont = resp["NextContinuationToken"]
        else:
            break

    steps.sort()
    return steps


# ── Coordinate mapping (built once per run) ──────────────────────────────────────
def build_mapping(nc_path):
    """Return (rows, cols, valid) arrays for remapping UKV grid → output Mercator grid."""
    with xr.open_dataset(nc_path, engine="netcdf4") as ds:
        # Find the grid_mapping variable (scalar variable with grid_mapping_name attr)
        gm_var = None
        for v in list(ds.data_vars) + list(ds.coords):
            if "grid_mapping_name" in ds[v].attrs:
                gm_var = v
                break
        if gm_var is None:
            raise ValueError("No grid_mapping variable found in NetCDF")

        attrs   = dict(ds[gm_var].attrs)
        gm_name = attrs.get("grid_mapping_name", "")

        if gm_name == "rotated_latitude_longitude":
            src_crs = CRS.from_cf({
                "grid_mapping_name": "rotated_latitude_longitude",
                "grid_north_pole_latitude":  float(attrs["grid_north_pole_latitude"]),
                "grid_north_pole_longitude": float(attrs["grid_north_pole_longitude"]),
            })
            src_lats = src_lons = None
            for c in ["grid_latitude", "rlat", "latitude"]:
                if c in ds.coords and ds[c].ndim == 1:
                    src_lats = ds[c].values; break
            for c in ["grid_longitude", "rlon", "longitude"]:
                if c in ds.coords and ds[c].ndim == 1:
                    src_lons = ds[c].values; break
        elif gm_name in ("transverse_mercator", "lambert_azimuthal_equal_area"):
            src_crs  = CRS.from_epsg(27700)
            src_lats = src_lons = None
            for c in ["projection_y_coordinate", "northing", "y"]:
                if c in ds.coords and ds[c].ndim == 1:
                    src_lats = ds[c].values; break
            for c in ["projection_x_coordinate", "easting", "x"]:
                if c in ds.coords and ds[c].ndim == 1:
                    src_lons = ds[c].values; break
        else:
            raise ValueError(f"Unsupported grid_mapping_name: {gm_name!r}")

    if src_lats is None or src_lons is None:
        raise ValueError("Could not identify source coordinate arrays")

    ll_to_merc  = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_src = Transformer.from_crs("EPSG:3857", src_crs,     always_xy=True)

    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)  # top → bottom
    MX, MY  = np.meshgrid(merc_xs, merc_ys)

    Xsrc, Ysrc = merc_to_src.transform(MX, MY)

    x_step = float(src_lons[1] - src_lons[0])
    y_step = float(src_lats[1] - src_lats[0])
    cols   = np.round((Xsrc - float(src_lons[0])) / x_step).astype(np.int32)
    rows   = np.round((Ysrc - float(src_lats[0])) / y_step).astype(np.int32)
    valid  = (rows >= 0) & (rows < len(src_lats)) & (cols >= 0) & (cols < len(src_lons))

    return rows, cols, valid


# ── Data extraction ───────────────────────────────────────────────────────────────
def extract_array(nc_path, mapping):
    """Extract the main data variable, convert units, remap to output grid."""
    rows, cols, valid = mapping
    with xr.open_dataset(nc_path, engine="netcdf4") as ds:
        gm_names = {v for v in list(ds.data_vars) + list(ds.coords)
                    if "grid_mapping_name" in ds[v].attrs}
        candidates = [v for v in ds.data_vars
                      if v not in gm_names and ds[v].ndim >= 2]
        if not candidates:
            return None
        da    = ds[candidates[0]]
        arr2d = da.values.squeeze()
        units = (da.attrs.get("units", "") or "").lower()

    while arr2d.ndim > 2:
        arr2d = arr2d[0]
    if arr2d.ndim != 2:
        return None

    arr2d = np.where(np.isfinite(arr2d), arr2d, 0.0).astype(np.float32)
    arr2d = np.clip(arr2d, 0, None)

    # kg m-2 s-1 → mm hr-1
    if "kg" in units and ("s-1" in units or "s**-1" in units):
        arr2d = arr2d * 3600.0

    out = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    out[valid] = arr2d[rows[valid], cols[valid]]
    return out


def render_png(arr, scheme):
    indices = np.digitize(arr, scheme["bounds"])
    indices[arr <= 0] = 0
    return Image.fromarray(scheme["colors"][indices], "RGBA")


# ── S3 download → temp file ───────────────────────────────────────────────────────
def download_nc(s3, run_ts, valid_ts, offset, param):
    """Key format: {prefix}{run_ts}/{valid_ts}-{offset}-{param}.nc"""
    key = f"{UKV_PREFIX}{run_ts}/{valid_ts}-{offset}-{param}.nc"
    try:
        obj = s3.get_object(Bucket=MET_BUCKET, Key=key)
        return obj["Body"].read()
    except Exception as e:
        print(f"    Warning: {os.path.basename(key)} — {e}")
        return None


def load_arr(s3, run_ts, valid_ts, offset, param, mapping):
    """Download NC, write temp file, extract array. Returns None on failure."""
    nc_bytes = download_nc(s3, run_ts, valid_ts, offset, param)
    if nc_bytes is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
        tmp.write(nc_bytes)
        path = tmp.name
    try:
        arr = extract_array(path, mapping)
        if arr is not None:
            print(f"      max={arr.max():.4f}  nonzero={np.count_nonzero(arr)}")
        return arr
    finally:
        os.unlink(path)


def upload_schemes(r2, arr, schemes, run_ts, offset, prefix):
    """Render arr with each scheme, upload PNGs, return {scheme_name: url}."""
    if arr is None:
        return None
    urls = {}
    for sname, scheme in schemes.items():
        img = render_png(arr, scheme)
        key = f"ukv/{run_ts}/{offset}_{prefix}_{sname}.png"
        png_to_r2(r2, key, img)
        urls[sname] = f"{R2_PUBLIC_URL}/{key}"
    return urls


# ── Valid time label ──────────────────────────────────────────────────────────────
def valid_label_str(valid_ts):
    return parse_run_dt(valid_ts).strftime("%-d %b %Y %H:%M UTC")


# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    if not USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    s3 = get_s3()
    r2 = get_r2()

    print("Finding latest UKV run...")
    run_ts = find_latest_run(s3)
    if not run_ts:
        print("No runs found on S3.")
        sys.exit(0)
    print(f"  Latest: {run_ts}")

    existing_meta = json_from_r2(r2, "ukv_meta.json", {"runs": []})
    existing_runs = existing_meta.get("runs", [])
    if existing_runs and existing_runs[0].get("run_ts") == run_ts:
        print(f"  {run_ts} already processed — done.")
        sys.exit(0)

    print("Discovering forecast steps...")
    steps = discover_steps(s3, run_ts)
    if not steps:
        print("  No whole-hour steps found.")
        sys.exit(1)
    print(f"  {len(steps)} steps: T+{steps[0][0]}h → T+{steps[-1][0]}h")

    print("Building coordinate mapping from T+1h file...")
    first_hours, first_offset, first_valid = steps[0]
    nc_bytes = download_nc(s3, run_ts, first_valid, first_offset, "rainfall_rate")
    if nc_bytes is None:
        print("  Cannot build mapping — aborting.")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
        tmp.write(nc_bytes)
        sample_path = tmp.name
    try:
        mapping = build_mapping(sample_path)
    finally:
        os.unlink(sample_path)
    print("  Mapping ready.")

    rtype  = run_type_str(run_ts)
    rlabel = run_label_str(run_ts)
    print(f"  Run: {rlabel} ({rtype})")

    step_entries  = []
    running_total = np.zeros((HEIGHT, WIDTH), dtype=np.float32)

    for i, (hours, offset, valid_ts) in enumerate(steps):
        vlabel = valid_label_str(valid_ts)
        print(f"  [{i+1}/{len(steps)}] {offset}  {vlabel}", flush=True)

        entry = {"offset": offset, "offset_hours": hours, "valid_label": vlabel}

        # Rainfall rate
        arr  = load_arr(s3, run_ts, valid_ts, offset, "rainfall_rate", mapping)
        urls = upload_schemes(r2, arr, RATE_SCHEMES, run_ts, offset, "rainfall_rate")
        if urls:
            entry["rainfall_rate"] = urls

        # 1-hour accumulation + running total
        arr = load_arr(s3, run_ts, valid_ts, offset, "rainfall_accumulation-PT01H", mapping)
        if arr is not None:
            running_total += arr
            urls = upload_schemes(r2, arr, ACCUM_SCHEMES, run_ts, offset, "rainfall_accum")
            if urls:
                entry["rainfall_accum"] = urls
            total_urls = upload_schemes(r2, running_total.copy(), ACCUM_SCHEMES,
                                        run_ts, offset, "rainfall_total")
            if total_urls:
                entry["rainfall_total"] = total_urls

        # Snowfall rate
        arr = load_arr(s3, run_ts, valid_ts, offset, "snowfall_rate", mapping)
        if arr is not None:
            urls = upload_schemes(r2, arr, {"norm": RATE_SCHEMES["norm"]},
                                  run_ts, offset, "snowfall_rate")
            if urls:
                entry["snowfall_rate"] = urls

        step_entries.append(entry)

    # Prune runs older than 48 h from meta (PNGs left in R2 — cheap)
    cutoff = datetime.utcnow() - timedelta(hours=48)
    kept_runs = [r for r in existing_runs if parse_run_dt(r["run_ts"]) >= cutoff]

    new_run = {
        "run_ts":         run_ts,
        "run_label":      rlabel,
        "run_type":       rtype,
        "forecast_hours": steps[-1][0],
        "steps":          step_entries,
    }

    meta = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "runs": [new_run] + kept_runs,
    }

    with open("ukv_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    json_to_r2(r2, "ukv_meta.json", meta)
    print(f"Done: {len(meta['runs'])} run(s) in meta, {len(step_entries)} steps this run.")


if __name__ == "__main__":
    main()
