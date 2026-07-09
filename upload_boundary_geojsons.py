#!/usr/bin/env python3
"""
Upload the static boundary GeoJSON layers (counties, regions, catchments,
rainfall-summary polygons, 20km grid) to R2 so the frontend can fetch them
from the CDN instead of GitHub Pages, cutting Pages bandwidth for these
large, rarely-changing files.

Run from GitHub Actions (has R2 secrets) whenever one of these files changes.
"""
import os

import boto3

R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET = os.environ.get('R2_BUCKET_NAME', '')

FILES = [
    "uk-counties.geojson",
    "uk_regions.geojson",
    "uk_catchments.geojson",
    "rainfallsummarypolygons.geojson",
    "uk_grid_20km.geojson",
]


def main():
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET]):
        raise SystemExit("Missing R2 credentials in environment")

    r2 = boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )

    for fname in FILES:
        if not os.path.exists(fname):
            print(f"  skip (not found): {fname}")
            continue
        key = f"geo/{fname}"
        r2.upload_file(fname, R2_BUCKET, key, ExtraArgs={'ContentType': 'application/geo+json'})
        print(f"  uploaded: {key}")


if __name__ == "__main__":
    main()
