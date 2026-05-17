import json
import os
import sys
from datetime import datetime, timedelta

import boto3
import numpy as np
from pyproj import Transformer

from poly_utils import (
    GEOJSON_LAYERS,
    build_merc_axes,
    build_pixel_masks,
)

# ── Domain (matches make_accum_multi.py / make_gauge_bias.py) ──────────────────
LON_MIN, LON_MAX = -17.9739, 16.1291
LAT_MIN, LAT_MAX =  43.7009, 62.9207
WIDTH,   HEIGHT  =  1725,    2175

# Accumulation periods: name -> number of 15-min slots
PERIODS = {
    "1hr":   4,
    "3hr":   12,
    "6hr":   24,
    "12hr":  48,
    "24hr":  96,
    "48hr":  192,
}

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


def exists_in_r2(r2, key):
    try:
        r2.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception:
        return False


# ── Station helpers ────────────────────────────────────────────────────────────
def station_pixel(ll_to_merc, xmin, xmax, ymax_m, ymin_m, lon, lat):
    mx, my = ll_to_merc.transform(lon, lat)
    col = round((mx - xmin) / (xmax - xmin) * (WIDTH - 1))
    row = round((ymax_m - my) / (ymax_m - ymin_m) * (HEIGHT - 1))
    if 0 <= row < HEIGHT and 0 <= col < WIDTH:
        return row, col
    return None


def build_label_grid(masks):
    """Build uint16 label grid: pixel -> 1-based index into name_list.
    Returns (label_grid, name_list)."""
    name_list = list(masks.keys())
    grid = np.zeros((HEIGHT, WIDTH), dtype=np.uint16)
    for idx, name in enumerate(name_list, start=1):
        rows, cols = masks[name]
        grid[rows, cols] = idx
    return grid, name_list


# ── Gauge accumulation ─────────────────────────────────────────────────────────
def fk_to_date_slot(fk):
    """'202605101245' -> ('20260510', 51)  [slot = HH*4 + MM//15]"""
    return fk[:8], int(fk[8:10]) * 4 + int(fk[10:12]) // 15


def load_day_file(r2, date_key, day_cache):
    if date_key in day_cache:
        return day_cache[date_key]
    data = json_from_r2(r2, f"rain/readings/{date_key}.json", default=None)
    day_cache[date_key] = data["readings"] if data else {}
    return day_cache[date_key]


def snap_to_frame_keys(snap_ts_str, n_slots):
    """Return list of n_slots frame keys ending at snap_ts (oldest first)."""
    dt = datetime.strptime(snap_ts_str, "%Y%m%d%H%M")
    return [
        (dt - timedelta(minutes=15 * (n_slots - 1 - i))).strftime("%Y%m%d%H%M")
        for i in range(n_slots)
    ]


