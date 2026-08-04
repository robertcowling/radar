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
     directly (by-measure, date-scoped, same pattern as fetch_rain.py's
     bulk query) to see whether they have a 900s measure with recent data
     that our bulk query is somehow missing.

Read-only: does not write to R2. Safe to run any time.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

from fetch_rain import HYDRO_BASE, get_r2, r2_get_json, USE_R2


def probe_measure(guid, suffix, date_str):
    """Query a single measure's readings for one date. Mirrors the date-scoped
    pattern fetch_rain.py's bulk query uses, just narrowed to one measure."""
    url = f"{HYDRO_BASE}/id/measures/{guid}-{suffix}/readings.json"
    r = requests.get(url, params={"date": date_str, "_limit": 200}, timeout=30)
    return r


def main():
    if not USE_R2:
        print("No R2 creds — aborting")
        sys.exit(1)
    r2 = get_r2()
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    yest_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

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
        try:
            r = requests.get(f"{HYDRO_BASE}/id/stations/{guid}.json", timeout=30)
            print(f"Station detail (by guid) HTTP {r.status_code}")
            r.raise_for_status()
            items = r.json().get("items", [])
            item = items[0] if isinstance(items, list) else items
            print("EA station record label:", item.get("label"))
            measures = item.get("measures", [])
            if isinstance(measures, dict):
                measures = [measures]
            print(f"EA lists {len(measures)} measure(s):")
            for m in measures:
                lr = m.get("latestReading")
                lr_time = lr.get("dateTime") if isinstance(lr, dict) else lr
                print("  -", m.get("@id"))
                print("      parameter:", m.get("parameter"), "| period(s):", m.get("period"),
                      "| valueType:", m.get("valueType"), "| latestReading:", lr_time)
        except Exception as e:
            print("Station detail fetch failed:", e)

        for suffix in ["rainfall-t-900-mm-qualified", "rainfall-t-900-mm-unqualified"]:
            for date_str in (today_str, yest_str):
                try:
                    r = probe_measure(guid, suffix, date_str)
                    n = 0
                    if r.ok:
                        n = len(r.json().get("items", []))
                    print(f"{suffix} date={date_str}: HTTP {r.status_code}, {n} items")
                except Exception as e:
                    print(f"{suffix} date={date_str}: fetch failed: {e}")
    else:
        print("No guid on file for Wet Sleddale — can't probe measures")

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

    sample = missing[:25]
    print(f"\n  Probing EA directly for {len(sample)} of them (today's date={today_str})...")
    bug_candidates = 0
    for ref in sample:
        s = stations[ref]
        guid = s.get("guid")
        name = s.get("name")
        if not guid:
            print(f"  {ref:10s} '{name}': no guid on file")
            continue
        try:
            r = probe_measure(guid, "rainfall-t-900-mm-qualified", today_str)
            if r.status_code == 404:
                print(f"  {ref:10s} '{name}': no 900s-qualified measure (404) -- genuinely not on this API")
                continue
            r.raise_for_status()
            items = r.json().get("items", [])
            if items:
                bug_candidates += 1
                print(f"  {ref:10s} '{name}': EA HAS {len(items)} readings today! latest={items[-1].get('dateTime')} val={items[-1].get('value')}  <-- BUG CANDIDATE")
            else:
                print(f"  {ref:10s} '{name}': measure exists, 0 readings today (consistent with offline gauge)")
        except Exception as e:
            print(f"  {ref:10s} '{name}': probe failed: {e}")

    print(f"\nBug candidates (EA has data we're missing): {bug_candidates} / {len(sample)} sampled")
    print("Done.")


if __name__ == "__main__":
    main()
