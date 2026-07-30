#!/usr/bin/env python3
"""
build_nrw_climatology.py — Multi-decade rainfall climatology for NRW gauges.

NRW's public telemetry API (www2.sepa.org.uk/rainfall) has no date-range
parameter and exposes only a rolling ~3-4 day window. NRW gauges now appear in
MIDAS Open (https://catalogue.ceda.ac.uk/uuid/dbd451271eb04662beade68da43546e1/)
as of dataset-version-202607, under Welsh historic county directories
(gwynedd, dyfed, clwyd, powys, etc.).

MIDAS stations are joined to NRW telemetry by lat/lon nearest-neighbour match:
fetch the MIDAS station metadata CSV (coordinates included), find each NRW
station's nearest MIDAS src_id within ~5 km, then fetch the station-year
BADC-CSV files.

Confirmed coverage: ~700+ Welsh daily rainfall stations, earliest 1961.

Day convention: MIDAS labels like EA (09:00 start-of-window).

Outputs the same shape as build_rain_climatology.py:
  rain/climatology/nrw_{station_no}.json
  rain/climatology_index_nrw.json

Usage:
  CEDA_TOKEN=<jwt> py -3 build_nrw_climatology.py
  CEDA_TOKEN=<jwt> LIMIT_STATIONS=5 py -3 build_nrw_climatology.py
  CEDA_TOKEN=<jwt> FORCE=1 py -3 build_nrw_climatology.py
Requires R2 env vars (same set as fetch_rain.py).
"""

import csv
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

from fetch_rain import get_r2, USE_R2
from make_rain_summary import r2_get_json, r2_put_json
from build_rain_climatology import CLIMATOLOGY_PFX, compute_climatology

INDEX_KEY = "rain/climatology_index_nrw.json"

CEDA_BASE = "https://dap.ceda.ac.uk/badc/ukmo-midas-open/data/uk-daily-rain-obs"
DATASET_VERSION = "dataset-version-202607"
METADATA_URL = f"{CEDA_BASE}/{DATASET_VERSION}/midas-open_uk-daily-rain-obs_dv-202607_station-metadata.csv"

MAX_WORKERS = 2
CEDA_TOKEN = os.environ.get("CEDA_TOKEN")
MATCH_RADIUS_KM = 5.0


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance in km between two points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def fetch_midas_metadata():
    """Fetch and parse MIDAS station metadata CSV. -> {src_id: {lat, lon, name, first_year, last_year}}"""
    headers = {}
    if CEDA_TOKEN:
        headers["Authorization"] = f"Bearer {CEDA_TOKEN}"
    try:
        r = requests.get(METADATA_URL, timeout=60, headers=headers)
        if r.status_code == 401:
            print("Error: 401 Unauthorized fetching MIDAS metadata — CEDA token invalid/expired?")
            return None
        if r.status_code == 404:
            print("Error: 404 MIDAS metadata not found at CEDA")
            return None
        r.raise_for_status()
    except Exception as e:
        print(f"Error fetching MIDAS metadata: {e}")
        return None

    # Station names can contain commas (e.g. "NORTH RONALDSAY, GERBO") and are
    # CSV-quoted, so this needs a real CSV parser rather than naive .split(",").
    metadata = {}
    data_lines = []
    in_data = False
    for line in r.text.split("\n"):
        if line.startswith("data"):
            in_data = True
            continue
        if line.startswith("end data"):
            break
        if not in_data or not line.strip():
            continue
        data_lines.append(line)

    reader = csv.reader(data_lines)
    header_row = None
    for parts in reader:
        if header_row is None:
            header_row = [p.strip() for p in parts]
            continue
        if len(parts) < len(header_row):
            continue
        try:
            row = dict(zip(header_row, [p.strip() for p in parts]))
            src_id = row.get("src_id", "").strip()
            lat = float(row.get("station_latitude", 0))
            lon = float(row.get("station_longitude", 0))
            name = row.get("station_name", "").strip()
            file_name = row.get("station_file_name", "").strip()
            county = row.get("historic_county", "").strip()
            first_year = int(row.get("first_year", 1961))
            last_year = int(row.get("last_year", 2026))
            if not src_id or not lat or not lon or not file_name or not county:
                continue
            metadata[src_id] = {
                "lat": lat,
                "lon": lon,
                "name": name,
                "file_name": file_name,
                "county": county,
                "first_year": first_year,
                "last_year": last_year,
            }
        except (ValueError, KeyError, IndexError):
            continue

    return metadata if metadata else None


