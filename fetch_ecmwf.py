"""
fetch_ecmwf.py — Download ECMWF HRES (IFS) 0.25° deterministic rainfall forecast,
render PNGs, upload to R2.

ECMWF Open Data S3 bucket: ecmwf-forecasts (eu-central-1, public, unsigned)
Key format: {yyyymmdd}/{HH}z/ifs/0p25/{stream}/{yyyymmdd}{HH}0000-{step}h-{stream}-fc.grib2
  - stream "oper": 00Z/12Z runs, 3-hourly to T+144h then 6-hourly to T+240h.
  - stream "scda": 06Z/18Z runs, 3-hourly to T+90h only (short-range companion runs).
Each GRIB2 file bundles every surface/pressure-level parameter for that step (not
split by parameter) — we filter to `tp` (total precipitation, metres, accumulated
since forecast start) via cfgrib. License: CC BY 4.0 (ecmwf.int) — attribution
required wherever this data is displayed.

Data note: this is ECMWF's free Open Data extract of the HRES/IFS deterministic
model ("Set I" in ECMWF's dataset catalogue) — the same forecast ECMWF's own
OpenCharts products are drawn from, but at Open Data's fixed 0.25° (~28km) grid
rather than the ~9km native model resolution ECMWF's own charts use internally.
That resolution gap — not a data mismatch — is why ECMWF's official charts show
smooth, curved rain-area boundaries while a naive nearest-neighbour render of
this coarser grid looks blocky. render_png() below narrows that gap by
bilinearly interpolating (with a light pre-smooth) onto the output canvas
rather than sampling the nearest source cell, so colour-band edges follow a
smooth interpolated surface instead of raw 28km grid squares.

v1 scope: a single "met"-style colour scheme (matching the alternative palette
already used by /ukv), and four accumulation windows (3h/6h/24h/48h). No splat
masks, no polygon/gauge time series — those can be added later the way the UKV
pipeline itself grew incrementally.

Usage: python fetch_ecmwf.py
Required env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
              R2_BUCKET_NAME, R2_PUBLIC_BASE_URL
Optional env: FORCE_RERUN (reprocess latest run even if already marked processed),
              KEEP_RUNS (older runs retained in the manifest, default 30)

Dependencies (install in CI with apt-get + pip — not in requirements.txt, same
convention as fetch_mslp.py):
    sudo apt-get install -y libeccodes-dev
    pip install boto3 botocore eccodes cfgrib xarray numpy scipy Pillow pyproj
"""
import io
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3
import numpy as np
import xarray as xr
from botocore import UNSIGNED
from botocore.client import Config
from PIL import Image
from pyproj import Transformer
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import gaussian_filter

# ── Output domain — same canvas as /ukv for direct visual comparability.
# ECMWF's 0.25° (~28km) grid renders much blockier than UKV's 2km — expected.
LON_MIN, LON_MAX = -26.0, 17.0
LAT_MIN, LAT_MAX =  43.0, 63.0
WIDTH,   HEIGHT  =  1725, 1800

# ── ECMWF Open Data S3 ────────────────────────────────────────────────────────
ECMWF_BUCKET = "ecmwf-forecasts"
ECMWF_REGION = "eu-central-1"

RUN_HOURS = [0, 6, 12, 18]
STREAM_FOR_HOUR = {0: "oper", 12: "oper", 6: "scda", 18: "scda"}
# Final expected forecast step per stream — used as the "run complete" signal.
FINAL_STEP = {"oper": 240, "scda": 90}

