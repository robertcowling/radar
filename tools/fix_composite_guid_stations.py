#!/usr/bin/env python3
"""
One-time repair: fix rain/stations.json entries whose stored "guid" points to
a co-located level/flow instrument instead of the rainfall instrument's real
measure-id prefix.

Some EA Hydrology sites host a rain gauge co-located with a level/flow
recorder under the same base stationGuid (e.g. Wet Sleddale, ref 600986).
EA disambiguates the two by appending the stationReference to the GUID in
the rainfall measure's own @id ("{guid}_{ref}-rainfall-...") while the
level/flow measures keep the plain "{guid}-level-..." form. Our stored
"guid" field used to be the bare stationGuid, which resolves to the
level/flow instrument for these sites — so every rainfall reading for them
was silently dropped in parse_readings() (raw_id from the measure URI never
matched suid_to_ref). fetch_rain.py's fetch_stations() is fixed to derive
the correct raw_id going forward; this script repairs the stations already
cached in R2's rain/stations.json (only re-fetched when count < 100, so it
doesn't self-heal).

For each EA-origin station (not nrw_/sepa_ prefixed):
  1. Fetch /id/stations/{guid}.json for the stored guid.
  2. If that record already has a rainfall measure, it's fine — skip.
  3. Otherwise, search /id/stations.json?label={name} for a sibling record
     with a rainfall measure, and correct the stored guid to its real
     raw-id prefix.
  4. If nothing found, leave it alone and report it (genuinely no rainfall
     measure available, or an EA metadata quirk we can't resolve here).

Writes the corrected stations.json back to R2 (and locally) only if at
least one station was fixed. Safe to re-run; idempotent.
"""
import sys

import requests

from fetch_rain import (HYDRO_BASE, STATIONS_PATH, get_r2, r2_get_json,
                         r2_put_json, USE_R2, write_local_json)


def measures_of(item):
    measures = (item or {}).get("measures", [])
    if isinstance(measures, dict):
        measures = [measures]
    return measures


def rainfall_raw_id(item):
    for m in measures_of(item):
        if (m.get("parameter") or "").lower() == "rainfall":
            mid = (m.get("@id") or "").rsplit("/", 1)[-1]
            if "-rainfall" in mid:
                return mid.split("-rainfall")[0]
    return None


def fetch_station_detail(guid):
    r = requests.get(f"{HYDRO_BASE}/id/stations/{guid}.json", timeout=30)
    if not r.ok:
        return None
    items = r.json().get("items", [])
    if isinstance(items, list):
        return items[0] if items else None
    return items if isinstance(items, dict) else None


def main():
    if not USE_R2:
        print("No R2 creds — aborting")
        sys.exit(1)
    r2 = get_r2()

    stations_data, ok = r2_get_json(r2, "rain/stations.json", None)
    if not ok or not stations_data:
        print("Could not load stations.json")
        sys.exit(1)
    stations = stations_data.get("stations", {})
    print(f"Loaded {len(stations)} stations")

    ea_refs = [ref for ref in stations if not ref.startswith("nrw_") and not ref.startswith("sepa_")]
    print(f"Checking {len(ea_refs)} EA-origin stations...")

    fixed, already_ok, unresolved, errors = [], 0, [], []
    for ref in ea_refs:
        s = stations[ref]
        guid = s.get("guid")
        name = s.get("name")
        if not guid:
            continue
        try:
            item = fetch_station_detail(guid)
            if item and rainfall_raw_id(item):
                already_ok += 1
                continue

            r = requests.get(f"{HYDRO_BASE}/id/stations.json", params={"label": name}, timeout=30)
            r.raise_for_status()
            candidates = r.json().get("items", [])
            new_raw_id = None
            for cand in candidates:
                rid = rainfall_raw_id(cand)
                if rid:
                    new_raw_id = rid
                    break

            if new_raw_id and new_raw_id != guid:
                print(f"  FIX {ref:10s} '{name}': {guid} -> {new_raw_id}")
                stations[ref]["guid"] = new_raw_id
                fixed.append(ref)
            else:
                unresolved.append((ref, name))
        except Exception as e:
            errors.append((ref, name, str(e)))

    print(f"\nAlready OK: {already_ok}")
    print(f"Fixed: {len(fixed)} -> {fixed}")
    print(f"Unresolved (left as-is): {len(unresolved)}")
    for ref, name in unresolved:
        print(f"  {ref:10s} {name}")
    print(f"Errors: {len(errors)}")
    for ref, name, e in errors:
        print(f"  {ref:10s} {name}: {e}")

    if fixed:
        stations_data["stations"] = stations
        write_local_json(STATIONS_PATH, stations_data)
        r2_put_json(r2, "rain/stations.json", stations_data)
        print(f"\nUploaded corrected stations.json ({len(fixed)} guids repaired)")
    else:
        print("\nNo changes made — not uploading.")


if __name__ == "__main__":
    main()
