"""
fetch_ecmwf.py — Download ECMWF HRES (IFS) 9km native-resolution deterministic
rainfall forecast, render PNGs, upload to R2.

Source: Open-Meteo's public AWS Open Data S3 bucket (`s3://openmeteo`,
us-west-2, unauthenticated `--no-sign-request` access), which redistributes
ECMWF's own IFS HRES output at full ~9km native resolution (O1280 octahedral
reduced Gaussian grid) under CC-BY 4.0 — the same license and ultimate source
as ECMWF's own Open Data feed, just not yet offered directly by ECMWF at this
resolution (their own free feed is capped at 0.25°; ECMWF says a native-
resolution free feed of their own is coming "later in 2026"). This is a
genuinely different, better data source than an earlier version of this
script that pulled ECMWF's own 0.25° Open Data GRIB2 files directly and
smoothed them for display — that coarser path has been removed entirely now
that real 9km data is usable.

Key format: data_spatial/ecmwf_ifs/{YYYY}/{MM}/{DD}/{HH}00Z/{valid_iso}.om
  - meta.json alongside each run's files lists exact valid_times + a
    `completed` flag — used directly as the "run ready" signal, no need to
    probe for a specific final-step file like the old GRIB2 approach did.
  - 00Z/12Z runs go to the full 15-day horizon (hourly to T+90h, 3-hourly to
    T+144h, 6-hourly beyond); 06Z/18Z runs only reach T+144h — the same
    oper/scda-style split ECMWF's own Open Data feed has, just relabelled
    here by run hour rather than a "stream" field from the source.
  - Each .om file is a hierarchical store of ~48 variables for that one
    valid time; we only read the `precipitation` child. Per Open-Meteo's own
    docs, precipitation is a **backward sum** — the total rainfall (mm) for
    the interval since the *previous* available valid time (1h/3h/6h
    depending on where in the run's cadence it falls) — confirmed
    empirically (see git history / CLAUDE.md) by checking that global sums
    scale with the interval length rather than growing cumulatively. That
    means, unlike raw ECMWF `tp` (cumulative since T+0), no de-accumulation
    step is needed here — each step's value already is the incremental
    rainfall for its own interval.

Grid: the O1280 octahedral reduced Gaussian grid has no per-file coordinate
array (it's implicit/static, the same for every timestep and run), so
build_o1280_grid() reconstructs it: Gaussian latitudes via Gauss-Legendre
quadrature (numpy) + the standard octahedral points-per-row formula
pl(i) = 4*i + 16. This was verified two ways before being trusted: the
computed total point count (6,599,680) matches Open-Meteo's array length
exactly, and a rendered subset over the UK domain lines up pixel-for-pixel
with the UK coastline/region boundaries.

Remapping onto our output canvas uses inverse-distance-weighted interpolation
over each pixel's 4 nearest source points (scipy.spatial.cKDTree, built once
per run since the source grid is static) rather than a regular-grid method —
the O1280 grid is unstructured (each latitude row has a different point
count), so a bilinear RegularGridInterpolator (as used for MSLP/older ECMWF
code) does not apply here.

v1 scope: a single "met"-style colour scheme (matching the alternative
palette already used by /ukv), and four accumulation windows (3h/6h/24h/48h).
No splat masks, no polygon/gauge time series — those can be added later the
way the UKV pipeline itself grew incrementally.

Usage: python fetch_ecmwf.py
Required env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
              R2_BUCKET_NAME, R2_PUBLIC_BASE_URL
Optional env: FORCE_RERUN (reprocess latest run even if already marked processed),
              KEEP_RUNS (older runs retained in the manifest, default 30)

Dependencies (install in CI with pip — not in requirements.txt, same
convention as fetch_mslp.py/the GRIB-based scripts, even though this script
no longer touches GRIB itself):
    pip install boto3 botocore omfiles fsspec s3fs numpy scipy Pillow pyproj
"""
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3
import fsspec
import numpy as np
from omfiles import OmFileReader
from PIL import Image
from pyproj import Transformer
from scipy.spatial import cKDTree

# ── Output domain — same canvas as /ukv for direct visual comparability.
LON_MIN, LON_MAX = -26.0, 17.0
LAT_MIN, LAT_MAX =  43.0, 63.0
WIDTH,   HEIGHT  =  1725, 1800

# ── Open-Meteo Open Data S3 (public AWS Open Data Sponsorship bucket) ────────
OM_BUCKET = "openmeteo"
OM_MODEL  = "ecmwf_ifs"   # 9km native-resolution IFS HRES, distinct from "ecmwf_ifs025"