# ── Colour scheme — reuses the existing "met"-style palette from fetch_ukv.py
# (13-class blue→green→yellow→orange→red→purple→magenta→white→grey→olive),
# chosen deliberately as an *alternative* to UKV's default "norm"/_STD scheme
# so the two pages aren't visually confusable, without inventing new colours.
_MET_COLORS = np.array([
    [  0,   0,   0,   0],   # transparent  < 0.03
    [ 58, 108, 255, 255],   # blue          0.03–1
    [  0, 255,   0, 255],   # bright green  1–5
    [255, 255, 149, 255],   # pale yellow   5–10
    [255, 213,  99, 255],   # sand          10–20
    [255, 150,  24, 255],   # orange        20–40
    [232,  97,   0, 255],   # dark orange   40–60
    [186,  32,   0, 255],   # red           60–80
    [204,  83, 125, 255],   # rose          80–100
    [219, 146, 220, 255],   # light purple  100–120
    [255,   2, 255, 255],   # magenta       120–140
    [255, 255, 255, 255],   # white         140–160
    [200, 200, 200, 255],   # light grey    160–180
    [191, 191,   0, 255],   # olive         > 180
], dtype=np.uint8)

# Single-entry dicts (keyed "met") so the manifest shape stays parallel to
# UKV's multi-scheme {scheme_name: url} maps, in case a second scheme is
# ever added later.
ECMWF_RATE_SCHEME = {
    "met": {"bounds": np.array([0.03, 0.1, 0.5, 1, 2, 4, 8, 16, 32, 64, 100, 150, 200]),
            "colors": _MET_COLORS},
}
ECMWF_ACCUM_SCHEME = {
    "met": {"bounds": np.array([0.03, 1, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180]),
            "colors": _MET_COLORS},
}

# Dropped UKV's 1h (no signal at 3-hourly cadence) and 12h (not requested).
ACCUM_PERIODS = [3, 6, 24, 48]

# ── R2 ───────────────────────────────────────────────────────────────────────
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


# Thread-local R2 client so upload threads each hold their own connection.
_tl = threading.local()

def _r2_tl():
    if not getattr(_tl, "r2", None):
        _tl.r2 = get_r2()
    return _tl.r2


