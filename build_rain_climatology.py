#!/usr/bin/env python3
"""
build_rain_climatology.py — Multi-decade rainfall climatology per EA gauge.

The EA Hydrology API exposes a *daily* rainfall measure per station
(`-rainfall-t-86400-mm-qualified`) whose full record can be pulled in one
request. Confirmed by probing: all 995 EA rainfall stations have this measure,
with dateOpened spanning 1957..2024 (median 1998) — i.e. genuine climatology,
not the ~15 months the bulk archive CSVs hold.

Designed to run LOCALLY, station by station: fetch one station's full history
(~2.5 MB CSV), reduce it to compact derived stats, upload those, discard the
raw data, move on. Peak disk/memory stays at one station's worth rather than
the ~2.5 GB the whole network would need.

Outputs (per station, tiny — monthly series + climatology, not raw days):
  rain/climatology/{ref}.json   full monthly series, per-calendar-month
                                climatology (mean/p10/p90), annual totals,
                                all-time records, period of record
  rain/climatology_index.json   compact per-station record-length index

Data quality rules (EA flags every daily value):
  - `quality == "Missing"` rows carry an empty value and are excluded entirely.
  - Good/Estimated/Unchecked/Suspect values are all summed, but the share of
    non-Good data is recorded per station (`qualityGoodPct`) so the UI can
    caveat rather than silently presenting ~20%-Suspect series as gospel.
  - A month only counts toward climatological means/percentiles if at least
    MIN_MONTH_COMPLETENESS of its calendar days have a valid value — otherwise
    a month half-missing would drag averages down.
  - A calendar month needs MIN_YEARS_FOR_CLIMATOLOGY qualifying years before a
    mean/percentile is published for it.

Note on day definition: EA daily totals are 09:00–09:00 rain days (the
meteorological convention), which differs from the UTC-midnight days in
rain/archive/daily_{YYYY}.json. Climatology and all-time records come from
this EA series; live rolling totals and month/year-to-date stay on the
existing archive. They are never summed together.

Usage:
  py -3 build_rain_climatology.py              # all stations, resumable
  LIMIT_STATIONS=5 py -3 build_rain_climatology.py   # smoke test
  FORCE=1 py -3 build_rain_climatology.py      # ignore existing, recompute
Requires R2 env vars (same set as fetch_rain.py).
"""

import csv
import io
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import requests

from fetch_rain import HYDRO_BASE, get_r2, USE_R2
from make_rain_summary import DRY_DAY_MM, r2_get_json, r2_put_json

CLIMATOLOGY_PFX = "rain/climatology"
INDEX_KEY = "rain/climatology_index.json"

# EA rate-limits aggressive pulls (a 6-worker backfill tripped 429s after
# ~150-200 requests), so stay gentle: these are big responses.
MAX_WORKERS = 2
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_S = 20

MIN_MONTH_COMPLETENESS = 0.9   # fraction of a month's days needing a valid value
MIN_YEARS_FOR_CLIMATOLOGY = 5  # qualifying years needed before publishing a monthly normal
EXCLUDE_QUALITY = {"Missing"}
GOOD_QUALITY = {"Good"}


