"""One-time script: download GeoJSON files from R2 geo/ prefix to repo root."""
import os
import json
import boto3

R2_ACCOUNT_ID    = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_KEY    = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET        = os.environ.get('R2_BUCKET_NAME', '')


def get_r2():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name='auto',
    )


def main():
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET]):
        print("R2 credentials not set in environment. Aborting.")
        return

    r2 = get_r2()
    paginator = r2.get_paginator('list_objects_v2')
    found = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix='geo/'):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if not key.endswith('.geojson'):
                continue
            filename = os.path.basename(key)
            dest = filename
            print(f'Downloading {key} → {dest} ...')
            r2.download_file(R2_BUCKET, key, dest)
            size = os.path.getsize(dest)
            print(f'  Saved {size:,} bytes')
            found.append(dest)

            # Report first feature's property keys to help identify the name field
            with open(dest) as f:
                gj = json.load(f)
            features = gj.get('features', [])
            print(f'  {len(features)} features')
            if features:
                props = features[0].get('properties', {})
                print(f'  Property keys: {list(props.keys())}')
                print(f'  Sample values: { {k: v for k, v in list(props.items())[:5]} }')

    if not found:
        print('No .geojson files found under geo/ prefix.')
    else:
        print(f'\nDownloaded: {found}')
        print('Commit these files, then update GEOJSON_LAYERS in make_accum_multi.py if the name key differs.')


if __name__ == '__main__':
    main()