def fetch_midas_csv(county, src_id, file_name, year):
    """Fetch a BADC-CSV file from MIDAS for a station-year. Returns CSV text or None.

    Path/filename convention confirmed by directory listing:
    {county}/{src_id}_{file_name}/qc-version-1/
      midas-open_uk-daily-rain-obs_dv-202607_{county}_{src_id}_{file_name}_qcv-1_{year}.csv
    """
    fname = f"midas-open_uk-daily-rain-obs_dv-202607_{county}_{src_id}_{file_name}_qcv-1_{year}.csv"
    path = f"{CEDA_BASE}/{DATASET_VERSION}/{county}/{src_id}_{file_name}/qc-version-1/{fname}"
    headers = {}
    if CEDA_TOKEN:
        headers["Authorization"] = f"Bearer {CEDA_TOKEN}"
    try:
        r = requests.get(path, timeout=30, headers=headers)
        if r.status_code == 401:
            print(f"    401 Unauthorized fetching {county}/{src_id}/{year}")
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"    fetch failed for {county}/{src_id}/{year}: {e}")
        return None


def parse_badc_csv(text):
    """Parse BADC-CSV format (header block, then 'data'/'end data' block with a
    CSV header row naming columns — ob_date, prcp_amt, etc). -> {YYYY-MM-DD: mm}

    Missing values are flagged 'NA' in prcp_amt per the file's own G-block
    (missing_value,G,NA) and are excluded rather than treated as zero rainfall.

    ob_day_cnt is the number of days the reading accumulates (usually 1, i.e.
    a true daily total). When a gauge isn't visited for a while MIDAS records
    the backlog as one multi-day "catch-up" total with ob_day_cnt > 1 on the
    day it was finally read (confirmed against a real row: Bala 2025-01-01,
    ob_day_cnt=31, prcp_amt=243.6mm). Attributing that whole total to a single
    date would fabricate an extreme "wettest day" record and corrupt dry-spell
    detection, so only ob_day_cnt == 1 rows are treated as daily totals.
    """
    days = {}
    in_data = False
    header = None
    for line in text.split("\n"):
        if line.startswith("data"):
            in_data = True
            continue
        if line.startswith("end data"):
            break
        if not in_data or not line.strip():
            continue
        if header is None:
            header = line.split(",")
            continue
        parts = line.split(",")
        if len(parts) < len(header):
            continue
        row = dict(zip(header, parts))
        raw_date = row.get("ob_date", "")
        raw_val = row.get("prcp_amt", "NA")
        if not raw_date or raw_val == "NA":
            continue
        if row.get("ob_day_cnt", "1") != "1":
            continue
        try:
            v = float(raw_val)
            if v < 0:
                continue
            days[raw_date[:10]] = v
        except (ValueError, TypeError):
            continue
    return days


