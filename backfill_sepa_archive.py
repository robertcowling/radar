#!/usr/bin/env python3
"""
backfill_sepa_archive.py — Historical backfill of SEPA daily rainfall totals
into the permanent rain/archive/daily_{YYYY}.json files.

backfill_rain_archive.py's docstring claims "NRW and SEPA have no equivalent
historical bulk-readings endpoint" and leaves both networks to build up their
archive day-by-day from make_rain_summary.py's own finalisation, which only
started running 2026-07-30. That claim is wrong for SEPA specifically:
build_sepa_climatology.py already proves SEPA's KiWIS service returns a full
historical Day.Total series per station on request (the same API this script
uses), exactly like EA's Hydrology API — it's NRW that genuinely has no bulk
source. Confirmed live: SEPA's "year so far" figures were built from ~3 days
of real archive data plus a rolling-window floor, understating what's likely
several hundred mm of real 2026 rainfall in North/Central Scotland.

Unlike EA's per-day-across-all-stations fetch (backfill_rain_archive.py),
KiWIS serves one station's full range in a single call — much cheaper here:
one request per station rather than one per day.

Run once via workflow_dispatch, or locally with R2 env vars set. Safe to
re-run: skips any station whose archive already covers the target range.

Usage:
  py -3 backfill_sepa_archive.py
  BACKFILL_YEARS_BACK=2 py -3 backfill_sepa_archive.py
  LIMIT_STATIONS=5 py -3 backfill_sepa_archive.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import requests

from fetch_rain import get_r2, USE_R2
from make_rain_summary import ARCHIVE_PFX, r2_get_json, r2_put_json
from build_sepa_climatology import list_daily_series, fetch_daily_values

MAX_WORKERS = 3
DEFAULT_YEARS_BACK = 2  # matches make_rain_summary.py's ARCHIVE_YEARS_LOOKBACK


def already_covered(archive, sid, date_from, date_to, min_fraction=0.5):
    """A station counts as already backfilled for this range if at least
    half its calendar days already have an entry — cheap enough to just
    re-fetch the rest via KiWIS rather than doing an exact per-day diff,
    since the fetch itself is one request per station either way."""
    station_days = archive.get(sid, {})
    if not station_days:
        return False
    total_days = (date_to - date_from).days + 1
    hits = sum(1 for d in station_days if date_from.isoformat() <= d <= date_to.isoformat())
    return hits / total_days >= min_fraction


def main():
    if not USE_R2:
        print("Error: R2 env vars not set — aborting")
        sys.exit(1)
    r2 = get_r2()

    stations_data = r2_get_json(r2, "rain/stations.json", None)
    if not stations_data or not stations_data.get("stations"):
        print("Error: rain/stations.json missing on R2 — aborting")
        sys.exit(1)
    stations = stations_data["stations"]
    sepa_refs = {ref for ref, s in stations.items() if s.get("source") == "sepa"}
    print(f"{len(sepa_refs)} SEPA stations in the live roster")

    years_back = int(os.environ.get("BACKFILL_YEARS_BACK", DEFAULT_YEARS_BACK))
    limit = int(os.environ.get("LIMIT_STATIONS", "0") or 0)
    today = datetime.now(timezone.utc).date()
    date_from = date(today.year - years_back, 1, 1)
    date_from_str, date_to_str = date_from.isoformat(), today.isoformat()

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

    series = list_daily_series(session)
    print(f"{len(series)} SEPA daily precip series on KiWIS")
    series_by_station = {s["station_no"]: s for s in series if s.get("station_no")}

    years = sorted({date_from.year + i for i in range(years_back + 1)}, reverse=True)
    archives = {y: (r2_get_json(r2, f"{ARCHIVE_PFX}/daily_{y}.json", {}) or {}) for y in years}

    todo = []
    for ref in sepa_refs:
        station_no = ref[len("sepa_"):]
        s = series_by_station.get(station_no)
        if not s:
            continue
        # Check against whichever year's archive holds most of this range —
        # current year is the common case.
        covered = already_covered(archives[today.year], ref, date_from, today)
        if not covered:
            todo.append((ref, s["ts_id"]))
    print(f"{len(todo)} stations not yet substantially backfilled for {date_from_str}..{date_to_str}")
    if limit:
        todo = todo[:limit]
        print(f"LIMIT_STATIONS={limit} — processing {len(todo)}")

    def checkpoint():
        for y, archive in archives.items():
            if not archive:
                continue
            changed = r2_put_json(r2, archive, f"{ARCHIVE_PFX}/daily_{y}.json")
            print(f"  checkpoint: daily_{y}.json {'uploaded' if changed else 'unchanged'} ({len(archive)} stations)")

    filled, done = 0, 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_daily_values, session, ts_id, date_from_str, date_to_str): ref
                   for ref, ts_id in todo}
        for fut in as_completed(futures):
            ref = futures[fut]
            try:
                days = fut.result()
            except Exception as e:
                print(f"  {ref}: fetch failed — {e}")
                days = None
            done += 1
            if days:
                for d, mm in days.items():
                    y = int(d[:4])
                    if y not in archives:
                        continue
                    archives[y].setdefault(ref, {})[d] = mm
                filled += 1
            if done % 10 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} stations fetched ({filled} with data)")
            if done % 50 == 0:
                checkpoint()

    checkpoint()
    print(f"\nBackfill complete: {filled}/{len(todo)} stations had data "
          f"({len(sepa_refs) - len(todo)} were already substantially archived).")


if __name__ == "__main__":
    main()