RUN_HOURS = [0, 6, 12, 18]

# KDTree remap: k nearest source points per output pixel, IDW-blended.
K_NEIGHBORS = 4
GRID_PAD_DEG = 1.0  # margin around the output domain when subsetting source points

# ── Colour scheme — reuses the existing "met"-style palette from /ukv (13-
# class blue→green→yellow→orange→red→purple→magenta→white→grey→olive),
# chosen deliberately as an *alternative* to UKV's default "norm" scheme so
# the two pages aren't visually confusable.
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


def get_om_fs():
    """Anonymous fsspec filesystem for Open-Meteo's public S3 bucket."""
    return fsspec.filesystem("s3", anon=True)


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
def run_prefix(run_dt):
    return f"data_spatial/{OM_MODEL}/{run_dt:%Y}/{run_dt:%m}/{run_dt:%d}/{run_dt:%H}00Z/"


def meta_key(run_dt):
    return run_prefix(run_dt) + "meta.json"


def run_ts_str(run_dt):
    return run_dt.strftime("%Y%m%dT%H%MZ")


def parse_run_ts(run_ts):
    return datetime.strptime(run_ts, "%Y%m%dT%H%MZ")


def fetch_run_meta(om_fs, run_dt):
    """Read a run's meta.json (valid_times + completed flag). None if absent."""
    try:
        with om_fs.open(f"{OM_BUCKET}/{meta_key(run_dt)}", "rb") as f:
            return json.loads(f.read())
    except Exception:
        return None


def find_latest_run(om_fs):
    """Find the latest run whose meta.json reports completed=true.

    Open-Meteo's bucket only retains a handful of recent days, so a short
    walk-back is enough. A run whose meta.json exists but isn't complete yet
    is skipped in favour of an older complete run, so a slow/delayed newest
    run doesn't stall processing of one that already finished.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    newest_seen = None
    for days_back in range(4):
        date = (now - timedelta(days=days_back)).date()
        for run_h in sorted(RUN_HOURS, reverse=True):
            run_dt = datetime(date.year, date.month, date.day, run_h)
            if run_dt > now:
                continue
            meta = fetch_run_meta(om_fs, run_dt)
            if meta is None:
                print(f"    {run_ts_str(run_dt)}: not available yet")
                continue
            if newest_seen is None:
                newest_seen = run_dt
            if meta.get("completed"):
                return run_dt, meta
            print(f"    {run_ts_str(run_dt)}: started but not yet complete — checking earlier run")
    if newest_seen:
        print(f"  No complete run found; newest in-progress run is {run_ts_str(newest_seen)}")
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


def valid_label_str(dt_utc_naive):
    """Forecast step valid time — shown in UK local time (BST/GMT), since
    that's what a UK reader actually cares about for a specific moment."""
    local, tz = _uk_local(dt_utc_naive.replace(tzinfo=timezone.utc))
    return f"{local.day} " + local.strftime("%b %Y %H:%M") + f" {tz}"


def run_label_str(dt_utc_naive):
    """Model run time — always UTC ("the 00Z/12Z run"), never localized to
    BST/GMT. Meteorological run times are conventionally quoted in UTC
    regardless of season; converting to local time would make "the 12Z run"
    read as 13:00 in summer, which isn't how forecasters refer to it."""
    return f"{dt_utc_naive.day} " + dt_utc_naive.strftime("%b %Y %H:%M") + " UTC"


def parse_valid_time(vt_str):
    """meta.json valid_times look like '2026-08-12T03:00Z'."""
    return datetime.strptime(vt_str, "%Y-%m-%dT%H:%MZ")


def valid_iso_filename(dt):
    """.om filenames use '2026-08-12T0300' (no colon, no trailing Z)."""
    return dt.strftime("%Y-%m-%dT%H%M")


# ── O1280 octahedral reduced Gaussian grid ────────────────────────────────────
def build_o1280_grid():
    """Reconstruct the full O1280 grid's per-point (lat, lon), matching
    Open-Meteo's flat 6,599,680-point array ordering.

    There is no coordinate array shipped with the data (it's static and the
    same for every file), so this derives it: Gaussian latitudes for N=1280
    via Gauss-Legendre quadrature, and the standard octahedral points-per-row
    formula pl(i) = 4*i + 16 (mirrored south of the equator). Verified before
    being trusted: the resulting point count (6,599,680) matches Open-Meteo's
    array length exactly, and a rendered UK-domain subset lines up with the
    actual UK coastline/region boundaries.
    """
    N = 1280
    nodes, _ = np.polynomial.legendre.leggauss(2 * N)
    lat_rows = np.degrees(np.arcsin(nodes))[::-1]  # north (~90) -> south (~-90), 2560 rows
    pl_half = np.array([4 * i + 16 for i in range(1, N + 1)])
    pl_full = np.concatenate([pl_half, pl_half[::-1]])
    assert int(pl_full.sum()) == 6_599_680, f"O1280 point count mismatch: {pl_full.sum()}"

    lats = np.repeat(lat_rows, pl_full)
    lons = np.concatenate([np.arange(n) * (360.0 / n) for n in pl_full])
    lons = np.where(lons > 180.0, lons - 360.0, lons)
    return lats, lons


