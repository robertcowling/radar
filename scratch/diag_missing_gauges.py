#!/usr/bin/env python3
"""
Diagnostic (temporary, read-only): why do some Hydrology-listed rain
stations never show up in our merged readings, even though EA's own
Hydrology Data Explorer has data for them?

Checks:
  1. Wet Sleddale (600986) specifically — confirmed its stored GUID
     resolves to a reservoir LEVEL station (no rainfall measure at all).
  2. Whether a separate, correctly-tagged rainfall "Wet Sleddale" station
     exists in EA's Hydrology API under a different GUID/stationReference.
  3. A broader sweep: every EA-origin station in our stations.json that is
     absent from BOTH of the last two days' merged readings files in R2.
     For a sample, fetch the station's detail page BY GUID (not a guessed
     measure URL — readings.json for a nonexistent measure ID returns
     HTTP 200 with an empty list, not 404, so probing it directly is
     misleading) and check whether it actually has a rainfall measure at
     all. This tells us how many "missing" gauges share Wet Sleddale's
     bug (guid points to a non-rainfall station) vs are genuinely
     offline rainfall gauges.

Read-only: does not write to R2. Safe to run any time.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

from fetch_rain import HYDRO_BASE, get_r2, r2_get_json, USE_R2


def fetch_station_detail(guid):
    """GET a station's detail page by GUID. Returns (item_dict, http_status)."""
    r = requests.get(f"{HYDRO_BASE}/id/stations/{guid}.json", timeout=30)
    if not r.ok:
        return None, r.status_code
    items = r.json().get("items", [])
    item = items[0] if isinstance(items, list) else items
    return item, r.status_code


def measures_of(item):
    measures = (item or {}).get("measures", [])
    if isinstance(measures, dict):
        measures = [measures]
    return measures


def rainfall_measure(item):
    for m in measures_of(item):
        if (m.get("parameter") or "").lower() == "rainfall":
            return m
    return None


def main():
    if not USE_R2:
        print("No R2 creds — aborting")
        sys.exit(1)
    r2 = get_r2()
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    stations_data, ok = r2_get_json(r2, "rain/stations.json", None)
    if not ok or not stations_data:
        print("Could not load stations.json")
        sys.exit(1)
    stations = stations_data.get("stations", {})
    print(f"Loaded {len(stations)} stations")

    # ── Part 1: Wet Sleddale specifically ──────────────────────────────────
    print("\n=== Wet Sleddale (600986) ===")
    ws = stations.get("600986")
    print("Our stations.json entry:", json.dumps(ws))
    guid = ws.get("guid") if ws else None

    if guid:
        item, status = fetch_station_detail(guid)
        print(f"Station detail (by our stored guid) HTTP {status}")
        if item:
            print("EA label:", item.get("label"))
            for m in measures_of(item):
                print("  -", m.get("@id"), "| parameter:", m.get("parameter"), "| period:", m.get("period"))
            rf = rainfall_measure(item)
            print("Has a rainfall measure?", "YES" if rf else "NO -- this guid is not a rainfall station")

    print("\n--- Searching EA Hydrology API for ALL stations labelled 'Wet Sleddale' ---")
    try:
        r = requests.get(f"{HYDRO_BASE}/id/stations.json",
                          params={"label": "Wet Sleddale"}, timeout=30)
        print(f"label search HTTP {r.status_code}")
        if r.ok:
            items = r.json().get("items", [])
            print(f"  {len(items)} station(s) with label 'Wet Sleddale':")
            for it in items:
                print(f"    ref={it.get('stationReference')} guid={it.get('stationGuid')} "
                      f"label={it.get('label')} lat={it.get('lat')} lon={it.get('long')}")
                for m in measures_of(it):
                    print(f"      measure: {m.get('@id')} | parameter={m.get('parameter')} period={m.get('period')}")
    except Exception as e:
        print("label search failed:", e)

    # ── Part 2: broader sweep ──────────────────────────────────────────────
    print("\n=== Broader sweep: EA stations missing from last 2 days of our readings ===")
    day_keys = [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(2)]
    day_data = {}
    for dk in day_keys:
        d, ok = r2_get_json(r2, f"rain/readings/{dk}.json", None)
        day_data[dk] = (d or {}).get("readings", {})
        print(f"  {dk}: {len(day_data[dk])} stations present in our merged file")

    ea_refs = [ref for ref in stations if not ref.startswith("nrw_") and not ref.startswith("sepa_")]
    print(f"  {len(ea_refs)} EA-origin stations in stations.json")

    missing = [ref for ref in ea_refs
               if ref not in day_data[day_keys[0]] and ref not in day_data[day_keys[1]]]
    print(f"  {len(missing)} EA stations missing from BOTH days")

    sample = missing[:60]
    print(f"\n  Checking station DETAIL (by guid) for {len(sample)} of them...")
    no_rainfall_measure = []
    has_rainfall_but_no_data_today = []
    errors = []
    for ref in sample:
        s = stations[ref]
        guid = s.get("guid")
        name = s.get("name")
        if not guid:
            print(f"  {ref:10s} '{name}': no guid on file")
            continue
        try:
            item, status = fetch_station_detail(guid)
            if status != 200 or not item:
                errors.append((ref, name, status))
                print(f"  {ref:10s} '{name}': station detail HTTP {status}")
                continue
            rf = rainfall_measure(item)
            if not rf:
                no_rainfall_measure.append((ref, name))
                params = sorted({m.get("parameter") for m in measures_of(item)})
                print(f"  {ref:10s} '{name}': NO rainfall measure on this guid (has: {params})  <-- SAME BUG AS WET SLEDDALE")
                continue
            # Has a rainfall measure — check today's readings via its real @id
            measure_id_url = rf.get("@id", "")
            rr = requests.get(f"{measure_id_url}/readings.json",
                               params={"date": today_str, "_limit": 200}, timeout=30)
            items = rr.json().get("items", []) if rr.ok else []
            if items:
                has_rainfall_but_no_data_today.append((ref, name))
                print(f"  {ref:10s} '{name}': HAS rainfall measure AND {len(items)} readings today! latest={items[-1].get('dateTime')} val={items[-1].get('value')}  <-- REAL BUG CANDIDATE")
            else:
                print(f"  {ref:10s} '{name}': has rainfall measure, 0 readings today (period={rf.get('period')}) — consistent with offline gauge")
        except Exception as e:
            errors.append((ref, name, str(e)))
            print(f"  {ref:10s} '{name}': probe failed: {e}")

    print(f"\n--- Summary of {len(sample)} sampled ---")
    print(f"  No rainfall measure at all (Wet-Sleddale-style guid bug): {len(no_rainfall_measure)}")
    print(f"  Has rainfall measure + EA shows data today we're missing: {len(has_rainfall_but_no_data_today)}")
    print(f"  Errors: {len(errors)}")
    if no_rainfall_measure:
        print("\n  Non-rainfall-guid stations:")
        for ref, name in no_rainfall_measure:
            print(f"    {ref:10s} {name}")

    print("\nDone.")


if __name__ == "__main__":
    main()