def days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def percentile(sorted_vals, pct):
    """Linear-interpolated percentile on an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def fetch_station_daily_csv(session, measure_id):
    """Full daily record for one measure as CSV text, or None."""
    url = f"{HYDRO_BASE}/id/measures/{measure_id}/readings.csv?_limit=200000"
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            r = session.get(url, timeout=(10, 120))
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                if attempt == RATE_LIMIT_RETRIES:
                    print(f"    still rate-limited after {RATE_LIMIT_RETRIES} retries — skipping")
                    return None
                wait = RATE_LIMIT_BACKOFF_S * (attempt + 1)
                print(f"    429 — backing off {wait}s ({attempt + 1}/{RATE_LIMIT_RETRIES})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"    fetch failed: {e}")
            return None
    return None


def parse_daily_csv(text):
    """-> (days {date_str: mm}, n_good, n_valid). Excludes Missing/blank values."""
    days, n_good, n_valid = {}, 0, 0
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        quality = (row.get("quality") or "").strip()
        raw = (row.get("value") or "").strip()
        d = (row.get("date") or "").strip()
        if not d or not raw or quality in EXCLUDE_QUALITY:
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if v < 0:
            continue
        days[d] = v
        n_valid += 1
        if quality in GOOD_QUALITY:
            n_good += 1
    return days, n_good, n_valid


def month_fully_elapsed(month_key, today):
    """True only once the calendar month has completely finished. An
    in-progress month must never be treated as a monthly total: with a 90%
    day threshold, day 28 of a 31-day month passes, so a dry start to the
    current month would otherwise land as the station's all-time driest."""
    y, mo = int(month_key[:4]), int(month_key[5:7])
    return date(y, mo, days_in_month(y, mo)) < today


def compute_climatology(days, today=None):
    """days: {YYYY-MM-DD: mm} -> derived stats dict (no raw days retained)."""
    if not days:
        return None
    today = today or date.today()

    monthly = defaultdict(float)
    month_day_counts = defaultdict(int)
    annual = defaultdict(float)
    for d, v in days.items():
        monthly[d[:7]] += v
        month_day_counts[d[:7]] += 1
        annual[d[:4]] += v

    monthly = {m: round(v, 2) for m, v in monthly.items()}
    annual = {y: round(v, 2) for y, v in annual.items()}

    # A month counts as a real monthly total only if it has both fully
    # elapsed and enough valid days, so in-progress and gap-ridden months
    # are never compared against finished ones.
    complete_months = {}
    for m, total in monthly.items():
        y, mo = int(m[:4]), int(m[5:7])
        if not month_fully_elapsed(m, today):
            continue
        if month_day_counts[m] / days_in_month(y, mo) >= MIN_MONTH_COMPLETENESS:
            complete_months[m] = total

    # Per-calendar-month normals from complete months only.
    by_calendar_month = defaultdict(list)
    for m, total in complete_months.items():
        by_calendar_month[int(m[5:7])].append(total)

    climatology = {}
    for mo in range(1, 13):
        vals = sorted(by_calendar_month.get(mo, []))
        if len(vals) < MIN_YEARS_FOR_CLIMATOLOGY:
            continue
        climatology[f"{mo:02d}"] = {
            "mean": round(sum(vals) / len(vals), 1),
            "p10": round(percentile(vals, 0.10), 1),
            "p90": round(percentile(vals, 0.90), 1),
            "years": len(vals),
        }

    wettest_day_date = max(days, key=days.get)
    records = {
        "wettestDay": {"date": wettest_day_date, "mm": round(days[wettest_day_date], 2)},
    }
    if complete_months:
        wm = max(complete_months, key=complete_months.get)
        dm = min(complete_months, key=complete_months.get)
        records["wettestMonth"] = {"month": wm, "mm": complete_months[wm]}
        records["driestMonth"] = {"month": dm, "mm": complete_months[dm]}

    # Longest run of consecutive *present* days below DRY_DAY_MM. A gap in the
    # record breaks the run — an absent day is unknown, not confirmed dry.
    best_len, best_start = 0, None
    run_len, run_start, prev = 0, None, None
    for d in sorted(days):
        cur = datetime.strptime(d, "%Y-%m-%d")
        contiguous = prev is not None and (cur - prev).days == 1
        if days[d] < DRY_DAY_MM:
            run_len = run_len + 1 if contiguous else 1
            if run_len == 1:
                run_start = d
            if run_len > best_len:
                best_len, best_start = run_len, run_start
        else:
            run_len = 0
        prev = cur
    if best_len:
        records["longestDrySpell"] = {"days": best_len, "from": best_start}

    all_dates = sorted(days)
    first, last = all_dates[0], all_dates[-1]
    span_years = round((datetime.strptime(last, "%Y-%m-%d") -
                        datetime.strptime(first, "%Y-%m-%d")).days / 365.25, 1)

    return {
        "monthly": dict(sorted(monthly.items())),
        "monthDayCounts": dict(sorted(month_day_counts.items())),
        "annual": dict(sorted(annual.items())),
        "climatology": climatology,
        "records": records,
        "recordStart": first,
        "recordEnd": last,
        "recordYears": span_years,
        "completeMonths": len(complete_months),
        "totalMonths": len(monthly),
    }


