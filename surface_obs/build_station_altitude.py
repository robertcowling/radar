#!/usr/bin/env python3
"""
build_station_altitude.py — One-off (re-run only when the station set
changes) script that samples a public DEM at each known station's lat/lon
and caches the result to station_altitude.json (sid -> altitude_m).

Why a DEM instead of CEDA MIDAS station metadata: MIDAS would also hand us
station names/WMO IDs "for free" and is the better long-term source, but it
requires a CEDA account (registration + credentials) that isn't available in
this environment. Elevation is all the MSLP reduction actually needs, so
this uses the public OpenTopoData API (no key required, ASTER 30m dataset)
instead — cheap, self-contained, and fine for a value that essentially never
changes for a fixed station network. Revisit with a MIDAS-matched station
list later if the station name / WMO ID gap becomes worth solving too.

Usage: python surface_obs/build_station_altitude.py [path/to/latest_obs.geojson]
Defaults to surface_obs/out/latest_obs.geojson (run build_snapshot.py first).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.opentopodata.org/v1/aster30m"
BATCH = 100
OUT_PATH = os.path.join(os.path.dirname(__file__), "station_altitude.json")


def fetch_batch(coords):
    locs = "|".join(f"{lat},{lon}" for lat, lon in coords)
    url = API + "?locations=" + urllib.parse.quote(locs, safe="|,.-")
    for attempt, delay in enumerate((0, 2, 4)):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read())
            return [res["elevation"] for res in data["results"]]
        except Exception as e:
            last = e
    raise last


import urllib.parse  # noqa: E402 (kept near use for clarity)


def main():
    snapshot_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "out", "latest_obs.geojson")
    with open(snapshot_path, encoding="utf-8") as f:
        snapshot = json.load(f)

    existing = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            existing = json.load(f)

    stations = []
    for feat in snapshot["features"]:
        sid = feat["properties"]["sid"]
        if sid in existing:
            continue
        lon, lat = feat["geometry"]["coordinates"]
        stations.append((sid, lat, lon))

    print(f"{len(existing)} altitude(s) already cached, {len(stations)} new station(s) to look up")
    if not stations:
        return

    for i in range(0, len(stations), BATCH):
        batch = stations[i:i + BATCH]
        coords = [(s[1], s[2]) for s in batch]
        elevations = fetch_batch(coords)
        for (sid, lat, lon), elev in zip(batch, elevations):
            existing[sid] = round(elev, 1) if elev is not None else None
        print(f"  Fetched {len(batch)} elevation(s) ({i + len(batch)}/{len(stations)})")
        time.sleep(1.1)   # public API courtesy rate limit (~1 req/s)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, sort_keys=True)
    print(f"Wrote {OUT_PATH} ({len(existing)} station(s) total)")


if __name__ == "__main__":
    main()
