#!/usr/bin/env python3
"""
Fetch GFS MSLP from NOAA NOMADS, render transparent PNG isobar overlays,
upload to R2 at mslp/, and write mslp_meta.json.

GFS runs at 00Z, 06Z, 12Z, 18Z with hourly output steps 0-120h.
For each hourly valid time in the last 48 hours we pick the GFS run with
the shortest lead time, download just a small regional GRIB2 slice via the
NOMADS geographic filter (~50-100 KB each), render isobars on a transparent
canvas (Web Mercator, matching satBounds in index.html), and upload to R2.

Dependencies (install in CI with apt-get + pip — not in requirements.txt):
    sudo apt-get install -y libeccodes-dev
    pip install eccodes cfgrib xarray scipy matplotlib pyproj boto3
"""

import os
import json
import datetime
from datetime import timezone
from pathlib import Path

import numpy as np
import boto3

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from scipy.interpolate import RegularGridInterpolator
from pyproj import Transformer

# ---- Geographic bounds (same as satellite domain in index.html) ----
SAT_LON_MIN, SAT_LON_MAX = -32.0, 24.0
SAT_LAT_MIN, SAT_LAT_MAX = 37.75, 72.75

# Pre-compute Mercator corners once
_ll2m = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_m2ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
MERC_XMIN, MERC_YMIN = _ll2m.transform(SAT_LON_MIN, SAT_LAT_MIN)
MERC_XMAX, MERC_YMAX = _ll2m.transform(SAT_LON_MAX, SAT_LAT_MAX)

# Output image size in pixels (width × height in Mercator aspect ratio)
# 3600px wide at DPI=200 → lines rendered at ~1.5px width (clean, above the
# sub-pixel antialiasing threshold) while output pixel count stays the same.
OUT_WIDTH = 3600
OUT_HEIGHT = int(round(OUT_WIDTH * (MERC_YMAX - MERC_YMIN) / (MERC_XMAX - MERC_XMIN)))
DPI = 200

# ---- Storage paths ----
GRIB_CACHE_DIR = "data_mslp"
PNG_DIR        = "static/mslp"
R2_PREFIX      = "mslp"
META_FILE      = "mslp_meta.json"

# ---- R2 config (GitHub Actions secrets) ----
R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')
R2_PUBLIC_URL    = os.environ.get('R2_PUBLIC_BASE_URL', '').rstrip('/')

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

HOURS_BACK    = 48          # cover last 48 hours (hourly)
GFS_RUN_HOURS = [0, 6, 12, 18]  # GFS runs per day


# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------

def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )


def list_r2_mslp_keys(r2):
    keys = set()
    paginator = r2.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"{R2_PREFIX}/"):
        for obj in page.get('Contents', []):
            keys.add(obj['Key'])
    print(f"R2 mslp/: {len(keys)} existing objects")
    return keys


def upload_to_r2(r2, local_path, r2_key, existing_keys, content_type='image/png', force=False):
    if not force and r2_key in existing_keys:
        return True
    try:
        r2.upload_file(local_path, R2_BUCKET, r2_key, ExtraArgs={'ContentType': content_type})
        print(f"  Uploaded: {r2_key}")
        existing_keys.add(r2_key)
        return True
    except Exception as e:
        print(f"  R2 upload failed for {r2_key}: {e}")
        return False


def cleanup_r2_mslp(r2, retention_days=14):
    cutoff_str = (datetime.datetime.now(timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=retention_days)).strftime('%Y%m%d%H%M')
    paginator = r2.get_paginator('list_objects_v2')
    deleted = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"{R2_PREFIX}/"):
        for obj in page.get('Contents', []):
            key = obj['Key']
            ts = os.path.basename(key)[:12]
            if ts.isdigit() and ts < cutoff_str:
                r2.delete_object(Bucket=R2_BUCKET, Key=key)
                print(f"  Deleted old MSLP: {key}")
                deleted += 1
    if deleted:
        print(f"R2 MSLP cleanup: {deleted} deleted")


# ---------------------------------------------------------------------------
# GFS time logic
# ---------------------------------------------------------------------------