def needed_dates(snap_ts_str, max_slots):
    """Return set of YYYYMMDD date keys needed to cover max_slots back from snap_ts."""
    dt = datetime.strptime(snap_ts_str, "%Y%m%d%H%M")
    oldest = dt - timedelta(minutes=15 * (max_slots - 1))
    dates = set()
    cur = oldest.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= dt:
        dates.add(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return dates


def compute_period_accums(r2, snap_ts_str, station_ids, day_cache):
    """Compute per-station accumulations for each period.
    Returns {period: {station_id: mm}} — only stations with >=50% slot coverage."""
    result = {}
    for period, n_slots in PERIODS.items():
        frame_keys = snap_to_frame_keys(snap_ts_str, n_slots)
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
        min_slots = n_slots * 0.5
        result[period] = {
            sid: totals[sid]
            for sid in station_ids
            if counts[sid] >= min_slots
        }
    return result


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    r2 = get_r2()

    # Load accum metadata to get list of snap_ts values to process
    print("Loading accum_multi_meta.json...")
    with open("accum_multi_meta.json") as f:
        accum_meta = json.load(f)
    all_snaps = [s["ts"] for s in accum_meta.get("snapshots", [])]
    if not all_snaps:
        print("No snapshots in accum_multi_meta.json — nothing to do.")
        return
    print(f"  {len(all_snaps)} snapshots in metadata")

    # Find which snapshots still need gauge_poly
    to_process = [ts for ts in all_snaps if not exists_in_r2(r2, f"gauge_poly/{ts}.json")]
    if not to_process:
        print("All gauge_poly files already exist — nothing to do.")
        return
    print(f"  {len(to_process)} new snapshots to process")

    # Load station registry
    print("Loading rain/stations.json...")
    with open("rain/stations.json") as f:
        sdata = json.load(f)
    stations = sdata["stations"]
    print(f"  {len(stations)} stations loaded")

    # Build Mercator axes and station pixel positions
    print("Building station pixel positions...")
    ll_to_merc, xmin, xmax, ymin_m, ymax_m = build_merc_axes(
        WIDTH, HEIGHT, LON_MIN, LON_MAX, LAT_MIN, LAT_MAX
    )
    station_pixels = {}
    for sid, s in stations.items():
        px = station_pixel(ll_to_merc, xmin, xmax, ymax_m, ymin_m, s["lon"], s["lat"])
        if px is not None:
            station_pixels[sid] = px
    station_ids = list(station_pixels.keys())
    print(f"  {len(station_ids)} stations inside domain")

    # Build pixel masks and label grids for each boundary layer
    print("Building boundary label grids...")
    label_grids = {}   # layer_name -> (label_grid, name_list)
    for layer_name, (geojson_path, name_key) in GEOJSON_LAYERS.items():
        masks = build_pixel_masks(geojson_path, name_key, WIDTH, HEIGHT,
                                  ll_to_merc, xmin, xmax, ymin_m, ymax_m)
        label_grids[layer_name] = build_label_grid(masks)
        print(f"  [{layer_name}] {len(masks)} areas")

    # Build station -> area mapping for each boundary layer
    station_area = {}   # layer_name -> {station_id: area_name}
    for layer_name, (grid, name_list) in label_grids.items():
        mapping = {}
        for sid, (row, col) in station_pixels.items():
            idx = int(grid[row, col])
            if idx > 0:
                mapping[sid] = name_list[idx - 1]
        station_area[layer_name] = mapping
        in_count = sum(1 for sid in station_ids if sid in mapping)
        print(f"  [{layer_name}] {in_count}/{len(station_ids)} stations assigned to an area")

    # Free label grids — no longer needed
    del label_grids

    # Process each new snapshot
    max_slots = max(PERIODS.values())
    for snap_ts in to_process:
        print(f"Processing {snap_ts}...")

        # Ensure required day files are cached
        day_cache = {}
        for date_key in needed_dates(snap_ts, max_slots):
            load_day_file(r2, date_key, day_cache)

        # Compute per-station, per-period accumulations
        period_accums = compute_period_accums(r2, snap_ts, station_ids, day_cache)

        # Aggregate per area for each boundary layer
        poly_data = {}
        for layer_name, mapping in station_area.items():
            # Build reverse map: area -> list of stations in that area
            area_stations = {}
            for sid, area in mapping.items():
                area_stations.setdefault(area, []).append(sid)

            layer_result = {}
            for period, station_vals in period_accums.items():
                period_result = {}
                for area, sids_in_area in area_stations.items():
                    vals = [station_vals[sid] for sid in sids_in_area if sid in station_vals]
                    if vals:
                        period_result[area] = round(sum(vals) / len(vals), 2)
                layer_result[period] = period_result
            poly_data[layer_name] = layer_result

        # Upload to R2
        r2_key = f"gauge_poly/{snap_ts}.json"
        json_to_r2(r2, r2_key, poly_data)

        area_counts = {ln: sum(len(p) for p in ld.values()) for ln, ld in poly_data.items()}
        print(f"  Uploaded {r2_key}  ({area_counts})")

    print("Done.")


if __name__ == "__main__":
    main()
