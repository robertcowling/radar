#!/usr/bin/env python3
"""
Diagnostic (temporary, read-only): why do some Hydrology-listed rain
stations never show up in our merged readings, even though EA's own
Hydrology Data Explorer has data for them?

Checks:
  1. Wet Sleddale (600986) specifically — its Hydrology station record
     (available measures + periods) and whether its 900s measure has
     recent readings directly from EA, vs what our bulk fetch captured.
  2. A broader sweep: every EA-origin station (ref not prefixed nrw_/sepa_)
     in our stations.json that is absent from BOTH of the last two days'
     merged readings files in R2 — for a sample of those, query EA
     directly to see whether they have a 900s measure with recent data
     that our bulk query is somehow missing.

Read-only: does not write to R2. Safe to run any time.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

from fetch_rain import HYDRO_BASE, get_r2, r2_get_json, USE_R2

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

    # ── Part 1: Wet Sleddale specifically ──────────────────────────────────
    print("\n=== Wet Sleddale (600986) ===")
    ws = stations.get("600986")
    print("Our stations.json entry:", json.dumps(ws))
    guid = ws.get("guid") if ws else None

    try:
        r = requests.get(f"{HYDRO_BASE}/id/stations/600986.json", timeout=30)
        r.raise_for_status()
        items = r.json().get("items", [])
        item = items[0] if isinstance(items, list) else items
        print("EA station record label:", item.get("label"))
        print("EA station record stationGuid:", item.get("stationGuid"))
        measures = item.get("measures", [])
        if isinstance(measures, dict):
            measures = [measures]
        print(f"EA lists {len(measures)} measure(s):")
        for m in measures:
            print("  -", m.get("@id"), "| parameter:", m.get("parameter"),
                  "| period:", m.get("period"), "| valueType:", m.get("valueType"),
                  "| latestReading time:", (m.get("latestReading") or {}).get("dateTime")
                  if isinstance(m.get("latestReading"), dict) else m.get("latestReading"))
    except Exception as e:
        print("EA station record fetch failed:", e)

    if guid:
        for suffix in ["rainfall-t-900-mm-qualified", "rainfall-t-900-mm-unqualified"]:
            measure_url = f"{HYDRO_BASE}/id/measures/{guid}-{suffix}"
            try:
                r = requests.get(f"{measure_url}/readings.json",
                                  params={"_sorted": "", "_limit": 5}, timeout=30)
                print(f"\n{suffix}: HTTP {r.status_code}")
                if r.ok:
                    items = r.json().get("items", [])
                    print(f"  {len(items)} readings, latest few:")
                    for it in items[:5]:
                        print("   ", it.get("dateTime"), it.get("value"), it.get("quality"))
            except Exception as e:
                print(f"{suffix}: fetch failed:", e)

    # ── Part 2: broader sweep ──────────────────────────────────────────────
    print("\n=== Broader sweep: EA stations missing from last 2 days of our readings ===")
    now = datetime.now(timezone.utc)
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

    sample = missing[:25]
    print(f"\n  Probing EA directly for {len(sample)} of them...")
    for ref in sample:
        s = stations[ref]
        guid = s.get("guid")
        name = s.get("name")
        if not guid:
            print(f"  {ref:10s} '{name}': no guid on file")
            continue
        try:
            r = requests.get(
                f"{HYDRO_BASE}/id/measures/{guid}-rainfall-t-900-mm-qualified/readings.json",
                params={"_sorted": "", "_limit": 3}, timeout=20)
            if r.status_code == 404:
                print(f"  {ref:10s} '{name}': no 900s-qualified measure (404)")
                continue
            r.raise_for_status()
            items = r.json().get("items", [])
            if items:
                print(f"  {ref:10s} '{name}': EA HAS recent 900s data! latest={items[0].get('dateTime')} val={items[0].get('value')}  <-- BUG CANDIDATE")
            else:
                print(f"  {ref:10s} '{name}': 900s measure exists but no recent items")
        except Exception as e:
            print(f"  {ref:10s} '{name}': probe failed: {e}")

    print("\nDone.")

if __name__ == "__main__":
    main()
