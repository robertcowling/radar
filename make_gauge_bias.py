import io
import json
import math
import os
import sys
from datetime import datetime, timedelta

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.client import Config
from pyproj import Transformer

# ── Output domain (must match make_accum_multi.py exactly) ─────────────────────
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX =  43.7009, 62.9207
WIDTH,   HEIGHT  =  1725,    2175

PERIODS = ["1hr", "3hr", "6hr", "12hr", "24hr", "48hr", "5d"]
PERIOD_HOURS = {"1hr": 1, "3hr": 3, "6hr": 6, "12hr": 12, "24hr": 24, "48hr": 48, "5d": 120}
HIST_RETENTION = 192   # 48hr × 4 runs/hr

# ── R2 ─────────────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])


def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def npz_from_r2(r2, key):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return np.load(io.BytesIO(obj["Body"].read()))["data"]
    except Exception:
        return None


def json_from_r2(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default


def json_to_r2(r2, key, data):
    r2.put_object(
        Bucket=R2_BUCKET, Key=key,
        Body=json.dumps(data, separators=(",", ":")).encode(),
        ContentType="application/json; charset=utf-8",
    )


# ── Coordinate mapping ──────────────────────────────────────────────────────────
def build_merc_bounds():
    ll_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xmin, ymin = ll_to_merc.transform(LON_MIN, LAT_MIN)
    xmax, ymax = ll_to_merc.transform(LON_MAX, LAT_MAX)
    return ll_to_merc, xmin, xmax, ymin, ymax


def station_pixel(ll_to_merc, xmin, xmax, ymax, ymin_merc, lon, lat):
    """Return (row, col) in the NPZ grid for a given lon/lat, or None if outside domain."""
    mx, my = ll_to_merc.transform(lon, lat)
    col = round((mx - xmin) / (xmax - xmin) * (WIDTH - 1))
    row = round((ymax - my) / (ymax - ymin_merc) * (HEIGHT - 1))
    if 0 <= row < HEIGHT and 0 <= col < WIDTH:
        return row, col
    return None


def build_radius_mask(row, col, lat, xmin, xmax, ymin_m, ymax_m, radius_m=5000):
    """Return (rows, cols) index arrays for all pixels within radius_m of (row, col).
    Uses Mercator-space distance scaled by 1/cos(lat) to approximate true Earth distance."""
    merc_r = radius_m / math.cos(math.radians(lat))
    dx = (xmax - xmin) / WIDTH    # Mercator metres per pixel column
    dy = (ymax_m - ymin_m) / HEIGHT  # Mercator metres per pixel row
    r_col = merc_r / dx
    r_row = merc_r / dy
    c0 = max(0, int(col - r_col) - 1)
    c1 = min(WIDTH - 1,  int(col + r_col) + 1)
    r0 = max(0, int(row - r_row) - 1)
    r1 = min(HEIGHT - 1, int(row + r_row) + 1)
    rr, cc = np.meshgrid(np.arange(r0, r1 + 1), np.arange(c0, c1 + 1), indexing='ij')
    inside = ((rr - row) / r_row) ** 2 + ((cc - col) / r_col) ** 2 <= 1.0
    return rr[inside].ravel(), cc[inside].ravel()


# ── Gauge data helpers ──────────────────────────────────────────────────────────
def frame_label(fk):
    try:
        return datetime.strptime(fk, "%Y%m%d%H%M").strftime("%a %d %b %Y %H:%M UTC")
    except Exception:
        return fk


def fk_to_date_slot(fk):
    """'202605101245' → ('20260510', 51)  [slot = HH*4 + MM//15]"""
    return fk[:8], int(fk[8:10]) * 4 + int(fk[10:12]) // 15


def load_day_file(r2, date_key, day_cache):
    if date_key in day_cache:
        return day_cache[date_key]
    data = json_from_r2(r2, f"rain/readings/{date_key}.json", default=None)
    readings = data["readings"] if data else {}
    day_cache[date_key] = readings
    return readings


def compute_gauge_accum(r2, frame_keys, station_ids, day_cache):
    """Sum gauge readings for all stations over the given frame key window.
    Returns dict stationId -> (total_mm, n_slots_found) for all periods sharing this window."""
    totals = {sid: 0.0 for sid in station_ids}
    counts = {sid: 0   for sid in station_ids}

    for fk in frame_keys:
        date_key, slot = fk_to_date_slot(fk)
        readings = load_day_file(r2, date_key, day_cache)
        slot_str = str(slot)
        for sid in station_ids:
            v = readings.get(sid, {}).get(slot_str)
            if isinstance(v, (int, float)) and v >= 0:
                totals[sid] += v
                counts[sid] += 1

    return totals, counts


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    r2 = get_r2()

    # Load station registry
    print("Loading stations.json...")
    with open("rain/stations.json") as f:
        sdata = json.load(f)
    stations = sdata["stations"]
    print(f"  {len(stations)} stations loaded")

    # Precompute pixel locations and 5km radius masks
    print("Computing station pixel locations and 5km masks...")
    ll_to_merc, xmin, xmax, ymin_merc, ymax = build_merc_bounds()
    station_pixels = {}
    station_masks  = {}
    for sid, s in stations.items():
        px = station_pixel(ll_to_merc, xmin, xmax, ymax, ymin_merc, s["lon"], s["lat"])
        if px is not None:
            station_pixels[sid] = px
            station_masks[sid]  = build_radius_mask(
                px[0], px[1], s["lat"], xmin, xmax, ymin_merc, ymax
            )
    print(f"  {len(station_pixels)} stations inside radar domain")

    station_ids = list(station_pixels.keys())

    # SEPA gauges report hourly (1-in-4 15-min slots), unlike EA/NRW's native
    # 15-min cadence — the "≥50% of slots populated" completeness check below
    # must be scaled per station or a fully-reporting SEPA gauge never qualifies.
    cadence_divisor = {
        sid: (4 if stations.get(sid, {}).get("source") == "sepa" else 1)
        for sid in station_ids
    }

    # Load accum sums and frame key windows per period
    print("Loading accum sums and frame windows from R2...")
    period_sums   = {}   # period -> np.array
    period_fkeys  = {}   # period -> list of frame keys
    snap_ts = None

    for period in PERIODS:
        arr = npz_from_r2(r2, f"accum_sum/{period}.npz")
        fkeys = json_from_r2(r2, f"accum_sum/{period}_index.json", default=[])
        if arr is None or not fkeys:
            print(f"  [{period}] sum or index missing — skipping")
            continue
        period_sums[period]  = arr
        period_fkeys[period] = fkeys
        if snap_ts is None:
            snap_ts = fkeys[-1]   # latest frame key = snapshot timestamp
        print(f"  [{period}] {len(fkeys)} frames  {fkeys[0]} → {fkeys[-1]}")

    if not period_sums:
        print("No period data available — aborting.")
        sys.exit(1)

    snap_time = frame_label(snap_ts)
    print(f"Snapshot: {snap_ts} ({snap_time})")

    # Load needed gauge day files
    print("Loading gauge day files...")
    needed_dates = set()
    for fkeys in period_fkeys.values():
        for fk in fkeys:
            needed_dates.add(fk[:8])
    day_cache = {}
    for dk in sorted(needed_dates):
        load_day_file(r2, dk, day_cache)
        print(f"  Loaded rain/readings/{dk}.json ({len(day_cache[dk])} stations)")

    # Build per-station, per-period gauge accumulations and radar samples
    print("Computing gauge accumulations and sampling radar...")

    # Compute gauge totals for each period separately
    period_gauge = {}   # period -> {sid: mm}
    for period, fkeys in period_fkeys.items():
        n_frames = len(fkeys)
        totals, counts = compute_gauge_accum(r2, fkeys, station_ids, day_cache)
        period_gauge[period] = {
            sid: totals[sid]
            for sid in station_ids
            if counts[sid] >= (n_frames / cadence_divisor[sid]) * 0.5
        }
        print(f"  [{period}] {len(period_gauge[period])} gauges with sufficient data")

    # Build output: per station, triplets [g, r_pt, r_5km] × len(PERIODS)
    result_stations = {}
    for sid, (row, col) in station_pixels.items():
        values = []
        has_any = False
        mask_r, mask_c = station_masks.get(sid, (np.array([row]), np.array([col])))
        for period in PERIODS:
            arr = period_sums.get(period)
            gauge_dict = period_gauge.get(period, {})
            g    = round(gauge_dict[sid], 2) if sid in gauge_dict else None
            r_pt = round(float(arr[row, col]), 2) if arr is not None else None
            r_5k = round(float(arr[mask_r, mask_c].mean()), 2) if arr is not None else None
            values.append(g)
            values.append(r_pt)
            values.append(r_5k)
            if g is not None:
                has_any = True
        if has_any:
            result_stations[sid] = values

    print(f"  {len(result_stations)} stations have at least one period of gauge data")

    # Build snapshot JSON
    snapshot = {
        "ts":   snap_ts,
        "time": snap_time,
        "stations": result_stations,
    }

    # Upload snapshot to R2
    r2_key = f"gauge_bias/{snap_ts}.json"
    json_to_r2(r2, r2_key, snapshot)
    print(f"Uploaded {r2_key}")

    # Update meta index
    existing_meta = json_from_r2(r2, "gauge_bias_meta.json", default={})
    snaps = [s for s in existing_meta.get("snapshots", []) if s.get("ts") != snap_ts]
    snaps.append({"ts": snap_ts, "time": snap_time})
    snaps = snaps[-HIST_RETENTION:]

    meta = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "r2_base_url":  R2_PUBLIC_URL,
        "snapshots":    snaps,
    }
    with open("gauge_bias_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    json_to_r2(r2, "gauge_bias_meta.json", meta)
    print(f"Meta written ({len(snaps)} snapshots)")

    # Cleanup old snapshots (14-day retention)
    cutoff_str = (datetime.utcnow() - timedelta(days=14)).strftime("%Y%m%d%H%M")
    deleted = 0
    for page in r2.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix="gauge_bias/"):
        for obj in page.get("Contents", []):
            ts = os.path.basename(obj["Key"])[:12]
            if ts.isdigit() and ts < cutoff_str:
                r2.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])
                deleted += 1
    if deleted:
        print(f"  Deleted {deleted} expired bias snapshots from R2")

    print("Done.")


if __name__ == "__main__":
    main()