def fetch_ea_stations():
    """-> {ref: {"measure": daily_measure_id, "name":, "dateOpened":}}"""
    url = f"{HYDRO_BASE}/id/stations.json?observedProperty=rainfall&_limit=10000"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    out = {}
    for item in r.json().get("items", []):
        ref = item.get("stationReference")
        if not ref:
            continue
        daily = None
        for m in item.get("measures", []):
            mid = m.get("@id") if isinstance(m, dict) else m
            if mid and "-86400-" in mid:
                daily = mid.rsplit("/", 1)[-1]
                break
        if not daily:
            continue
        out[ref] = {
            "measure": daily,
            "name": item.get("label") or ref,
            "dateOpened": item.get("dateOpened"),
        }
    return out


def main():
    if not USE_R2:
        print("Error: R2 env vars not set — aborting")
        sys.exit(1)
    r2 = get_r2()
    force = os.environ.get("FORCE") == "1"
    limit = int(os.environ.get("LIMIT_STATIONS", "0"))

    stations = fetch_ea_stations()
    print(f"{len(stations)} EA stations with a daily rainfall measure")

    index = r2_get_json(r2, INDEX_KEY, {}) or {}
    refs = sorted(stations)
    if not force:
        refs = [r for r in refs if r not in index]
        print(f"{len(refs)} still to process ({len(index)} already in index)")
    if limit:
        refs = refs[:limit]
        print(f"LIMIT_STATIONS={limit} — processing {len(refs)}")

    session = requests.Session()
    session.mount(HYDRO_BASE, requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

    def process(ref):
        meta = stations[ref]
        text = fetch_station_daily_csv(session, meta["measure"])
        if not text:
            return ref, None
        days, n_good, n_valid = parse_daily_csv(text)
        stats = compute_climatology(days)
        del days, text  # raw data discarded before moving on
        if not stats:
            return ref, None
        stats["stationRef"] = ref
        stats["name"] = meta["name"]
        stats["dateOpened"] = meta["dateOpened"]
        stats["qualityGoodPct"] = round(100.0 * n_good / n_valid, 1) if n_valid else None
        stats["validDays"] = n_valid
        return ref, stats

    done, saved = 0, 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, ref): ref for ref in refs}
        for fut in as_completed(futures):
            ref = futures[fut]
            try:
                ref, stats = fut.result()
            except Exception as e:
                print(f"  {ref}: failed — {e}")
                stats = None
            done += 1
            if stats:
                r2_put_json(r2, stats, f"{CLIMATOLOGY_PFX}/{ref}.json")
                # Deliberately slim: the table page fetches this for every
                # station, so per-month normals and records stay in the
                # per-station files (loaded only by that gauge's detail page).
                index[ref] = {
                    "recordStart": stats["recordStart"],
                    "recordEnd": stats["recordEnd"],
                    "recordYears": stats["recordYears"],
                    "qualityGoodPct": stats["qualityGoodPct"],
                    "annualNormal": round(sum(
                        c["mean"] for c in stats["climatology"].values()), 1) if len(stats["climatology"]) == 12 else None,
                }
                saved += 1
            if done % 10 == 0 or done == len(refs):
                print(f"  {done}/{len(refs)} stations ({saved} saved)")
            if done % 50 == 0:
                r2_put_json(r2, index, INDEX_KEY)
                print(f"    checkpoint: index now {len(index)} stations")

    r2_put_json(r2, index, INDEX_KEY)
    print(f"\nDone: {saved}/{len(refs)} stations processed this run; index holds {len(index)}.")


if __name__ == "__main__":
    main()
