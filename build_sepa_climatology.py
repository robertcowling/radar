#!/usr/bin/env python3
"""
build_sepa_climatology.py — Multi-decade rainfall climatology for SEPA gauges.

The rolling ~3-4 day window in fetch_rain.py comes from the www2.sepa.org.uk
rainfall API, which has no date-range parameter. SEPA separately publishes a
KISTERS/WISKI **KiWIS** time-series service with the full archive, keyless and
under the Open Government Licence, linked from
https://www.sepa.org.uk/environment/environmental-data/ and documented at
https://timeseriesdoc.sepa.org.uk/ ("API keys are only required accessing
large amounts of data over short periods").

Verified against the live service: 382 `Day.Total` / `Precip` series exist,
earliest starting 1961, median 2000, and `station_no` is the *same key* as the
`station_no` we already store for `sepa_*` stations — so no coordinate or
name matching is needed.

  getTimeseriesList  -> ts_id + coverage per station
  getTimeseriesValues -> {"data": [[timestamp, value], ...]}

Two documented traps this handles:
  - Missing days are silently omitted rather than returned as null (a probe
    for 2025-03-01..06 returned a single row), so valid days are always
    counted against calendar days rather than assuming a dense series.
    compute_climatology() already works this way.
  - `LongTermValue.Month.Mean` series exist but their baseline period is
    undocumented and doesn't reconcile with the exposed record, so they are
    deliberately ignored — normals are computed from the daily series here.

Day-labelling caveat: SEPA's 09:00 timestamp labels the *start* of the 24h
accumulation window (verified upstream against 15-min data), the opposite of
the Met Office convention. Totals are attributed to the label date as given,
which is internally consistent for monthly normals but can shift a single
"wettest day" attribution by one day versus Met Office sources.

Outputs the same shape as build_rain_climatology.py, so the frontend reads
both identically:
  rain/climatology/sepa_{station_no}.json
  rain/climatology_index.json   (shared index, merged not overwritten)

Usage:
  py -3 build_sepa_climatology.py
  LIMIT_STATIONS=5 py -3 build_sepa_climatology.py
  FORCE=1 py -3 build_sepa_climatology.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

from fetch_rain import get_r2, USE_R2
from make_rain_summary import r2_get_json, r2_put_json
from build_rain_climatology import CLIMATOLOGY_PFX, compute_climatology

# Deliberately not the EA builder's index: the two jobs run concurrently and
# a shared index raced, last writer silently dropping the other's entries.
INDEX_KEY = "rain/climatology_index_sepa.json"

KIWIS = "https://timeseries.sepa.org.uk/KiWIS/KiWIS"
BASE_PARAMS = {
    "service": "kisters",
    "type": "queryServices",
    "datasource": "0",
    "format": "json",
}
# No published rate limits or SLA on this host, so stay deliberately modest.
MAX_WORKERS = 3
RETRIES = 3
BACKOFF_S = 15


def list_daily_series(session):
    """-> [{"station_no":, "station_name":, "ts_id":, "from":, "to":}]"""
    params = dict(BASE_PARAMS)
    params.update({
        "request": "getTimeseriesList",
        "ts_name": "Day.Total",
        "parametertype_name": "Precip",
        "returnfields": "station_no,station_name,ts_id,coverage",
    })
    r = session.get(KIWIS, params=params, timeout=(10, 120))
    r.raise_for_status()
    rows = r.json()
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def fetch_daily_values(session, ts_id, date_from, date_to):
    """-> {YYYY-MM-DD: mm}, skipping nulls/negatives. None on failure."""
    params = dict(BASE_PARAMS)
    params.update({
        "request": "getTimeseriesValues",
        "ts_id": ts_id,
        "from": date_from,
        "to": date_to,
    })
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(KIWIS, params=params, timeout=(10, 180))
            if r.status_code == 429:
                if attempt == RETRIES:
                    return None
                time.sleep(BACKOFF_S * (attempt + 1))
                continue
            r.raise_for_status()
            payload = r.json()
            if not payload:
                return {}
            days = {}
            for ts, value in payload[0].get("data", []) or []:
                if value is None:
                    continue
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    continue
                if v < 0:
                    continue
                days[ts[:10]] = v
            return days
        except Exception as e:
            if attempt == RETRIES:
                print(f"    fetch failed for ts {ts_id}: {e}")
                return None
            time.sleep(BACKOFF_S * (attempt + 1))
    return None


def main():
    if not USE_R2:
        print("Error: R2 env vars not set — aborting")
        sys.exit(1)
    r2 = get_r2()
    force = os.environ.get("FORCE") == "1"
    limit = int(os.environ.get("LIMIT_STATIONS", "0") or 0)

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

    series = list_daily_series(session)
    print(f"{len(series)} SEPA daily precip series")
    if not series:
        print("Error: no series returned — aborting rather than writing an empty index")
        sys.exit(1)

    index = r2_get_json(r2, INDEX_KEY, {}) or {}
    todo = series
    if not force:
        todo = [s for s in series if f"sepa_{s['station_no']}" not in index]
        print(f"{len(todo)} still to process ({len(series) - len(todo)} already in index)")
    if limit:
        todo = todo[:limit]
        print(f"LIMIT_STATIONS={limit} — processing {len(todo)}")

    today_str = date.today().strftime("%Y-%m-%d")

    def process(s):
        sid = f"sepa_{s['station_no']}"
        start = (s.get("from") or "1961-01-01")[:10]
        days = fetch_daily_values(session, s["ts_id"], start, today_str)
        if not days:
            return sid, None
        stats = compute_climatology(days)
        del days  # raw series discarded before moving on
        if not stats:
            return sid, None
        stats["stationRef"] = sid
        stats["name"] = s.get("station_name") or sid
        stats["source"] = "sepa"
        stats["dayWindow"] = "09:00 start-of-window (SEPA convention)"
        # SEPA does not expose per-value quality flags on this service, so a
        # Good-share figure would be fabricated — left null rather than 100.
        stats["qualityGoodPct"] = None
        stats["validDays"] = sum(stats["monthDayCounts"].values())
        return sid, stats

    done = saved = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, s): s for s in todo}
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
                print(f"  {done}/{len(todo)} series ({saved} saved)")
            if done % 50 == 0:
                r2_put_json(r2, index, INDEX_KEY)
                print(f"    checkpoint: index now {len(index)} stations")

    r2_put_json(r2, index, INDEX_KEY)
    print(f"\nDone: {saved}/{len(todo)} SEPA series this run; index holds {len(index)}.")


if __name__ == "__main__":
    main()
