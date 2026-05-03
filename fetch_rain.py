#!/usr/bin/env python3
"""
fetch_rain.py — Incremental EA rainfall data fetcher.

Fetches 15-min rainfall readings from the Environment Agency Real Time API,
merges them into daily JSON files in Cloudflare R2, and updates rain/meta.json
(which is also committed to the repo).

Called every 15 min by rain_update.yml. Importable by backfill_rain.py.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import requests

# ── EA API ────────────────────────────────────────────────────────────────────
EA_BASE = "https://environment.data.gov.uk/flood-monitoring"

# ── Cloudflare R2 ─────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

RETENTION_DAYS  = 31
META_PATH       = "rain/meta.json"
STATIONS_PATH   = "rain/stations.json"
R2_READINGS_PFX = "rain/readings"


def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def r2_get_json(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")


def station_id_from_measure(uri):
    """Extract stationReference from a measure URI.
    e.g. '.../measures/52203-rainfall-tipping_bucket_raingauge-t-15_min-mm' → '52203'
    """
    suffix = uri.rsplit("/", 1)[-1]
    return suffix.split("-rainfall")[0]


def dt_to_slot(dt_str):
    """Return (date_key 'YYYYMMDD', slot_idx 0-95) from an ISO-8601 UTC string."""
    dt = datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y%m%d"), (dt.hour * 60 + dt.minute) // 15


def fetch_stations():
    print("Fetching station list from EA API...")
    url = f"{EA_BASE}/id/stations?parameter=rainfall&_limit=10000"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    stations = {}
    for item in r.json().get("items", []):
        ref = item.get("stationReference") or item.get("notation")
        lat = item.get("lat")
        lon = item.get("long")
        if not ref or lat is None or lon is None:
            continue
        stations[ref] = {
            "lat": round(float(lat), 5),
            "lon": round(float(lon), 5),
            "name": item.get("label", ref),
        }
    print(f"  {len(stations)} stations with coordinates")
    return stations


def fetch_readings_for_date(date_str):
    """Fetch all readings for a YYYY-MM-DD date.
    The ?date= filter has no default limit on the EA API, so all readings for
    the day are returned in one response. No pagination needed.
    """
    url = f"{EA_BASE}/data/readings?parameter=rainfall&date={date_str}"
    print(f"  Fetching readings for {date_str}...")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    items = r.json().get("items", [])
    print(f"    {len(items)} readings")
    return items


def parse_readings(items):
    """Group readings by (date_key → station_id → slot_str → value).
    Returns (by_date dict, latest_dateTime str or None).
    """
    by_date = {}
    latest_dt = None
    for item in items:
        dt_str  = item.get("dateTime", "")
        raw_val = item.get("value")
        measure = item.get("measure", "")
        if isinstance(measure, dict):
            measure = measure.get("@id", "")
        if not dt_str or raw_val is None:
            continue
        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            continue
        if val < 0:
            continue  # EA uses negative sentinels for missing data
        station = station_id_from_measure(measure)
        if not station:
            continue
        date_key, slot = dt_to_slot(dt_str)
        by_date.setdefault(date_key, {}).setdefault(station, {})[str(slot)] = round(val, 2)
        if latest_dt is None or dt_str > latest_dt:
            latest_dt = dt_str
    return by_date, latest_dt


def load_local_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_local_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def main():
    now = datetime.now(timezone.utc)
    os.makedirs("rain", exist_ok=True)

    r2 = get_r2() if USE_R2 else None
    if not USE_R2:
        print("Warning: R2 env vars not set — writing local files only (dry run)")

    # ── Station metadata ───────────────────────────────────────────────────────
    stations_data = load_local_json(STATIONS_PATH, {})
    stations = stations_data.get("stations", {})
    if len(stations) < 100:
        stations = fetch_stations()
        stations_data = {
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stations": stations,
        }
        write_local_json(STATIONS_PATH, stations_data)
        if USE_R2:
            try:
                r2_put_json(r2, "rain/stations.json", stations_data)
                print(f"Uploaded stations.json ({len(stations)} stations)")
            except Exception as e:
                print(f"Warning: stations.json upload failed: {e}")
    else:
        print(f"Using cached stations ({len(stations)} stations)")

    # ── Load current meta ──────────────────────────────────────────────────────
    meta = load_local_json(META_PATH, {
        "generated_at": "", "latest_time": "", "available_days": [], "r2_base_url": ""
    })

    # ── Fetch today's readings (and yesterday's in the first 30 min of the day) ─
    # The EA API only supports ?since= at the station/measure level, not globally.
    # Using ?date= returns all readings for that day with no default limit.
    today_str     = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        items = fetch_readings_for_date(today_str)
        if now.hour == 0 and now.minute < 30:
            items += fetch_readings_for_date(yesterday_str)
    except Exception as e:
        print(f"Error fetching readings: {e}")
        sys.exit(1)

    if not items:
        print("No readings returned — updating generated_at only")
        meta["generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        meta["r2_base_url"] = R2_PUBLIC_URL
        write_local_json(META_PATH, meta)
        if USE_R2:
            r2_put_json(r2, "rain/meta.json", meta)
        return

    by_date, latest_dt = parse_readings(items)

    # ── Merge into R2 day files ───────────────────────────────────────────────
    available_days = set(meta.get("available_days", []))
    for date_key, new_readings in sorted(by_date.items()):
        r2_key = f"{R2_READINGS_PFX}/{date_key}.json"
        if USE_R2:
            existing = r2_get_json(r2, r2_key, {"date": date_key, "readings": {}})
        else:
            existing = {"date": date_key, "readings": {}}

        merged = existing.get("readings", {})
        for station, slots in new_readings.items():
            if station not in merged:
                merged[station] = {}
            merged[station].update(slots)

        day_obj = {"date": date_key, "readings": merged}
        if USE_R2:
            try:
                r2_put_json(r2, r2_key, day_obj)
                print(f"  Uploaded {r2_key} ({len(merged)} stations)")
            except Exception as e:
                print(f"  Warning: {r2_key} upload failed: {e}")
        else:
            print(f"  (dry run) would upload {r2_key} ({len(merged)} stations)")
        available_days.add(date_key)

    # ── Prune days beyond retention window ────────────────────────────────────
    cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
    old_days = sorted(d for d in available_days if d < cutoff)
    for d in old_days:
        if USE_R2:
            try:
                r2.delete_object(Bucket=R2_BUCKET, Key=f"{R2_READINGS_PFX}/{d}.json")
                print(f"  Deleted old day: {d}")
            except Exception as e:
                print(f"  Warning: could not delete {d}: {e}")
        available_days.discard(d)

    # ── Write updated meta ────────────────────────────────────────────────────
    meta = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest_time": latest_dt or last_time,
        "available_days": sorted(available_days),
        "r2_base_url": R2_PUBLIC_URL,
    }
    write_local_json(META_PATH, meta)
    if USE_R2:
        try:
            r2_put_json(r2, "rain/meta.json", meta)
        except Exception as e:
            print(f"Warning: meta.json upload failed: {e}")
    print(f"Done. {len(available_days)} days, latest={meta['latest_time']}")


if __name__ == "__main__":
    main()
