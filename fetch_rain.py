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

# ── EA APIs ───────────────────────────────────────────────────────────────────
EA_BASE    = "https://environment.data.gov.uk/flood-monitoring"   # readings (real-time)
HYDRO_BASE = "https://environment.data.gov.uk/hydrology"          # stations + readings

# ── NRW API ───────────────────────────────────────────────────────────────────
NRW_BASE = "https://api.naturalresources.wales/rivers-and-seas/v1/api"
NRW_KEY  = os.environ.get("NRW_KEY", "")
USE_NRW  = bool(NRW_KEY)

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


def station_id_from_measure(uri, suid_to_ref=None):
    """Extract station ID from a measure URI.

    Hydrology API:    .../measures/{GUID}-rainfall-t-900-mm-qualified  → GUID lookup
    Flood monitoring: .../measures/52203-rainfall-tipping_bucket_...   → direct ref
    """
    suffix = uri.rsplit("/", 1)[-1]
    raw_id = suffix.split("-rainfall")[0]
    if suid_to_ref is not None:
        return suid_to_ref.get(raw_id)
    return raw_id


def dt_to_slot(dt_str):
    """Return (date_key 'YYYYMMDD', slot_idx 0-95) from an ISO-8601 UTC string."""
    dt = datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y%m%d"), (dt.hour * 60 + dt.minute) // 15


def fetch_stations():
    """Fetch stations from the Hydrology API (real place-name labels).
    Returns (stations dict keyed by stationReference, suid_to_ref lookup).
    """
    print("Fetching station list from Hydrology API...")
    url = f"{HYDRO_BASE}/id/stations.json?observedProperty=rainfall&_limit=10000"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    stations = {}
    suid_to_ref = {}
    for item in r.json().get("items", []):
        ref  = item.get("stationReference")
        guid = item.get("stationGuid")
        lat  = item.get("lat")
        lon  = item.get("long")
        if not ref or lat is None or lon is None:
            continue
        if guid:
            suid_to_ref[guid] = ref
        stations[ref] = {
            "lat":  round(float(lat), 5),
            "lon":  round(float(lon), 5),
            "name": item.get("label") or ref,
            "guid": guid,
        }
    print(f"  {len(stations)} stations with coordinates")
    return stations, suid_to_ref


def fetch_readings_for_date(date_str):
    """Fetch all 15-min rainfall readings for a YYYY-MM-DD date from Hydrology API.
    ~96k readings/day (1000 stations × 96 slots) — well within 200k limit.
    """
    url = (f"{HYDRO_BASE}/data/readings.json"
           f"?observedProperty=rainfall&period=900&date={date_str}&_limit=200000")
    print(f"  Fetching readings for {date_str}...")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    items = r.json().get("items", [])
    print(f"    {len(items)} readings")
    return items


def fetch_nrw_data():
    """Fetch latest rainfall readings from NRW StationData API.
    Returns (nrw_stations dict, by_date dict, latest_dt str or None).
    Station IDs are prefixed 'nrw_' to avoid EA collisions.
    """
    if not USE_NRW:
        return {}, {}, None
    print("Fetching NRW station data...")
    r = requests.get(f"{NRW_BASE}/StationData",
                     headers={"Ocp-Apim-Subscription-Key": NRW_KEY},
                     timeout=60)
    r.raise_for_status()
    items = r.json()

    nrw_stations = {}
    by_date = {}
    latest_dt = None

    for item in items:
        lat = item.get("latitude")
        lon = item.get("longitude")
        if lat is None or lon is None:
            continue
        sid  = f"nrw_{item.get('stationId')}"
        name = item.get("nameEN") or sid
        for param in (item.get("parameters") or []):
            if "rainfall" not in (param.get("paramNameEN") or "").lower():
                continue
            val_str  = param.get("latestValue")
            time_str = (param.get("latestTime") or "")
            if val_str is None or not time_str:
                continue
            try:
                val = float(val_str)
            except (TypeError, ValueError):
                continue
            if val < 0:
                continue
            nrw_stations[sid] = {
                "lat":    round(float(lat), 5),
                "lon":    round(float(lon), 5),
                "name":   name,
                "source": "nrw",
            }
            date_key, slot = dt_to_slot(time_str[:19].rstrip("Z"))
            by_date.setdefault(date_key, {}).setdefault(sid, {})[str(slot)] = round(val, 2)
            if latest_dt is None or time_str > latest_dt:
                latest_dt = time_str
            break  # one rainfall param per station

    print(f"  NRW: {len(nrw_stations)} rainfall stations, latest={latest_dt}")
    return nrw_stations, by_date, latest_dt


def parse_readings(items, suid_to_ref=None):
    """Group readings by (date_key → station_id → slot_str → value).
    Returns (by_date dict, latest_dateTime str or None).
    suid_to_ref: GUID→stationReference lookup for Hydrology API readings;
                 pass None when parsing flood-monitoring CSVs (backfill).
    """
    by_date = {}
    latest_dt = None
    for item in items:
        dt_str  = item.get("dateTime", "")
        raw_val = item.get("value")
        quality = item.get("quality", "")
        measure = item.get("measure", "")
        if isinstance(measure, dict):
            measure = measure.get("@id", "")
        if not dt_str or raw_val is None or quality == "Missing":
            continue
        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            continue
        if val < 0:
            continue
        station = station_id_from_measure(measure, suid_to_ref)
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
        stations, suid_to_ref = fetch_stations()
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
        suid_to_ref = {s["guid"]: ref for ref, s in stations.items() if s.get("guid")}

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

    by_date, latest_dt = parse_readings(items, suid_to_ref)

    # ── NRW readings (merged in before upload loop) ───────────────────────────
    if USE_NRW:
        try:
            nrw_stations_new, nrw_by_date, nrw_latest = fetch_nrw_data()
            for date_key, station_slots in nrw_by_date.items():
                for sid, slots in station_slots.items():
                    by_date.setdefault(date_key, {}).setdefault(sid, {}).update(slots)
            if nrw_latest and (latest_dt is None or nrw_latest > latest_dt):
                latest_dt = nrw_latest
            if nrw_stations_new:
                stations.update(nrw_stations_new)
                stations_data["stations"] = stations
                write_local_json(STATIONS_PATH, stations_data)
                if USE_R2:
                    try:
                        r2_put_json(r2, "rain/stations.json", stations_data)
                        print(f"Updated stations.json with {len(nrw_stations_new)} NRW stations")
                    except Exception as e:
                        print(f"Warning: stations.json NRW update failed: {e}")
        except Exception as e:
            print(f"Warning: NRW fetch failed: {e}")

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
        "latest_time": latest_dt or meta.get("latest_time", ""),
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