def _to_xyz(lat_deg, lon_deg):
    """Unit-sphere Cartesian coords — a correct distance metric everywhere,
    including near the poles, unlike naive lat/lon Euclidean distance."""
    la, lo = np.radians(lat_deg), np.radians(lon_deg)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def build_mapping(lats, lons):
    """Precompute the IDW remap from O1280 source points onto the output
    canvas: which source points are in range, and for every output pixel,
    its K nearest source-point indices + normalized inverse-distance weights.

    Built once per run (the source grid and output canvas are both static),
    then reused for every step — only extract_precip_mm()'s weighted sum
    changes per timestep.
    """
    subset_mask = (
        (lons >= LON_MIN - GRID_PAD_DEG) & (lons <= LON_MAX + GRID_PAD_DEG) &
        (lats >= LAT_MIN - GRID_PAD_DEG) & (lats <= LAT_MAX + GRID_PAD_DEG)
    )
    subset_idx = np.nonzero(subset_mask)[0]
    sub_lat, sub_lon = lats[subset_idx], lons[subset_idx]
    tree = cKDTree(_to_xyz(sub_lat, sub_lon))

    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    merc_to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    mx0, my0 = ll_to_merc.transform(LON_MIN, LAT_MIN)
    mx1, my1 = ll_to_merc.transform(LON_MAX, LAT_MAX)
    mxs = np.linspace(mx0, mx1, WIDTH)
    mys = np.linspace(my1, my0, HEIGHT)  # top -> bottom
    MX, MY = np.meshgrid(mxs, mys)
    TLon, TLat = merc_to_ll.transform(MX, MY)

    dist, nn_idx = tree.query(_to_xyz(TLat.ravel(), TLon.ravel()), k=K_NEIGHBORS, workers=-1)
    weights = 1.0 / (dist ** 2 + 1e-12)
    weights /= weights.sum(axis=1, keepdims=True)

    print(f"    mapping ready: {len(subset_idx):,} source points, "
          f"{WIDTH * HEIGHT:,} output pixels, k={K_NEIGHBORS}")
    return {"subset_idx": subset_idx, "nn_idx": nn_idx, "weights": weights}


# ── Data extraction ────────────────────────────────────────────────────────────
def extract_precip_mm(om_fs, run_dt, valid_dt, mapping):
    """Read the `precipitation` child (mm, backward sum over the interval
    since the previous available valid time) for one timestep, IDW-remap
    onto the output canvas. Returns None on any read failure."""
    key = f"{OM_BUCKET}/{run_prefix(run_dt)}{valid_iso_filename(valid_dt)}.om"
    try:
        with om_fs.open(key, "rb") as f:
            reader = OmFileReader(f)
            precip = np.asarray(
                reader.get_child_by_name("precipitation").read_array((0, slice(None)))
            ).ravel()
    except Exception as e:
        print(f"    Warning: failed to read {key} — {e}")
        return None

    sub = precip[mapping["subset_idx"]]
    sub = np.where(np.isfinite(sub), sub, 0.0).astype(np.float32)
    sub = np.clip(sub, 0, None)

    out = (sub[mapping["nn_idx"]] * mapping["weights"]).sum(axis=1)
    out = out.reshape(HEIGHT, WIDTH).astype(np.float32)
    return np.clip(out, 0, None)