def get_valid_times():
    """Return sorted list of hourly UTC datetimes for the last HOURS_BACK hours."""
    now = datetime.datetime.now(timezone.utc).replace(tzinfo=None)
    base = now.replace(minute=0, second=0, microsecond=0)
    return sorted(base - datetime.timedelta(hours=i) for i in range(HOURS_BACK + 1))


def candidates_for_valid_time(vt):
    """
    Return all (run_dt, step_hours) pairs from GFS that cover valid_time vt,
    sorted by preference: shortest step first, most recent run first.
    GFS runs at 00Z, 06Z, 12Z, 18Z with hourly steps 0-120h.
    """
    candidates = []
    for days_back in range(4):
        d = vt.date() - datetime.timedelta(days=days_back)
        base_dt = datetime.datetime(d.year, d.month, d.day)
        for run_h in reversed(GFS_RUN_HOURS):
            run_dt = base_dt + datetime.timedelta(hours=run_h)
            if run_dt > vt:
                continue
            step = int((vt - run_dt).total_seconds() / 3600)
            if step > 120:
                continue
            candidates.append((run_dt, step))
    candidates.sort(key=lambda x: (x[1], -x[0].timestamp()))
    return candidates


# ---------------------------------------------------------------------------
# GRIB download — NOAA GFS via NOMADS geographic filter
# ---------------------------------------------------------------------------

def download_mslp_grib(run_dt, step, out_path):
    """
    Download PRMSL for our region from GFS via the NOMADS filter service.
    Returns a small regional GRIB2 (~50-150 KB) rather than a global file.
    Returns True on success.
    """
    import urllib.request

    date_str = run_dt.strftime('%Y%m%d')
    rh = run_dt.hour

    # Add 5° margin around the satellite domain for smooth edge contouring
    w = SAT_LON_MIN - 5
    e = SAT_LON_MAX + 5
    s = SAT_LAT_MIN - 5
    n = SAT_LAT_MAX + 5

    url = (
        f"https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
        f"?dir=%2Fgfs.{date_str}%2F{rh:02d}%2Fatmos"
        f"&file=gfs.t{rh:02d}z.pgrb2.0p25.f{step:03d}"
        f"&var_PRMSL=on&lev_mean_sea_level=on"
        f"&subregion=&leftlon={w:.1f}&rightlon={e:.1f}"
        f"&toplat={n:.1f}&bottomlat={s:.1f}"
    )

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'radar-mslp/1.0'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()

        if len(data) < 100 or data[:4] != b'GRIB':
            snippet = data[:120].decode('utf-8', errors='replace').strip()
            print(f"    Not GRIB ({len(data)} bytes): {snippet[:80]}")
            return False

        out_path.write_bytes(data)
        print(f"  NOMADS: {out_path.name} ({len(data):,} bytes)")
        return True

    except Exception as e:
        print(f"  NOMADS download failed (run={run_dt} step={step}): {e}")
        return False


def get_grib(vt):
    """
    Ensure a GRIB file exists for valid_time vt.
    Tries candidates in preference order. Returns Path or None.
    """
    for run_dt, step in candidates_for_valid_time(vt):
        grib_name = f"gfs_{run_dt.strftime('%Y%m%d')}_{run_dt.hour:02d}z_f{step:03d}.grib2"
        grib_path = Path(GRIB_CACHE_DIR) / grib_name
        if grib_path.exists() and grib_path.stat().st_size > 100:
            return grib_path
        print(f"  Trying GFS {run_dt.strftime('%Y-%m-%d %HZ')} f{step:03d} ...")
        if download_mslp_grib(run_dt, step, grib_path):
            return grib_path
    return None


# ---------------------------------------------------------------------------
# GRIB reading
# ---------------------------------------------------------------------------

