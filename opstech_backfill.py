"""One-shot backfill: render every 22-23 June 2026 radar frame into R2.

Re-renders the Met Office ODIM H5 composites (still on the public
met-office-radar-obs-data bucket) with the same mapping/palette as the
live parallel feed, uploads the PNGs to the permanent opstech/ prefix on
R2 (no retention job touches it), and writes opstech/frames.json for the
/opstech archive page. Run once from the opstech_backfill workflow, then
both the workflow and this script can be deleted.
"""

import json
import os
from datetime import datetime

import boto3
from botocore import UNSIGNED
from botocore.client import Config

from process_parallel import (
    BUCKET,
    R2_BUCKET,
    R2_PUBLIC_URL,
    USE_R2,
    get_mapping,
    get_r2_client,
    list_s3_keys_for_date,
    render_png,
    upload_to_r2,
)

DATES = [datetime(2026, 6, 22), datetime(2026, 6, 23)]
R2_PREFIX = "opstech"
H5_DIR = "data_h5_opstech"
PNG_DIR = "static/opstech"
MANIFEST = "opstech/frames.json"


def list_opstech_r2_keys(r2):
    keys = set()
    paginator = r2.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"{R2_PREFIX}/"):
        for obj in page.get('Contents', []):
            keys.add(obj['Key'])
    print(f"R2: {len(keys)} existing {R2_PREFIX}/ objects found.")
    return keys


def main():
    if not USE_R2:
        raise SystemExit("R2 credentials not configured — this backfill only makes sense against R2.")

    os.makedirs(H5_DIR, exist_ok=True)
    os.makedirs(PNG_DIR, exist_ok=True)

    s3 = boto3.client("s3", region_name="eu-west-2", config=Config(signature_version=UNSIGNED))
    r2 = get_r2_client()
    r2_keys = list_opstech_r2_keys(r2)

    keys = []
    for date in DATES:
        day_keys = list_s3_keys_for_date(s3, date)
        print(f"{date:%Y-%m-%d}: {len(day_keys)} composites in source bucket.")
        keys.extend(day_keys)
    keys.sort()

    mapping = None
    frames = []

    for key in keys:
        h5_name = os.path.basename(key)
        h5_path = os.path.join(H5_DIR, h5_name)
        png_name = h5_name.replace(".h5", ".png")
        png_path = os.path.join(PNG_DIR, png_name)
        r2_key = f"{R2_PREFIX}/{png_name}"

        try:
            dt = datetime.strptime(h5_name.split('_')[0], "%Y%m%d%H%M")
        except ValueError:
            print(f"Skipping unparseable key: {h5_name}")
            continue

        if r2_key in r2_keys:
            print(f"Skip (already in R2): {png_name}")
        else:
            if not os.path.exists(png_path):
                print(f"Downloading {h5_name}...")
                s3.download_file(BUCKET, key, h5_path)
                if mapping is None:
                    mapping = get_mapping(h5_path)
                print(f"Rendering {png_name}...")
                render_png(h5_path, png_path, mapping)
                os.remove(h5_path)
            if not upload_to_r2(r2, png_path, r2_key, "image/png", r2_keys):
                raise SystemExit(f"Upload failed for {r2_key} — aborting so the manifest stays honest.")

        frames.append({
            "time": dt.strftime("%a %d %b %Y %H:%M UTC"),
            "url": f"{R2_PUBLIC_URL}/{r2_key}",
        })

    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(frames, f, indent=2)
    print(f"Manifest {MANIFEST} written with {len(frames)} frames.")


if __name__ == "__main__":
    main()
