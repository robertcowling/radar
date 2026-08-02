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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from math import atan2, cos, radians, sin, sqrt

import boto3
import requests

# ── EA APIs ───────────────────────────────────────────────────────────────────
EA_BASE    = "https://environment.data.gov.uk/flood-monitoring"   # readings (real-time)
HYDRO_BASE = "https://environment.data.gov.uk/hydrology"          # stations + readings

# ── NRW API ───────────────────────────────────────────────────────────────────
NRW_RIVERS_BASE   = "https://api.naturalresources.wales/rivers-and-seas/v1/api"
NRW_TELEMETRY_BASE = "https://api.naturalresources.wales/telemetry/api"
NRW_KEY  = os.environ.get("NRW_KEY", "")
USE_NRW  = bool(NRW_KEY)

# ── SEPA API ──────────────────────────────────────────────────────────────────
SEPA_BASE = "https://www2.sepa.org.uk/rainfall"   # open data, no key required

# ── Cloudflare R2 ─────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

RETENTION_DAYS  = 14
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
    """Fetch JSON from R2. Returns (data, success_bool).
    success_bool is False only if a network/read exception occurred.
    """
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read()), True
    except Exception as e:
        err_code = ""
        if hasattr(e, "response") and isinstance(e.response, dict):
            err_code = e.response.get("Error", {}).get("Code", "")
        if err_code in ("NoSuchKey", "404", "NoSuchBucket"):
            return default, True
        print(f"Warning: R2 read error for {key}: {e}")
        return default, False


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
    """Fetch NRW rainfall data via the Telemetry API (confirmed 15-min cadence).

    Endpoints used:
      GET /telemetry/api/stations            → lat/lon/name per station reference
      GET /telemetry/api/measures            → rainfall measure_ids per station
      GET /telemetry/api/measures/readings   → bulk 15-min readings for last 49hr

    All field names confirmed against live API responses 2026-05-06.
    Station IDs are prefixed 'nrw_' to avoid EA collisions.
    Returns (nrw_stations dict, by_date dict, latest_dt str or None).
    """
    if not USE_NRW:
        return {}, {}, None

    hdrs = {"Ocp-Apim-Subscription-Key": NRW_KEY}
    nrw_stations = {}
    by_date = {}
    latest_dt = None

    # ── Step 1: station lat/lon/name lookup ────────────────────────────────────
    print("Fetching NRW stations from Telemetry API...")
    station_meta = {}   # monitoring_station_reference → {lat, lon, name}
    try:
        rs = requests.get(f"{NRW_TELEMETRY_BASE}/stations", headers=hdrs, timeout=30)
        rs.raise_for_status()
        sdata = rs.json()
        if not isinstance(sdata, list):
            sdata = sdata.get("items") or sdata.get("value") or []
        for s in sdata:
            ref = s.get("monitoring_station_reference", "")
            lat = s.get("latitude")
            lon = s.get("longitude")
            name = s.get("monitoring_station_name_en") or ref
            if ref and lat is not None and lon is not None:
                station_meta[ref] = {
                    "lat":  round(float(lat), 5),
                    "lon":  round(float(lon), 5),
                    "name": name,
                }
        print(f"  {len(station_meta)} stations with coordinates")
    except Exception as e:
        print(f"  Warning: Telemetry /stations failed: {e}")

    # ── Step 2: measures — build measure_id → station_ref map ─────────────────
    print("Fetching NRW measures from Telemetry API...")
    measure_to_sid = {}   # measure_id string → "nrw_<ref>"
    try:
        rm = requests.get(f"{NRW_TELEMETRY_BASE}/measures", headers=hdrs, timeout=30)
        rm.raise_for_status()
        mdata = rm.json()
        if not isinstance(mdata, list):
            mdata = mdata.get("items") or mdata.get("value") or []
        for m in mdata:
            # Filter: measure_type_en == "Rainfall"
            if m.get("measure_type_en", "").lower() != "rainfall":
                continue
            mid = m.get("measure_id", "")
            ref = m.get("monitoring_station_reference", "")
            if not mid or not ref:
                continue
            sid = f"nrw_{ref}"
            measure_to_sid[mid] = sid
            # Register station (use coords from step 1 if available)
            meta = station_meta.get(ref, {})
            if meta:
                nrw_stations[sid] = {
                    "lat":    meta["lat"],
                    "lon":    meta["lon"],
                    "name":   meta["name"],
                    "source": "nrw",
                }
            # Ingest latest_value as a fallback data point
            lt = (m.get("latest_time") or "").replace(".000Z", "").replace("Z", "").strip()
            lv = m.get("latest_value")
            if lt and lv is not None:
                try:
                    lv_f = float(lv)
                    if lv_f >= 0:
                        # timestamp format: "2026-05-06 19:45:00"
                        lt_clean = lt.replace(" ", "T")
                        dk, slot = dt_to_slot(lt_clean)
                        by_date.setdefault(dk, {}).setdefault(sid, {})[str(slot)] = round(lv_f, 2)
                        if latest_dt is None or lt_clean > latest_dt:
                            latest_dt = lt_clean
                except (TypeError, ValueError):
                    pass
        print(f"  {len(measure_to_sid)} rainfall measures at {len(nrw_stations)} stations")
    except Exception as e:
        print(f"  Warning: Telemetry /measures failed: {e}")

    if not measure_to_sid:
        print("  No rainfall measures found — NRW data unavailable")
        return nrw_stations, by_date, latest_dt

    # ── Step 3: bulk readings — smart chunked fetch ───────────────────────────
    # API hard limit: max 24hr historical, max ~12hr window per call.
    # Normal runs (every 15min): fetch last 1hr only (1 call, catches new readings).
    # Top-of-hour runs: also fetch 1-7hr ago to catch any delayed readings.
    now = datetime.now(timezone.utc)
    chunks_to_fetch = [(0, 1)]   # always: last 1 hour
    if now.minute < 20:          # near top of hour: add catch-up window
        chunks_to_fetch.append((1, 7))
    count = 0
    for (hrs_ago_end, hrs_ago_start) in chunks_to_fetch:
        chunk_end   = now - timedelta(hours=hrs_ago_end)
        chunk_start = now - timedelta(hours=hrs_ago_start)

        start_str = chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str   = chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  NRW fetch: {start_str} -> {end_str}")
        try:
            rr = requests.get(
                f"{NRW_TELEMETRY_BASE}/measures/readings",
                params={"start": start_str, "end": end_str},
                headers=hdrs,
                timeout=60,
            )
            rr.raise_for_status()
            readings = rr.json()
            if not isinstance(readings, list):
                readings = readings.get("items") or readings.get("value") or []
            for rec in readings:
                mid = rec.get("measureId") or rec.get("measure_id") or ""
                sid = measure_to_sid.get(mid)
                if not sid:
                    continue
                # timestamp: "2026-05-06 17:00:00" (space separator)
                ts = (rec.get("timestamp") or "").replace(" ", "T").replace(".000Z", "").rstrip("Z")
                v  = rec.get("value")
                if not ts or v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if v < 0:
                    continue
                dk, slot = dt_to_slot(ts)
                by_date.setdefault(dk, {}).setdefault(sid, {})[str(slot)] = round(v, 2)
                if latest_dt is None or ts > latest_dt:
                    latest_dt = ts
                count += 1
        except Exception as e:
            print(f"    Warning: chunk {chunk+1} failed: {e}")
    print(f"  Telemetry bulk readings: {count} readings ingested")


    _dedup_nrw_numeric(nrw_stations, by_date)
    print(f"  NRW total: {len(nrw_stations)} stations, latest={latest_dt}")
    return nrw_stations, by_date, latest_dt