def read_mslp_from_grib(grib_path):
    """
    Read PRMSL from a GFS regional GRIB2 file (from NOMADS).
    Returns (lat_1d, lon_1d, msl_2d) with ascending coords, lons in -180..180.
    """
    import xarray as xr
    ds = xr.open_dataset(
        str(grib_path),
        engine='cfgrib',
        backend_kwargs={'indexpath': ''},
    )
    # NOMADS file contains exactly one variable (PRMSL at meanSea)
    var = list(ds.data_vars)[0]
    lat_1d = ds.latitude.values.copy()
    lon_1d = ds.longitude.values.copy()
    msl_2d = ds[var].values.copy() / 100.0   # Pa → hPa
    ds.close()

    # Ensure lats ascending
    if lat_1d[0] > lat_1d[-1]:
        lat_1d = lat_1d[::-1]
        msl_2d = msl_2d[::-1, :]

    # Convert lons from 0–360 to −180–180 and sort
    lon_1d = np.where(lon_1d > 180.0, lon_1d - 360.0, lon_1d)
    order = np.argsort(lon_1d)
    lon_1d = lon_1d[order]
    msl_2d = msl_2d[:, order]

    return lat_1d, lon_1d, msl_2d


# ---------------------------------------------------------------------------
# PNG rendering
# ---------------------------------------------------------------------------

