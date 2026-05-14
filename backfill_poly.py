"""
Backfill accum_poly/{ts}.json for historical snapshots.

Walks backwards from the current running sums, sliding each period window
one 15-min step at a time.  Loads at most 2 frame NPZs per step (one to
remove, one to add per period) — much cheaper than rebuilding from scratch.

Usage:
    python backfill_poly.py          # backfill all snapshots in meta
    python backfill_poly.py 12       # backfill last 3 hr (12 steps)
    python backfill_poly.py 96       # backfill last 24 hr
"""
import os
import sys
from datetime import datetime, timedelta

import numpy as np

from make_accum_multi import (
    GEOJSON_LAYERS, PERIODS, R2_BUCKET,
    compute_poly_averages, get_r2, json_from_r2,
    load_or_build_masks, npz_from_r2, upload_poly_snapshot,
)


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None  # None = all snapshots

    r2 = get_r2()

    # ── Snapshots already processed ───────────────────────────────────────────
    already_done = set()
    for page in r2.get_paginator("list_objects_v2").paginate(
            Bucket=R2_BUCKET, Prefix="accum_poly/"):
        for obj in page.get("Contents", []):
            already_done.add(os.path.basename(obj["Key"])[:12])
    print(f"{len(already_done)} snapshots already have poly data")

    # ── Snapshot list from meta ───────────────────────────────────────────────
    meta = json_from_r2(r2, "accum_multi_meta.json", {})
    all_ts = sorted([s["ts"] for s in meta.get("snapshots", [])], reverse=True)
    if not all_ts:
        print("No snapshots in meta — aborting.")
        return

    steps = min(limit, len(all_ts)) if limit else len(all_ts)
    print(f"Meta has {len(all_ts)} snapshots; backfilling {steps} from {all_ts[0]}")

    # ── Load current running sums (correspond to all_ts[0]) ──────────────────
    print("Loading period sums from R2...")
    sums = {}
    for period in PERIODS:
        arr = npz_from_r2(r2, f"accum_sum/{period}.npz")
        if arr is None:
            print(f"  accum_sum/{period}.npz missing — aborting")
            return
        sums[period] = arr.copy().astype(np.float32)

    # ── Load polygon masks (from R2 cache or build once) ─────────────────────
    print("Loading polygon masks...")
    masks = {}
    for layer_name, (geojson_path, name_key) in GEOJSON_LAYERS.items():
        if os.path.exists(geojson_path):
            masks[layer_name] = load_or_build_masks(r2, layer_name, geojson_path, name_key)
        else:
            print(f"  {geojson_path} not found — skipping {layer_name}")

    if not masks:
        print("No polygon masks available — aborting.")
        return

    # ── Frame cache ───────────────────────────────────────────────────────────
    frame_cache = {}

    def load_frame(fk):
        if fk not in frame_cache:
            frame_cache[fk] = npz_from_r2(r2, f"accum_frames/{fk}.npz")
        return frame_cache[fk]

    # ── Walk newest → oldest ──────────────────────────────────────────────────
    # After step i the sums represent the window ending at all_ts[i].
    # To advance to all_ts[i+1] (15 min earlier):
    #   - Remove frame at all_ts[i]   (drops off the top of every window)
    #   - Add frame at all_ts[i] - period_duration  (enters at the bottom)
    uploaded = 0
    for i, ts in enumerate(all_ts[:steps]):
        if i > 0:
            prev_ts = all_ts[i - 1]
            prev_dt = datetime.strptime(prev_ts, "%Y%m%d%H%M")
            for period, n_frames in PERIODS.items():
                rm = load_frame(prev_ts)
                if rm is not None:
                    sums[period] = np.maximum(0.0, sums[period] - rm)
                add_dt  = prev_dt - timedelta(minutes=15 * n_frames)
                add_arr = load_frame(add_dt.strftime("%Y%m%d%H%M"))
                if add_arr is not None:
                    sums[period] = sums[period] + add_arr

        if ts in already_done:
            print(f"  {ts} already done — skipping")
            continue

        poly_snap = {"ts": ts}
        for layer_name, layer_masks in masks.items():
            poly_snap[layer_name] = compute_poly_averages(sums, layer_masks)
        upload_poly_snapshot(r2, ts, poly_snap)
        uploaded += 1
        print(f"  {ts} uploaded  ({i + 1}/{steps})")

    print(f"\nDone — {uploaded} new poly snapshots uploaded.")


if __name__ == "__main__":
    main()