def fetch_sepa_data():
    """Fetch SEPA (Scottish) rainfall data via the public Rainfall Data API.

      GET /api/Stations         → lat/lon/name per station (single bulk call)
      GET /api/Hourly/{ref}     → that station's own discrete hourly totals
                                   (not a running cumulative count), covering
                                   a rolling ~3-4 day window.

    SEPA has already done the differencing for us, so there's no cumulative
    counter to track and no reset to detect — we just take its hourly totals
    as-is. Re-fetching the rolling window every run and overwriting the same
    slots each time is idempotent and self-heals any bad values a prior run
    may have written.

    Station IDs are prefixed 'sepa_' to avoid collisions with EA/NRW.
    Returns (sepa_stations dict, by_date dict, latest_dt str or None,
    covered_days set of 'YYYYMMDD' strings this run has authoritative data for).
    """
    sepa_stations = {}
    by_date = {}
    latest_dt = None

    print("Fetching SEPA stations from Rainfall Data API...")
    try:
        r = requests.get(f"{SEPA_BASE}/api/Stations", timeout=30)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            items = items.get("items") or items.get("value") or []
    except Exception as e:
        print(f"  Warning: SEPA /api/Stations failed: {e}")
        return sepa_stations, by_date, latest_dt, set()

    refs = []
    for item in items:
        ref = item.get("station_no")
        lat = item.get("station_latitude")
        lon = item.get("station_longitude")
        if not ref or lat is None or lon is None:
            continue
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError):
            continue

        sid = f"sepa_{ref}"
        sepa_stations[sid] = {
            "lat":    round(lat_f, 5),
            "lon":    round(lon_f, 5),
            "name":   item.get("station_name") or ref,
            "source": "sepa",
        }
        refs.append((sid, ref))

    session = requests.Session()
    session.mount(SEPA_BASE, requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32))

    def fetch_hourly(sid_ref):
        sid, ref = sid_ref
        try:
            r = session.get(f"{SEPA_BASE}/api/Hourly/{ref}", timeout=20)
            r.raise_for_status()
            return sid, r.json()
        except Exception as e:
            print(f"  Warning: SEPA Hourly fetch failed for {ref}: {e}")
            return sid, None

    covered_days = set()  # every date this run's window actually reports on,
                           # including zero-rain hours — used to safely wipe
                           # stale/corrupt slots that a dry hour won't overwrite.

    print(f"  Fetching hourly totals for {len(refs)} SEPA stations...")
    with ThreadPoolExecutor(max_workers=32) as pool:
        for sid, records in pool.map(fetch_hourly, refs):
            for rec in records or []:
                ts = (rec.get("Timestamp") or "").strip()
                try:
                    dt = datetime.strptime(ts, "%d/%m/%Y %H:%M:%S")
                except ValueError:
                    continue
                dt_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
                dk, slot = dt_to_slot(dt_str)
                covered_days.add(dk)

                try:
                    v = float(rec.get("Value"))
                except (TypeError, ValueError):
                    continue
                # Keep genuine zero (dry-hour) readings — only reject negatives,
                # matching EA/NRW parsing. Dropping zeros here made a dry hour and
                # an offline gauge indistinguishable in storage (absent slot).
                if v < 0:
                    continue
                by_date.setdefault(dk, {}).setdefault(sid, {})[str(slot)] = round(v, 2)
                if latest_dt is None or dt_str > latest_dt:
                    latest_dt = dt_str

    print(f"  SEPA total: {len(sepa_stations)} stations, latest={latest_dt}")
    return sepa_stations, by_date, latest_dt, covered_days


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


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _dedup_nrw_numeric(stations, by_date=None, threshold_km=3.0):
    """Remove nrw_NNNN (legacy numeric-ID) entries that have a nearby nrw_H...
    counterpart within threshold_km.  Stations with no H-prefix partner are kept.
    Operates in-place; also prunes matching keys from by_date if supplied."""
    numeric = [sid for sid in stations if sid.startswith("nrw_") and sid[4:].isdigit()]
    h_list  = [(sid, s) for sid, s in stations.items() if sid.startswith("nrw_H")]
    if not numeric or not h_list:
        return

    to_remove = set()
    for sid in numeric:
        s = stations[sid]
        for _, hs in h_list:
            if _haversine_km(s["lat"], s["lon"], hs["lat"], hs["lon"]) <= threshold_km:
                to_remove.add(sid)
                break

    for sid in to_remove:
        del stations[sid]
    if by_date:
        for slots in by_date.values():
            for sid in to_remove:
                slots.pop(sid, None)
    if to_remove:
        print(f"  NRW dedup: removed {len(to_remove)} legacy numeric-ID stations")


