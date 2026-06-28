"""
backfill_splats.py — Backfill splat mask PNGs for older runs in ukv_meta.json and upload to R2.
"""
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import fetch_ukv

def main():
    if not fetch_ukv.USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    s3 = fetch_ukv.get_s3()
    r2 = fetch_ukv.get_r2()

    print("Loading ukv_meta.json...")
    meta = fetch_ukv.json_from_r2(r2, "ukv_meta.json")
    if not meta or not meta.get("runs"):
        if os.path.exists("ukv_meta.json"):
            with open("ukv_meta.json") as f:
                meta = json.load(f)
    
    if not meta or not meta.get("runs"):
        print("No runs found in metadata.")
        sys.exit(1)

    runs = meta.get("runs", [])
    print(f"Total runs in meta: {len(runs)}")

    # We backfill the top 5 active runs independently
    target_runs = runs[:5]

    mapping = None

    for r_idx, run in enumerate(target_runs):
        run_ts = run.get("run_ts")
        steps = run.get("steps", [])
        print(f"\n[{r_idx+1}/{len(target_runs)}] Checking run {run_ts} ({len(steps)} steps)...")

        missing_splats = False
        for s in steps:
            if "splats" not in s or not s["splats"]:
                missing_splats = True
                break
        
        if not missing_splats:
            print(f"  Run {run_ts} already has splat metadata — skipping.")
            continue

        print(f"  Backfilling splats for run {run_ts}...")
        
        # Discover steps on S3 if needed
        s3_steps = fetch_ukv.discover_steps(s3, run_ts)
        if not s3_steps:
            print(f"  Warning: No steps found on S3 for run {run_ts}.")
            continue

        if mapping is None:
            print("  Building coordinate mapping...")
            first_hours, first_offset, first_valid = s3_steps[0]
            nc_bytes = fetch_ukv.download_nc(s3, run_ts, first_valid, first_offset, "rainfall_rate")
            if nc_bytes is None:
                print("  Cannot download mapping sample.")
                continue
            with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
                tmp.write(nc_bytes)
                sample_path = tmp.name
            try:
                mapping = fetch_ukv.build_mapping(sample_path)
            finally:
                os.unlink(sample_path)

        step_map = {s[1]: s for s in s3_steps} # offset -> (hours, offset, valid_ts)
        accum_stack = []

        for i, (hours, offset, valid_ts) in enumerate(s3_steps):
            # Find corresponding step dict in meta
            meta_step = next((s for s in steps if s.get("offset") == offset), None)
            if not meta_step:
                continue

            arr, arr_1h = fetch_ukv.load_arr_both(s3, run_ts, valid_ts, offset, mapping)
            if arr_1h is not None:
                accum_stack.append({"arr": arr_1h, "hours": 1})
            elif arr is not None:
                step_h = hours - s3_steps[i - 1][0] if i > 0 else hours
                accum_stack.append({"arr": arr * step_h, "hours": step_h})

            total_stk = sum(s["hours"] for s in accum_stack)
            while total_stk > 48 and accum_stack:
                total_stk -= accum_stack.pop(0)["hours"]

            accum_renders = []
            for n in fetch_ukv.ACCUM_PERIODS:
                if total_stk < n:
                    continue
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
                accum_renders.append((n, fetch_ukv.np.sum(arrs, axis=0).astype(fetch_ukv.np.float32)))

            splat_results = {}
            for n, arr_n in accum_renders:
                dur_key = f"{n}h"
                if dur_key in fetch_ukv.SPLAT_THRESHOLDS:
                    for th in fetch_ukv.SPLAT_THRESHOLDS[dur_key]:
                        th_str = f"{int(th)}mm" if th == int(th) else f"{th}mm"
                        splat_img = fetch_ukv.render_splat_png(arr_n, th)
                        skey = f"ukv/{run_ts}/{offset}_splat_{dur_key}_{th_str}.png"
                        fetch_ukv.png_to_r2(fetch_ukv._r2_tl(), skey, splat_img)
                        splat_results[f"{dur_key}_{th_str}"] = f"{fetch_ukv.R2_PUBLIC_URL}/{skey}"

            if splat_results:
                meta_step["splats"] = splat_results
                print(f"    {offset}: backfilled {len(splat_results)} splat thresholds")

    print("\nSaving updated ukv_meta.json...")
    with open("ukv_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    fetch_ukv.json_to_r2(r2, "ukv_meta.json", meta)
    print("Backfill complete!")

if __name__ == "__main__":
    main()