def main():
    if not USE_R2:
        print("Error: R2 env vars not set — aborting")
        sys.exit(1)
    if not CEDA_TOKEN:
        print("Error: CEDA_TOKEN env var not set — aborting")
        print("  Usage: CEDA_TOKEN=<jwt> py -3 build_nrw_climatology.py")
        sys.exit(1)

    r2 = get_r2()

    # Load NRW stations from our existing station list.
    stations_data = r2_get_json(r2, "rain/stations.json", None)
    if not stations_data:
        print("Error: rain/stations.json missing on R2 — aborting")
        sys.exit(1)
    stations = stations_data["stations"]
    nrw_stations = {ref: s for ref, s in stations.items() if s.get("source") == "nrw"}
    print(f"{len(nrw_stations)} NRW stations to match")

    if not nrw_stations:
        print("Error: no NRW stations found in rain/stations.json — aborting")
        sys.exit(1)

    # Fetch MIDAS station metadata with coordinates.
    print("Fetching MIDAS station metadata from CEDA…")
    midas_meta = fetch_midas_metadata()
    if not midas_meta:
        print("Error: failed to fetch or parse MIDAS metadata — aborting")
        sys.exit(1)
    print(f"  {len(midas_meta)} MIDAS stations with coordinates")

    # Lat/lon nearest-neighbour matching: NRW → MIDAS.
    print("Matching NRW stations to MIDAS by lat/lon proximity…")
    matches = {}
    unmatched = []
    for ref, nrw in nrw_stations.items():
        nrw_lat = nrw.get("lat")
        nrw_lon = nrw.get("lon")
        if not nrw_lat or not nrw_lon:
            unmatched.append(ref)
            continue

        best_src_id = None
        best_dist = float("inf")
        for src_id, midas in midas_meta.items():
            dist = haversine_km(nrw_lat, nrw_lon, midas["lat"], midas["lon"])
            if dist < best_dist:
                best_dist = dist
                best_src_id = src_id

        if best_src_id and best_dist < MATCH_RADIUS_KM:
            m = midas_meta[best_src_id]
            matches[ref] = {
                "src_id": best_src_id,
                "dist_km": round(best_dist, 2),
                "midas_name": m["name"],
                "file_name": m["file_name"],
                "county": m["county"],
                "first_year": m["first_year"],
                "last_year": m["last_year"],
            }
        else:
            unmatched.append(ref)

    print(f"  {len(matches)} matches within {MATCH_RADIUS_KM} km")
    print(f"  {len(unmatched)} stations unmatched (too far or no coords)")

    if not matches:
        print("Error: no NRW-to-MIDAS matches found — aborting")
        sys.exit(1)

    force = os.environ.get("FORCE") == "1"
    limit = int(os.environ.get("LIMIT_STATIONS", "0") or 0)

    index = r2_get_json(r2, INDEX_KEY, {}) or {}
    todo = list(matches.items())
    if not force:
        todo = [(ref, m) for ref, m in todo if ref not in index]
        print(f"{len(todo)} still to process ({len(matches) - len(todo)} already in index)")
    if limit:
        todo = todo[:limit]
        print(f"LIMIT_STATIONS={limit} — processing {len(todo)}")

    def process(ref_match):
        ref, match_info = ref_match
        src_id = match_info["src_id"]
        county = match_info["county"]
        file_name = match_info["file_name"]
        nrw_sid = ref
        nrw_name = nrw_stations[ref].get("name", nrw_sid)

        first_year = match_info["first_year"]
        last_year = min(match_info["last_year"], date.today().year)

        # Fetch all years for this station from MIDAS.
        all_days = {}
        for year in range(first_year, last_year + 1):
            csv_text = fetch_midas_csv(county, src_id, file_name, year)
            if csv_text:
                days = parse_badc_csv(csv_text)
                all_days.update(days)

        if not all_days:
            return nrw_sid, None

        stats = compute_climatology(all_days)
        del all_days
        if not stats:
            return nrw_sid, None

        stats["stationRef"] = nrw_sid
        stats["name"] = nrw_name
        stats["source"] = "nrw"
        stats["dayWindow"] = "09:00 start-of-window (MIDAS convention)"
        stats["qualityGoodPct"] = None
        stats["validDays"] = sum(stats["monthDayCounts"].values())
        stats["midasSrcId"] = src_id
        stats["midasName"] = match_info.get("midas_name", "")
        stats["matchDistKm"] = match_info["dist_km"]

        return nrw_sid, stats

    done = saved = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, item): item for item in todo}
        for fut in as_completed(futures):
            try:
                sid, stats = fut.result()
            except Exception as e:
                print(f"  failed: {e}")
                stats = None
            done += 1
            if stats:
                r2_put_json(r2, stats, f"{CLIMATOLOGY_PFX}/{sid}.json")
                index[sid] = {
                    "recordStart": stats["recordStart"],
                    "recordEnd": stats["recordEnd"],
                    "recordYears": stats["recordYears"],
                    "qualityGoodPct": None,
                    "annualNormal": round(sum(
                        c["mean"] for c in stats["climatology"].values()), 1)
                        if len(stats["climatology"]) == 12 else None,
                }
                saved += 1
            if done % 10 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} stations ({saved} saved)")
            if done % 50 == 0:
                r2_put_json(r2, index, INDEX_KEY)
                print(f"    checkpoint: index now {len(index)} stations")

    r2_put_json(r2, index, INDEX_KEY)
    print(f"\nDone: {saved}/{len(todo)} NRW stations this run; index holds {len(index)}.")


if __name__ == "__main__":
    main()
