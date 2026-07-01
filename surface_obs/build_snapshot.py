#!/usr/bin/env python3
"""
build_snapshot.py — Turns the parsed "latest per-station" table from
parse_obs.py into the public snapshot files:

  latest_obs.geojson   One Feature per station (Point geometry), full
                        parameter set — source of truth.
  latest_obs.min.json  Compact array-of-arrays form (header row + one row
                        per station) for smaller mobile payloads.

Both are uploaded to R2 alongside the radar tiles (surface_obs/ prefix),
plus an hourly archive snapshot (surface_obs/history/{hour}.geojson) — cheap
insurance per the spec's non-goals note, in case a historical archive is
wanted later without having to have started collecting it from day one.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from params import PARAMS
from parse_obs import load_raw_frames, build_latest

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

LATEST_GEOJSON_KEY = "surface_obs/latest_obs.geojson"
LATEST_MIN_KEY      = "surface_obs/latest_obs.min.json"
HISTORY_DIR         = "surface_obs/history"


def get_r2_client():
    if not USE_R2:
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    except ImportError:
        print("Warning: boto3 not installed — R2 upload skipped.")
        return None


def r2_put_json(r2, key, obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    r2.put_object(
        Bucket=R2_BUCKET, Key=key, Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache, max-age=0",
    )
    return len(body)


def _round(val, decimals):
    if decimals is None:
        return val
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return val


def station_to_properties(sid, station):
    props = {
        "sid": sid,
        "obs_time": station["obs_time"].strftime("%Y-%m-%dT%H:%M:00Z"),
        "stale": station["stale"],
    }
    if station.get("name"):
        props["name"] = station["name"]

    qc = {}
    for short_key, entry in station["params"].items():
        meta = PARAMS[short_key]
        val = entry["value"]
        if short_key == "present_wx":
            if not isinstance(val, str) or not val.strip():
                continue
            props[short_key] = val.strip()
        else:
            rounded = _round(val, meta["decimals"])
            if rounded is None or (isinstance(rounded, float) and rounded != rounded):  # NaN check
                continue
            props[short_key] = rounded
        qc[short_key] = entry["qc"].capitalize()
        # Per-parameter obs_time only when it differs from the station's newest.
        if entry["obs_time"] != station["obs_time"]:
            props.setdefault("param_obs_time", {})[short_key] = entry["obs_time"].strftime("%Y-%m-%dT%H:%M:00Z")

    if qc:
        props["qc"] = qc
    return props


def build_geojson(stations):
    features = []
    for sid, station in stations.items():
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [station["lon"], station["lat"]]},
            "properties": station_to_properties(sid, station),
        })
    return {
        "type": "FeatureCollection",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "Contains Met Office data © Crown copyright, licence CC BY-SA 4.0 "
                        "(https://creativecommons.org/licenses/by-sa/4.0/)",
        "count": len(features),
        "features": features,
    }


def build_min_json(geojson):
    """Compact array-of-arrays: header row of property keys actually used
    across the snapshot, then one row per station (lat, lon, then values in
    header order, null for missing)."""
    keys = []
    seen = set()
    for f in geojson["features"]:
        for k in f["properties"]:
            if k not in seen and k not in ("qc", "param_obs_time"):
                seen.add(k)
                keys.append(k)
    header = ["lat", "lon"] + keys
    rows = [header]
    for f in geojson["features"]:
        lon, lat = f["geometry"]["coordinates"]
        row = [lat, lon] + [f["properties"].get(k) for k in keys]
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-qc", default="Good", choices=["Good", "Suspect"],
                     help="Minimum QC class accepted for most parameters (default Good)")
    ap.add_argument("--allow-suspect", nargs="*", default=["rh_pct", "precip_rate_mmhr", "snow_cm"],
                     help="Short param keys that also accept Suspect-QC values (coverage over purity)")
    ap.add_argument("--freshness-minutes", type=int, default=120)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    df = load_raw_frames()
    print(f"Loaded {len(df)} raw row(s)")
    stations = build_latest(
        df,
        min_qc=args.min_qc,
        allow_suspect_params=args.allow_suspect,
        freshness_minutes=args.freshness_minutes,
    )
    print(f"Built latest snapshot for {len(stations)} station(s)")
    if not stations:
        print("No stations with QC-passing data — nothing to write. Not an error (e.g. between ingest cycles).")
        return

    geojson = build_geojson(stations)
    min_json = build_min_json(geojson)
    stale_count = sum(1 for f in geojson["features"] if f["properties"]["stale"])
    print(f"  {stale_count} stale station(s) (older than {args.freshness_minutes} min)")

    geojson_path = os.path.join(OUT_DIR, "latest_obs.geojson")
    min_path = os.path.join(OUT_DIR, "latest_obs.min.json")
    with open(geojson_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    with open(min_path, "w", encoding="utf-8") as f:
        json.dump(min_json, f, separators=(",", ":"))
    print(f"Wrote {geojson_path} and {min_path}")

    r2 = get_r2_client()
    if r2:
        n = r2_put_json(r2, LATEST_GEOJSON_KEY, geojson)
        print(f"Uploaded {LATEST_GEOJSON_KEY} ({n:,} bytes)")
        n = r2_put_json(r2, LATEST_MIN_KEY, min_json)
        print(f"Uploaded {LATEST_MIN_KEY} ({n:,} bytes)")

        # Cheap insurance: archive this run's snapshot under its generated hour,
        # in case a historical archive beyond "latest" is wanted later.
        hour_tag = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        archive_key = f"{HISTORY_DIR}/{hour_tag}.geojson"
        n = r2_put_json(r2, archive_key, geojson)
        print(f"Uploaded {archive_key} ({n:,} bytes)")
    else:
        print("R2 not configured — local files only.")


if __name__ == "__main__":
    main()
