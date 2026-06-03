#!/usr/bin/env python3
import os
import json
import urllib.request
import math
from datetime import datetime, timedelta, timezone
import argparse

# Variables to fetch from the COSMOS-UK 1D API
# key: API field name, stored_as: key in output JSON
VARS = [
    {'api': 'cosmos_vwc',   'key': 'vwc'},
    {'api': 'precip',       'key': 'precip'},
    {'api': 'ta',           'key': 'ta'},
    {'api': 'stp_tsoil10',  'key': 'tsoil'},
]

def fetch_data(days=14):
    print("Fetching COSMOS-UK site locations...")
    req = urllib.request.Request("https://cosmos-api.ceh.ac.uk/collections/1D/locations")
    with urllib.request.urlopen(req) as response:
        locations_data = json.loads(response.read())

    sites = {}
    for feature in locations_data.get('features', []):
        site_id = feature['id']
        lat, lon = feature['geometry']['coordinates']
        name = feature['properties'].get('label', site_id)
        sites[site_id] = {
            'lat': lat,
            'lon': lon,
            'name': name
        }

    os.makedirs('cosmos', exist_ok=True)

    with open('cosmos/stations.json', 'w') as f:
        json.dump(sites, f, indent=2)

    frames = []
    now = datetime.now(timezone.utc)

    for i in range(days, -1, -1):
        check_date = now - timedelta(days=i)
        date_str = check_date.strftime("%Y-%m-%dT00:00:00Z")
        url = f"https://cosmos-api.ceh.ac.uk/collections/1D/cube?bbox=-9.0,49.75,2.01,61.01&datetime={date_str}"
        print(f"Trying: {url}")

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as response:
                cube_data = json.loads(response.read())

            readings = {}
            for coverage in cube_data.get('coverages', []):
                try:
                    lon = coverage['domain']['axes']['x']['values'][0]
                    lat = coverage['domain']['axes']['y']['values'][0]

                    site_id = None
                    for sid, s in sites.items():
                        if abs(s['lon'] - lon) < 0.01 and abs(s['lat'] - lat) < 0.01:
                            site_id = sid
                            break

                    if not site_id:
                        continue

                    entry = {}
                    for v in VARS:
                        api_key = v['api']
                        out_key = v['key']
                        if api_key in coverage['ranges']:
                            vals = coverage['ranges'][api_key]['values']
                            if vals and vals[0] is not None:
                                entry[out_key] = round(vals[0], 2)

                    # Only store if we got at least one variable
                    if entry:
                        readings[site_id] = entry

                except Exception:
                    pass

            if readings:
                print(f"Got readings for {len(readings)} sites for {date_str}")
                date_key = check_date.strftime("%Y%m%d")
                with open(f'cosmos/{date_key}.json', 'w') as f:
                    json.dump({'date': date_str, 'readings': readings}, f, separators=(',', ':'))

                frames.append({
                    'time': check_date.strftime("%a %d %b %Y 00:00 UTC"),
                    'url': f'{date_key}.json',
                    'date_key': date_key
                })

        except Exception as e:
            print(f"Failed: {e}")

    if frames:
        with open('cosmos/meta.json', 'w') as f:
            json.dump({
                'frames': frames,
                'latest_time': frames[-1]['time']
            }, f, indent=2)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=14)
    args = parser.parse_args()
    fetch_data(args.days)