def main():
    now = datetime.now(timezone.utc)
    os.makedirs("rain", exist_ok=True)

    r2 = get_r2() if USE_R2 else None
    if not USE_R2:
        print("Warning: R2 env vars not set — writing local files only (dry run)")

    # ── Station metadata ───────────────────────────────────────────────────────
    stations_data = None
    if USE_R2:
        try:
            stations_data, get_ok = r2_get_json(r2, "rain/stations.json", None)
            if get_ok and stations_data:
                print(f"Loaded stations.json from R2 ({len(stations_data.get('stations', {}))} stations)")
        except Exception as e:
            print(f"Warning: failed to load stations.json from R2: {e}")
    if not stations_data:
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
        # Clean up legacy numeric NRW stations that have an H-prefix counterpart
        before = len(stations)
        _dedup_nrw_numeric(stations)
        if len(stations) != before:
            stations_data["stations"] = stations
            write_local_json(STATIONS_PATH, stations_data)

    # ── Load current meta ──────────────────────────────────────────────────────
    meta = load_local_json(META_PATH, {
        "generated_at": "", "latest_time": "", "available_days": [], "r2_base_url": ""
    })

    # ── Fetch today's readings (and yesterday's if missing or degraded) ──────────
    today_str     = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_key = (now - timedelta(days=1)).strftime("%Y%m%d")

    # EA does not publish every station's readings in real time — Wick St
    # Lawrence (52223) was measured empirically to lag by upwards of two
    # hours on some evenings, so a `date=yesterday` query made right at
    # midnight can still be missing the tail of the day. A single catch-up
    # poll at 00:00-00:30 only closes that gap if EA's lag that night happens
    # to be under 30 minutes. Re-querying yesterday on every run for the
    # first three hours of the new day gives EA up to 3 hours to publish the
    # rest, comfortably past the worst lag observed; each extra run is one
    # lightweight date-scoped request, not a per-station pull.
    fetch_yesterday = False
    if now.hour < 3:
        fetch_yesterday = True
    elif USE_R2:
        r2_key_yest = f"{R2_READINGS_PFX}/{yesterday_key}.json"
        existing_yest, yest_ok = r2_get_json(r2, r2_key_yest, None)
        if yest_ok and (not existing_yest or len(existing_yest.get("readings", {})) < 100):
            print(f"Yesterday's archive ({yesterday_key}) missing or degraded in R2 — triggering self-healing fetch.")
            fetch_yesterday = True

    try:
        items = fetch_readings_for_date(today_str)
        if fetch_yesterday:
            print(f"  Fetching full archive readings for yesterday ({yesterday_str})...")
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

    # ── SEPA readings (merged in before upload loop) ──────────────────────────
    sepa_station_ids = set()
    sepa_covered_days = set()
    try:
        sepa_stations_new, sepa_by_date, sepa_latest, sepa_covered_days = fetch_sepa_data()
        sepa_station_ids = set(sepa_stations_new)
        for date_key, station_slots in sepa_by_date.items():
            for sid, slots in station_slots.items():
                by_date.setdefault(date_key, {}).setdefault(sid, {}).update(slots)
        if sepa_latest and (latest_dt is None or sepa_latest > latest_dt):
            latest_dt = sepa_latest
        if sepa_stations_new:
            stations.update(sepa_stations_new)
            stations_data["stations"] = stations
            write_local_json(STATIONS_PATH, stations_data)
            if USE_R2:
                try:
                    r2_put_json(r2, "rain/stations.json", stations_data)
                    print(f"Updated stations.json with {len(sepa_stations_new)} SEPA stations")
                except Exception as e:
                    print(f"Warning: stations.json SEPA update failed: {e}")
    except Exception as e:
        print(f"Warning: SEPA fetch failed: {e}")

    # ── Merge into R2 day files ───────────────────────────────────────────────
    available_days = set(meta.get("available_days", []))
    for date_key, new_readings in sorted(by_date.items()):
        r2_key = f"{R2_READINGS_PFX}/{date_key}.json"
        if USE_R2:
            existing, get_ok = r2_get_json(r2, r2_key, {"date": date_key, "readings": {}})
            if not get_ok:
                print(f"  Warning: Skipping update for {r2_key} due to R2 read error (protecting existing historical data).")
                continue
        else:
            existing = {"date": date_key, "readings": {}}

        merged = existing.get("readings", {})
        if date_key in sepa_covered_days:
            # SEPA's hourly fetch is a complete, authoritative snapshot of
            # this day's window (not an incremental delta like EA/NRW), so
            # clear each station's old slots first — otherwise a corrected
            # dry hour (no new slot) could never overwrite a stale bad value.
            for sid in sepa_station_ids:
                merged.pop(sid, None)
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
    # Drop old entries from meta tracking
    old_days = sorted(d for d in available_days if d < cutoff)
    for d in old_days:
        available_days.discard(d)
    # Scan R2 directly so orphaned objects not in meta are also removed.
    # The scan needs a LIST (Class A per page) and day files only cross the
    # retention cutoff once a day, so only scan on the first run of each hour
    # rather than every 10-minute invocation.
    if USE_R2 and now.minute < 10:
        try:
            paginator = r2.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=R2_READINGS_PFX + "/"):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    fname = key.rsplit("/", 1)[-1].replace(".json", "")
                    if len(fname) == 8 and fname.isdigit() and fname < cutoff:
                        try:
                            r2.delete_object(Bucket=R2_BUCKET, Key=key)
                            print(f"  Deleted old R2 object: {key}")
                        except Exception as e:
                            print(f"  Warning: could not delete {key}: {e}")
        except Exception as e:
            print(f"  Warning: R2 list scan for pruning failed: {e}")

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