def render_mslp_png(lat_1d, lon_1d, msl_2d, out_path):
    """
    Render MSLP isobar contours as a transparent PNG in Web Mercator projection,
    matching the satellite domain bounds used in index.html.
    """
    # Build the Mercator output grid (top-row = north = highest Mercator Y)
    merc_xs = np.linspace(MERC_XMIN, MERC_XMAX, OUT_WIDTH)
    merc_ys = np.linspace(MERC_YMAX, MERC_YMIN, OUT_HEIGHT)  # north → south
    MERC_X, MERC_Y = np.meshgrid(merc_xs, merc_ys)

    # Convert Mercator grid to lat/lon for MSLP lookup
    GRID_LON, GRID_LAT = _m2ll.transform(MERC_X, MERC_Y)

    # Clamp to GRIB extent (handles polar extremes)
    GRID_LAT = np.clip(GRID_LAT, lat_1d[0], lat_1d[-1])
    GRID_LON = np.clip(GRID_LON, lon_1d[0], lon_1d[-1])

    # Interpolate MSLP onto Mercator pixel grid
    interp = RegularGridInterpolator(
        (lat_1d, lon_1d), msl_2d,
        method='linear', bounds_error=False, fill_value=None,
    )
    pts = np.column_stack([GRID_LAT.ravel(), GRID_LON.ravel()])
    msl_grid = interp(pts).reshape(OUT_HEIGHT, OUT_WIDTH)

    # ---- Matplotlib rendering on transparent canvas ----
    fig = plt.figure(figsize=(OUT_WIDTH / DPI, OUT_HEIGHT / DPI), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])   # axes fills entire figure

    # Map data coordinates to Mercator space.
    # matplotlib y-axis is bottom=YMIN, top=YMAX (north at top) → correct.
    ax.set_xlim(MERC_XMIN, MERC_XMAX)
    ax.set_ylim(MERC_YMIN, MERC_YMAX)
    ax.axis('off')
    fig.patch.set_alpha(0)
    ax.set_facecolor('none')

    # Contour levels every 4 hPa (standard synoptic spacing)
    vmin = np.floor(np.nanmin(msl_grid) / 4) * 4
    vmax = np.ceil(np.nanmax(msl_grid) / 4) * 4
    levels_4 = np.arange(max(940, vmin), min(1052, vmax + 4), 4)

    if len(levels_4) == 0:
        plt.close(fig)
        return False

    # Thin black lines every 4 hPa — white halo via path effect
    # At DPI=200: 0.55pt = 1.53px — clean, above antialiasing threshold
    n = len(ax.collections)
    cs = ax.contour(
        MERC_X, MERC_Y, msl_grid,
        levels=levels_4,
        colors='black',
        linewidths=0.55,
    )
    for coll in ax.collections[n:]:
        coll.set_path_effects([
            pe.withStroke(linewidth=3.2, foreground=(1, 1, 1, 0.72)),
            pe.Normal(),
        ])

    # Thick lines at multiples of 20 hPa — wider halo
    # At DPI=200: 1.2pt = 3.33px
    thick_lvls = [l for l in levels_4 if l % 20 == 0]
    if thick_lvls:
        n = len(ax.collections)
        ax.contour(
            MERC_X, MERC_Y, msl_grid,
            levels=thick_lvls,
            colors='black',
            linewidths=1.2,
        )
        for coll in ax.collections[n:]:
            coll.set_path_effects([
                pe.withStroke(linewidth=5.0, foreground=(1, 1, 1, 0.72)),
                pe.Normal(),
            ])

    # Labels on every 8 hPa (less clutter)
    label_lvls = [l for l in levels_4 if l % 8 == 0]
    clabels = ax.clabel(cs, levels=label_lvls, inline=True, fontsize=8,
                        fmt='%d', use_clabeltext=True)
    for txt in clabels:
        txt.set_path_effects([pe.withStroke(linewidth=2.0, foreground='white')])
        txt.set_color('black')

    fig.savefig(str(out_path), format='png', transparent=True, dpi=DPI)
    plt.close(fig)
    print(f"  Rendered: {out_path.name}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def cleanup_old_gribs(days=5):
    """Remove cached GRIB files older than `days` days."""
    cutoff = datetime.datetime.now(timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=days)
    for f in Path(GRIB_CACHE_DIR).glob("*.grib2"):
        try:
            if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                print(f"  Removed old GRIB: {f.name}")
        except OSError:
            pass


def main():
    os.makedirs(GRIB_CACHE_DIR, exist_ok=True)
    os.makedirs(PNG_DIR, exist_ok=True)

    r2 = get_r2_client() if USE_R2 else None
    r2_keys = list_r2_mslp_keys(r2) if USE_R2 else set()

    valid_times = get_valid_times()
    print(f"Processing {len(valid_times)} MSLP valid times "
          f"({valid_times[0].strftime('%Y-%m-%d %HZ')} → {valid_times[-1].strftime('%Y-%m-%d %HZ')})")

    meta_frames = []

    for vt in valid_times:
        ts = vt.strftime('%Y%m%d%H%M')
        png_name = f"{ts}_mslp.png"
        png_path = Path(PNG_DIR) / png_name
        r2_key = f"{R2_PREFIX}/{png_name}"

        # Already in R2 — just record in meta
        if USE_R2 and r2_key in r2_keys:
            print(f"Skip (already in R2): {png_name}")
            meta_frames.append({
                'valid_time': vt.strftime('%a %d %b %Y %H:%M UTC'),
                'url': f"{R2_PUBLIC_URL}/{r2_key}",
            })
            continue

        print(f"\nValid time: {vt.strftime('%Y-%m-%d %HZ')}")

        # Get GRIB (download if necessary)
        grib_path = get_grib(vt)
        if grib_path is None:
            print(f"  No GRIB available for {vt}, skipping")
            continue

        # Render PNG
        if not png_path.exists():
            try:
                lat_1d, lon_1d, msl_2d = read_mslp_from_grib(grib_path)
                ok = render_mslp_png(lat_1d, lon_1d, msl_2d, png_path)
                if not ok:
                    print(f"  Render produced no levels, skipping")
                    continue
            except Exception as e:
                print(f"  Render error: {e}")
                import traceback; traceback.print_exc()
                continue
        else:
            print(f"  Already rendered: {png_name}")

        # Upload & record
        if USE_R2:
            upload_to_r2(r2, str(png_path), r2_key, r2_keys)
            url = f"{R2_PUBLIC_URL}/{r2_key}"
        else:
            url = f"static/mslp/{png_name}"

        meta_frames.append({
            'valid_time': vt.strftime('%a %d %b %Y %H:%M UTC'),
            'url': url,
        })

    # Write metadata JSON
    meta = {
        'generated_at': datetime.datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%dT%H:%M:00.000Z'),
        'frames': meta_frames,
    }
    with open(META_FILE, 'w') as f:
        json.dump(meta, f, indent=2)
    print(f"\nMeta written: {len(meta_frames)} MSLP frames → {META_FILE}")

    if USE_R2:
        upload_to_r2(r2, META_FILE, META_FILE, r2_keys,
                     content_type='application/json', force=True)
        cleanup_r2_mslp(r2)

    cleanup_old_gribs()


if __name__ == "__main__":
    main()