def get_s3():
    return boto3.client("s3", region_name=ECMWF_REGION,
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


# ── Run discovery ─────────────────────────────────────────────────────────────
def grib_key(run_dt, stream, step_hours):
    return (f"{run_dt:%Y%m%d}/{run_dt:%H}z/ifs/0p25/{stream}/"
            f"{run_dt:%Y%m%d%H}0000-{step_hours}h-{stream}-fc.grib2")


def step_schedule(stream):
    """Whole-hour forecast steps for a stream, step 0 excluded (tp≡0 there)."""
    if stream == "oper":
        return list(range(3, 145, 3)) + list(range(150, 241, 6))
    return list(range(3, 91, 3))  # scda


def parse_run_ts(run_ts):
    return datetime.strptime(run_ts, "%Y%m%dT%H%MZ")


def run_ts_str(run_dt):
    return run_dt.strftime("%Y%m%dT%H%MZ")


def is_run_complete(s3, run_dt, stream):
    """Return True only when the run's final expected forecast step is on S3."""
    key = grib_key(run_dt, stream, FINAL_STEP[stream])
    chk = s3.list_objects_v2(Bucket=ECMWF_BUCKET, Prefix=key, MaxKeys=1)
    return bool(chk.get("Contents"))


def find_latest_run(s3):
    """Find the latest (run_dt, stream) that is fully complete.

    ECMWF's S3 bucket only retains ~2-3 days (~12 most recent runs), so unlike
    UKV's 7-day walk-back this only needs to look back a few days. Walks
    newest-to-oldest across all 4 daily run hours; a run that has started
    publishing but isn't complete yet is skipped in favour of an older
    complete run, so a slow/delayed newest run doesn't stall processing.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    newest_seen = None
    for days_back in range(4):
        date = (now - timedelta(days=days_back)).date()
        for run_h in sorted(RUN_HOURS, reverse=True):
            run_dt = datetime(date.year, date.month, date.day, run_h)
            if run_dt > now:
                continue
            stream = STREAM_FOR_HOUR[run_h]
            first_key = grib_key(run_dt, stream, step_schedule(stream)[0])
            chk = s3.list_objects_v2(Bucket=ECMWF_BUCKET, Prefix=first_key, MaxKeys=1)
            if not chk.get("Contents"):
                print(f"    {run_ts_str(run_dt)} ({stream}): first step not available yet")
                continue
            if newest_seen is None:
                newest_seen = (run_dt, stream)
            if is_run_complete(s3, run_dt, stream):
                return run_dt, stream
            print(f"    {run_ts_str(run_dt)} ({stream}): started but not yet complete — checking earlier run")
    if newest_seen:
        print(f"  No complete run found; newest in-progress run is {run_ts_str(newest_seen[0])} ({newest_seen[1]})")
    return None, None


def _uk_local(dt_utc):
    """Return (local_dt, tz_name) for UK time (BST or GMT), no external deps.
    Copied verbatim from fetch_ukv.py per this repo's convention of independent
    single-file scripts rather than a shared util module."""
    import calendar
    year = dt_utc.year
    def last_sunday(y, month):
        last_day = calendar.monthrange(y, month)[1]
        d = datetime(y, month, last_day, 1, 0, tzinfo=timezone.utc)
        while d.weekday() != 6:
            d -= timedelta(days=1)
        return d
    bst_start = last_sunday(year, 3)
    bst_end   = last_sunday(year, 10)
    if bst_start <= dt_utc < bst_end:
        return dt_utc + timedelta(hours=1), "BST"
    return dt_utc, "GMT"


def label_str(dt_utc_naive):
    local, tz = _uk_local(dt_utc_naive.replace(tzinfo=timezone.utc))
    return f"{local.day} " + local.strftime("%b %Y %H:%M") + f" {tz}"


# ── Coordinate mapping (built once per run) ───────────────────────────────────
GRIB_LOCK = threading.Lock()


def build_mapping(lat_1d, lon_1d):
    """Return (lat_1d, lon_1d, Lat, Lon, valid) for remapping the ECMWF 0.25°
    regular lat-lon grid onto the output Mercator canvas via interpolation.

    Much simpler than UKV's build_mapping — ECMWF publishes a plain regular
    lat-lon grid, no CRS/grid_mapping detection needed. `open_tp()` has
    already normalized lat_1d ascending and lon_1d to sorted -180..180, so no
    0/360 seam wrap is needed here.

    Unlike UKV (2km native grid, where nearest-neighbour sampling is already
    fine-grained enough to look smooth), ECMWF's 0.25° (~28km) grid is coarse
    enough that nearest-neighbour sampling onto our dense output canvas
    produces visibly blocky ~28km squares — a much blockier look than
    ECMWF's own charts, which draw from ~9km native model resolution. We
    instead keep (Lat, Lon) as continuous target coordinates so
    extract_tp_mm() can bilinearly *interpolate* the source grid at every
    output pixel, which stays faithful to the same 0.25° data but produces
    smooth colour-band transitions instead of hard grid-cell edges.
    """
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    merc_xmin, merc_ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    merc_xmax, merc_ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    merc_xs = np.linspace(merc_xmin, merc_xmax, WIDTH)
    merc_ys = np.linspace(merc_ymax, merc_ymin, HEIGHT)  # top → bottom
    MX, MY  = np.meshgrid(merc_xs, merc_ys)

    Lon, Lat = merc_to_ll.transform(MX, MY)

    valid = (np.isfinite(Lat) & np.isfinite(Lon) &
             (Lat >= lat_1d[0]) & (Lat <= lat_1d[-1]) &
             (Lon >= lon_1d[0]) & (Lon <= lon_1d[-1]))

    # Clamp out-of-range coords so the interpolator never raises on them —
    # `valid` already zeroes those pixels out in extract_tp_mm.
    Lat_c = np.clip(Lat, lat_1d[0], lat_1d[-1])
    Lon_c = np.clip(Lon, lon_1d[0], lon_1d[-1])

    print(f"    valid pixels: {valid.sum():,} / {valid.size:,} ({100*valid.mean():.1f}%)")
    return lat_1d, lon_1d, Lat_c, Lon_c, valid


# ── Data extraction ────────────────────────────────────────────────────────────
def open_tp(grib_path):
    """Open a GRIB2 file, return (lat_1d, lon_1d, tp_metres_2d) for the `tp`
    field only. indexpath='' avoids stray .idx sidecar files on the runner.

    Normalizes lat ascending and lon to sorted -180..180 (ECMWF publishes
    0..359.75 descending-lat by convention) — same normalization fetch_mslp.py
    applies to GFS GRIB2 output — so build_mapping() never has to reason about
    the 0°/360° seam or grid direction.
    """
    with GRIB_LOCK:
        ds = xr.open_dataset(
            grib_path, engine="cfgrib",
            backend_kwargs={"filter_by_keys": {"shortName": "tp"}, "indexpath": ""},
        )
        try:
            var = list(ds.data_vars)[0]
            lat_1d = ds.latitude.values.copy()
            lon_1d = ds.longitude.values.copy()
            tp_2d  = ds[var].values.copy()
        finally:
            ds.close()

    while tp_2d.ndim > 2:
        tp_2d = tp_2d[0]

    if lat_1d[0] > lat_1d[-1]:
        lat_1d = lat_1d[::-1]
        tp_2d  = tp_2d[::-1, :]

    lon_1d = np.where(lon_1d > 180.0, lon_1d - 360.0, lon_1d)
    order  = np.argsort(lon_1d)
    lon_1d = lon_1d[order]
    tp_2d  = tp_2d[:, order]

    return lat_1d, lon_1d, tp_2d


# Light Gaussian pre-smooth (source-grid cells) before bilinear interpolation.
# Softens the coarse 0.25° cell edges into a continuous surface — the same
# technique fetch_mslp.py uses (sigma=1.0) for its isobar contours — without
# materially shifting rain totals; interpolation alone (no smoothing) still
# leaves faint diamond-shaped linear-interpolation artefacts at this grid
# spacing, which the pre-smooth removes.
_SMOOTH_SIGMA = 0.7


def extract_tp_mm(grib_bytes, mapping):
    """Download-to-temp, extract cumulative tp (mm since run start), bilinearly
    interpolate onto the output canvas. Returns None on any read failure.

    Interpolating (rather than nearest-neighbour sampling, as UKV does) keeps
    the same underlying 0.25° data but produces smooth colour-band boundaries
    closer to ECMWF's own higher-resolution charts — see build_mapping()."""
    lat_1d, lon_1d, Lat, Lon, valid = mapping
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tmp:
        tmp.write(grib_bytes)
        path = tmp.name
    try:
        _, _, tp_2d = open_tp(path)
    except Exception as e:
        print(f"    Warning: failed to read tp from GRIB — {e}")
        return None
    finally:
        os.unlink(path)

    arr2d = np.where(np.isfinite(tp_2d), tp_2d, 0.0).astype(np.float32)
    arr2d = np.clip(arr2d, 0, None) * 1000.0  # metres → mm
    arr2d = gaussian_filter(arr2d, sigma=_SMOOTH_SIGMA)

    interp = RegularGridInterpolator((lat_1d, lon_1d), arr2d,
                                      method="linear", bounds_error=False, fill_value=0.0)
    pts = np.column_stack([Lat.ravel(), Lon.ravel()])
    out = interp(pts).reshape(HEIGHT, WIDTH).astype(np.float32)
    out = np.clip(out, 0, None)
    out[~valid] = 0.0
    return out


def render_png(arr, scheme):
    indices = np.digitize(arr, scheme["bounds"])
    indices[arr <= 0] = 0
    return Image.fromarray(scheme["colors"][indices], "RGBA")


def download_grib(s3, run_dt, stream, step_hours):
    key = grib_key(run_dt, stream, step_hours)
    try:
        obj = s3.get_object(Bucket=ECMWF_BUCKET, Key=key)
        return obj["Body"].read()
    except Exception as e:
        print(f"    Warning: {os.path.basename(key)} — {e}")
        return None


def upload_scheme_set(arr, schemes, run_ts, offset, prefix):
    """Render arr with each scheme, upload PNGs, return {scheme_name: url}."""
    if arr is None:
        return None

    def _upload_one(sname, scheme):
        img = render_png(arr, scheme)
        key = f"ecmwf/{run_ts}/{offset}_{prefix}_{sname}.png"
        png_to_r2(_r2_tl(), key, img)
        return sname, f"{R2_PUBLIC_URL}/{key}"

    urls = {}
    with ThreadPoolExecutor(max_workers=len(schemes)) as ex:
        for sname, url in ex.map(lambda item: _upload_one(*item), schemes.items()):
            urls[sname] = url
    return urls


def cleanup_old_ecmwf_runs(r2, active_run_ts):
    """Delete R2 objects for ECMWF runs no longer in the active metadata run list."""
    print("Cleaning up old ECMWF runs from R2...")

    r2_runs = []
    try:
        paginator = r2.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="ecmwf/", Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                run_ts = cp["Prefix"].rstrip("/").split("/")[-1]
                if run_ts:
                    r2_runs.append(run_ts)
    except Exception as e:
        print(f"Error listing R2 folders under ecmwf/: {e}")
        return

    print(f"Found {len(r2_runs)} runs on R2 under ecmwf/")

    deleted_runs = 0
    for run_ts in r2_runs:
        if run_ts in active_run_ts:
            continue
        print(f"  Purging inactive run {run_ts}...")
        deleted_runs += 1
        prefix = f"ecmwf/{run_ts}/"
        deleted_count = 0
        paginator_del = r2.get_paginator("list_objects_v2")
        for page in paginator_del.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                r2.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": keys})
                deleted_count += len(keys)
        if deleted_count > 0:
            print(f"    Deleted {deleted_count} objects under {prefix}")

    print(f"ECMWF cleanup complete. Purged {deleted_runs} runs.")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    s3 = get_s3()
    r2 = get_r2()

    existing_meta = json_from_r2(r2, "ecmwf_meta.json", {"runs": []})
    existing_runs = existing_meta.get("runs", [])

    print("Finding latest complete ECMWF HRES run...")
    run_dt, stream = find_latest_run(s3)
    if not run_dt:
        print("No complete run found on S3 yet.")
        sys.exit(0)
    run_ts = run_ts_str(run_dt)
    print(f"  Latest complete: {run_ts} ({stream})")

    force = os.environ.get("FORCE_RERUN", "").lower() in ("1", "true", "yes")
    try:
        keep_runs = int(os.environ.get("KEEP_RUNS", "30"))
    except ValueError:
        keep_runs = 30
    if not force and existing_runs and existing_runs[0].get("run_ts") == run_ts:
        print(f"  {run_ts} already processed — done.")
        sys.exit(0)

    steps = step_schedule(stream)
    print(f"  {len(steps)} steps: T+{steps[0]}h → T+{steps[-1]}h")

    print("Building coordinate mapping from T+{}h file...".format(steps[0]))
    first_bytes = download_grib(s3, run_dt, stream, steps[0])
    if first_bytes is None:
        print("  Cannot download first step — aborting.")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tmp:
        tmp.write(first_bytes)
        sample_path = tmp.name
    try:
        lat_1d, lon_1d, _ = open_tp(sample_path)
    finally:
        os.unlink(sample_path)
    mapping = build_mapping(lat_1d, lon_1d)
    print("  Mapping ready.")

    rlabel = label_str(run_dt)
    print(f"  Run: {rlabel}")

    step_entries = []
    prev_cumulative = None
    prev_hours = 0
    # Stack of {'arr': ndarray, 'hours': int} holding per-interval (delta) rainfall.
    accum_stack = []

    for i, hours in enumerate(steps):
        offset = f"PT{hours:04d}H00M"
        valid_dt = run_dt + timedelta(hours=hours)
        vlabel = label_str(valid_dt)
        print(f"  [{i+1}/{len(steps)}] {offset}  {vlabel}", flush=True)

        entry = {"offset": offset, "offset_hours": hours, "valid_label": vlabel}

        grib_bytes = download_grib(s3, run_dt, stream, hours)
        cumulative = extract_tp_mm(grib_bytes, mapping) if grib_bytes else None

        if cumulative is None:
            step_entries.append(entry)
            continue

        step_interval_hours = hours - prev_hours
        if prev_cumulative is None:
            delta = cumulative  # first step: delta from run start (t=0) == cumulative
        else:
            delta = np.clip(cumulative - prev_cumulative, 0, None)
        prev_cumulative = cumulative
        prev_hours = hours

        # Rate PNG: average mm/hr over this step's interval.
        rate_arr = delta / step_interval_hours
        urls = upload_scheme_set(rate_arr, ECMWF_RATE_SCHEME, run_ts, offset, "rate")
        if urls:
            entry["rainfall_rate"] = urls

        accum_stack.append({"arr": delta, "hours": step_interval_hours})
        total_stk = sum(s["hours"] for s in accum_stack)
        while total_stk > 48 and accum_stack:
            total_stk -= accum_stack.pop(0)["hours"]

        if accum_stack:
            total_stk = sum(s["hours"] for s in accum_stack)
            accum_renders = []
            for n in ACCUM_PERIODS:
                if total_stk < n:
                    continue
                # Walk backwards accumulating whole slots; stop rather than
                # bridge a gap (e.g. a 6h slot can't serve a 3h request) —
                # this is why accum_3h stops appearing once a run enters its
                # 6-hourly tail (T+150+ for oper runs). Expected, not a bug.
                covered, arrs = 0, []
                for slot in reversed(accum_stack):
                    if covered >= n:
                        break
                    if slot["hours"] <= n - covered:
                        arrs.append(slot["arr"])
                        covered += slot["hours"]
                    else:
                        break
                if covered < n:
                    continue
                accum_renders.append((n, np.sum(arrs, axis=0).astype(np.float32)))

            def _upload_accum(n, arr_n):
                urls = upload_scheme_set(arr_n, ECMWF_ACCUM_SCHEME, run_ts, offset, f"accum_{n}h")
                return n, urls

            if accum_renders:
                with ThreadPoolExecutor(max_workers=min(len(accum_renders), 4)) as ex:
                    futs = [ex.submit(_upload_accum, n, a) for n, a in accum_renders]
                    for fut in as_completed(futs):
                        n, urls = fut.result()
                        entry[f"accum_{n}h"] = urls

        step_entries.append(entry)

    new_run = {
        "run_ts":         run_ts,
        "run_label":      rlabel,
        "stream":         stream,
        "forecast_hours": steps[-1],
        "steps":          step_entries,
    }

    # Retention: keep runs within 72h, capped at KEEP_RUNS older runs.
    now  = datetime.now(timezone.utc).replace(tzinfo=None)
    seen = {run_ts}
    older = []
    for r in existing_runs:
        rt = r.get("run_ts")
        if not rt or rt in seen:
            continue
        try:
            if parse_run_ts(rt) < now - timedelta(hours=72):
                continue
        except Exception:
            continue
        seen.add(rt)
        older.append(r)
    older = older[:max(0, keep_runs)]

    meta = {
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "attribution":  "ECMWF HRES (IFS) 0.25° deterministic — CC BY 4.0, ecmwf.int",
        "runs": [new_run] + older,
    }

    with open("ecmwf_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    json_to_r2(r2, "ecmwf_meta.json", meta)
    print(f"Done: {len(meta['runs'])} run(s) in meta, {len(step_entries)} steps this run.")

    active_run_ts = {r["run_ts"] for r in meta["runs"]}
    cleanup_old_ecmwf_runs(r2, active_run_ts)


if __name__ == "__main__":
    main()