def render_png(arr, scheme):
    indices = np.digitize(arr, scheme["bounds"])
    indices[arr <= 0] = 0
    return Image.fromarray(scheme["colors"][indices], "RGBA")


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

    om_fs = get_om_fs()
    r2 = get_r2()

    existing_meta = json_from_r2(r2, "ecmwf_meta.json", {"runs": []})
    existing_runs = existing_meta.get("runs", [])

    print("Finding latest complete ECMWF HRES (9km) run...")
    run_dt, om_meta = find_latest_run(om_fs)
    if not run_dt:
        print("No complete run found yet.")
        sys.exit(0)
    run_ts = run_ts_str(run_dt)
    stream = "oper" if run_dt.hour in (0, 12) else "scda"
    print(f"  Latest complete: {run_ts} ({stream})")

    force = os.environ.get("FORCE_RERUN", "").lower() in ("1", "true", "yes")
    try:
        keep_runs = int(os.environ.get("KEEP_RUNS", "30"))
    except ValueError:
        keep_runs = 30
    if not force and existing_runs and existing_runs[0].get("run_ts") == run_ts:
        print(f"  {run_ts} already processed — done.")
        sys.exit(0)

    # valid_times[0] is T+0 (no preceding interval to sum) — skip it, same as
    # the old GRIB2 pipeline skipped step 0.
    valid_dts = [parse_valid_time(v) for v in om_meta.get("valid_times", [])][1:]
    if not valid_dts:
        print("  No forecast steps found.")
        sys.exit(1)
    steps_h = [round((v - run_dt).total_seconds() / 3600) for v in valid_dts]
    print(f"  {len(valid_dts)} steps: T+{steps_h[0]}h → T+{steps_h[-1]}h")

    print("Building O1280 grid + remap (once per run)...")
    lats, lons = build_o1280_grid()
    mapping = build_mapping(lats, lons)

    rlabel = run_label_str(run_dt)
    print(f"  Run: {rlabel}")

    step_entries_by_hours = {}
    prev_hours = 0
    accum_stack = []  # [{'arr': ndarray, 'hours': int}] — real backward-sum intervals

    def _fetch_one(valid_dt):
        return extract_precip_mm(om_fs, run_dt, valid_dt, mapping)

    # Download+remap steps in parallel (I/O-bound S3 reads), but the accum
    # stack must still be folded in chronological order, so results are
    # collected first and then walked in sequence below.
    #
    # STEP_FETCH_WORKERS=16 is not an arbitrary bump: the omfiles library
    # reads each .om file as ~40-50 small (~65KB) sequential range GETs
    # rather than one bulk transfer (confirmed via S3 request tracing), so
    # each file's read time is dominated by per-request network latency, not
    # bandwidth — the actual data needed per file is only ~1-2MB. That makes
    # this purely latency-bound and highly parallelizable: empirically, 16
    # concurrent workers roughly halved wall-clock time vs 6 for the same
    # batch of files (33.4s -> 17.8s for 12 files). Going higher (30) was
    # *worse* than 16 — likely contention on fsspec's internal sync-over-
    # async event loop — so 16 is a measured sweet spot, not a guess.
    STEP_FETCH_WORKERS = 16
    results = {}
    with ThreadPoolExecutor(max_workers=STEP_FETCH_WORKERS) as ex:
        futs = {ex.submit(_fetch_one, vdt): vdt for vdt in valid_dts}
        for i, fut in enumerate(as_completed(futs), 1):
            vdt = futs[fut]
            results[vdt] = fut.result()
            print(f"  [{i}/{len(valid_dts)}] fetched T+{round((vdt - run_dt).total_seconds() / 3600)}h", flush=True)

    for hours, valid_dt in zip(steps_h, valid_dts):
        offset = f"PT{hours:04d}H00M"
        vlabel = valid_label_str(valid_dt)
        entry = {"offset": offset, "offset_hours": hours, "valid_label": vlabel}

        precip = results.get(valid_dt)
        if precip is None:
            step_entries_by_hours[hours] = entry
            continue

        step_interval_hours = hours - prev_hours
        prev_hours = hours

        rate_arr = precip / step_interval_hours
        urls = upload_scheme_set(rate_arr, ECMWF_RATE_SCHEME, run_ts, offset, "rate")
        if urls:
            entry["rainfall_rate"] = urls

        accum_stack.append({"arr": precip, "hours": step_interval_hours})
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
                # 6-hourly tail. Expected, not a bug.
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
                    afuts = [ex.submit(_upload_accum, n, a) for n, a in accum_renders]
                    for fut in as_completed(afuts):
                        n, urls = fut.result()
                        entry[f"accum_{n}h"] = urls

        step_entries_by_hours[hours] = entry

    step_entries = [step_entries_by_hours[h] for h in steps_h]

    new_run = {
        "run_ts":         run_ts,
        "run_label":      rlabel,
        "stream":         stream,
        "forecast_hours": steps_h[-1],
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
        "attribution":  "ECMWF HRES (IFS) 9km native-resolution deterministic — CC BY 4.0, "
                         "ecmwf.int, via Open-Meteo (openmeteo.com) Open Data",
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
