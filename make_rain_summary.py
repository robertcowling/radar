#!/usr/bin/env python3
"""
make_rain_summary.py — Per-gauge rainfall duration totals, thresholds & records.

Computes, for every station in rain/stations.json:
  - rolling totals: last 1h / 3h / 6h / 24h / 7d (from raw readings, which are
    retained for RETENTION_DAYS=14 by fetch_rain.py — well within this window)
  - a heavy/extreme threshold level (bands agreed in the Flood Forecast
    project's templates/rain.html: 1h >10/30mm, 3h >20/40mm, 6h >30/50mm)
  - month-to-date / year-to-date totals and all-time records (wettest day,
    wettest month, driest month, longest dry spell) — sourced from a
    permanent daily-total archive this script maintains itself, since raw
    readings are pruned after 14 days and can't answer "this month" or
    "this year" once that window has passed.

Writes:
  rain/summary.json               — one rollup consumed by rain/table.html
                                     and rain/gauge.html
  rain/archive/daily_{YYYY}.json  — {stationId: {"YYYY-MM-DD": totalMm}},
                                     appended to once per day when the
                                     previous UTC day finalises

Called every 15 min by rain_update.yml, right after fetch_rain.py.
"""

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone

import boto3

# ── Cloudflare R2 ─────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

STATIONS_KEY  = "rain/stations.json"
META_KEY      = "rain/meta.json"
READINGS_PFX  = "rain/readings"
SUMMARY_KEY   = "rain/summary.json"
ARCHIVE_PFX   = "rain/archive"

# Rolling-window totals used both for the table/detail pages and for the
# heavy/extreme threshold check.
WINDOWS_HOURS = {"last1h": 1, "last3h": 3, "last6h": 6, "last12h": 12, "last24h": 24, "last48h": 48}
WINDOW_7D_DAYS = 7
# Matches fetch_rain.py's RETENTION_DAYS — the longest window raw readings
# support. Not surfaced in the UI; used only as an internal floor for
# month/year-to-date so a station whose own daily archive hasn't accumulated
# much history yet can't show a year-to-date total smaller than a shorter,
# more complete rolling window (see compute_records).
WINDOW_14D_DAYS = 14

# Heavy/extreme accumulation bands — agreed set, ported verbatim from the
# Flood Forecast project's templates/rain.html THRESHOLDS object.
THRESHOLD_BANDS = {  # window_hours: (heavy_mm, extreme_mm)
    1: (10.0, 30.0),
    3: (20.0, 40.0),
    6: (30.0, 50.0),
}

DRY_DAY_MM = 1.0          # a day with less than this total counts as "dry"
ARCHIVE_YEARS_LOOKBACK = 2  # load current + N previous years' archive files
MONTH_COMPLETENESS_MIN = 0.9  # fraction of a month's days needing an archived
                               # value before that month can be a wettest/
                               # driest-month candidate (mirrors
                               # build_rain_climatology.py's own rule)


def days_in_month(month_key):
    """month_key: 'YYYY-MM' -> number of days in that calendar month."""
    y, m = int(month_key[:4]), int(month_key[5:7])
    if m == 12:
        return 31
    return (date(y, m + 1, 1) - date(y, m, 1)).days


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
    except Exception as e:
        err_code = ""
        if hasattr(e, "response") and isinstance(e.response, dict):
            err_code = e.response.get("Error", {}).get("Code", "")
        if err_code not in ("NoSuchKey", "404", "NoSuchBucket"):
            print(f"Warning: R2 read error for {key}: {e}")
        return default


def r2_put_json(r2, data, key):
    """Class-A-budget-conscious JSON upload: skip the PUT if the object on
    R2 already has this exact content (ETag = body MD5 for single-part puts).
    Mirrors r2_upload()/r2_put_json() in make_accum_hourly.py."""
    body = json.dumps(data, separators=(",", ":")).encode()
    try:
        head = r2.head_object(Bucket=R2_BUCKET, Key=key)
        if head.get("ETag", "").strip('"') == hashlib.md5(body).hexdigest():
            return False
    except Exception:
        pass
    r2.put_object(
        Bucket=R2_BUCKET, Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache, max-age=0",
    )
    return True


def load_day_file(r2, date_key, cache):
    if date_key not in cache:
        cache[date_key] = r2_get_json(r2, f"{READINGS_PFX}/{date_key}.json", None)
    return cache[date_key]


def compute_rolling_totals(stations, r2, now, day_cache):
    """Per-station last1h/3h/6h/24h/7d/14d totals, summed slot-by-slot across
    the day files needed to cover a 14-day lookback (14d is internal-only,
    see WINDOW_14D_DAYS)."""
    totals = {sid: {k: 0.0 for k in list(WINDOWS_HOURS) + ["last7d", "last14d"]} for sid in stations}
    cutoffs = {k: now - timedelta(hours=h) for k, h in WINDOWS_HOURS.items()}
    cutoffs["last7d"] = now - timedelta(days=WINDOW_7D_DAYS)
    cutoffs["last14d"] = now - timedelta(days=WINDOW_14D_DAYS)

    for days_ago in range(WINDOW_14D_DAYS + 1):
        date = now - timedelta(days=days_ago)
        date_key = date.strftime("%Y%m%d")
        day_obj = load_day_file(r2, date_key, day_cache)
        if not day_obj:
            continue
        base = datetime.strptime(date_key, "%Y%m%d").replace(tzinfo=timezone.utc)
        for sid, slots in day_obj.get("readings", {}).items():
            if sid not in totals:
                continue
            station_totals = totals[sid]
            for slot_str, mm in slots.items():
                try:
                    ts = base + timedelta(minutes=int(slot_str) * 15)
                except (TypeError, ValueError):
                    continue
                if ts > now:
                    continue
                for key, cutoff in cutoffs.items():
                    if ts >= cutoff:
                        station_totals[key] += mm

    for sid, t in totals.items():
        for key in t:
            t[key] = round(t[key], 2)
    return totals


def threshold_level(totals):
    level = "normal"
    for hours, (heavy, extreme) in THRESHOLD_BANDS.items():
        val = totals.get(f"last{hours}h", 0.0)
        if val > extreme:
            return "extreme"
        if val > heavy:
            level = "heavy"
    return level


def load_archive_years(r2, now):
    """Returns (archives dict keyed by year int -> data, current_year_key)."""
    archives = {}
    for back in range(ARCHIVE_YEARS_LOOKBACK + 1):
        year = now.year - back
        key = f"{ARCHIVE_PFX}/daily_{year}.json"
        archives[year] = r2_get_json(r2, key, {}) or {}
    return archives


def finalize_yesterday(stations, r2, now, archives, day_cache):
    """Sum yesterday's full-day readings per station and merge into the
    appropriate year's archive. Only called when gated by the caller."""
    yest = now - timedelta(days=1)
    date_key = yest.strftime("%Y%m%d")
    date_iso = yest.strftime("%Y-%m-%d")
    year = yest.year

    day_obj = load_day_file(r2, date_key, day_cache)
    if not day_obj:
        return False

    archive = archives.setdefault(year, {})
    changed = False
    for sid, slots in day_obj.get("readings", {}).items():
        if sid not in stations or not slots:
            continue
        station_archive = archive.setdefault(sid, {})
        if date_iso in station_archive:
            continue  # already finalised
        total = round(sum(slots.values()), 2)
        station_archive[date_iso] = total
        changed = True
    return changed


def compute_records(stations, archives, now, rolling_totals=None):
    """Per-station month/year totals + all-time (within loaded archive
    years) records, from the persisted daily-total archive plus today's live rainfall."""
    this_month = now.strftime("%Y-%m")
    this_year = now.strftime("%Y")
    records = {}

    for sid in stations:
        days = {}
        for year_archive in archives.values():
            days.update(year_archive.get(sid, {}))
        
        last24h = (rolling_totals.get(sid, {}).get("last24h", 0.0)) if rolling_totals else 0.0
        last7d = (rolling_totals.get(sid, {}).get("last7d", 0.0)) if rolling_totals else 0.0
        last14d = (rolling_totals.get(sid, {}).get("last14d", 0.0)) if rolling_totals else 0.0

        # The largest rolling window guaranteed to sit fully inside the
        # current month/year (skip the first few days of either, when the
        # window can legitimately span the boundary and a floor would be
        # wrong) — prefer the longer 14d window, fall back to 7d.
        month_floor = last14d if now.day > WINDOW_14D_DAYS else (last7d if now.day > WINDOW_7D_DAYS else 0.0)
        year_floor = (last14d if now.timetuple().tm_yday > WINDOW_14D_DAYS
                      else (last7d if now.timetuple().tm_yday > WINDOW_7D_DAYS else 0.0))

        if not days:
            month_total = round(max(last24h, month_floor), 2)
            year_total = round(max(last24h, year_floor, month_total), 2)
            records[sid] = {
                "monthTotal": month_total, "yearTotal": year_total,
                "wettestDay": None, "wettestMonth": None, "driestMonth": None,
                "longestDrySpell": None, "recordsSince": None,
            }
            continue

        # Exclude unphysical corrupted daily gauge spikes (>250mm/day)
        valid_days = {d: v for d, v in days.items() if 0.0 <= v <= 250.0} or days

        archived_month = sum(v for d, v in valid_days.items() if d.startswith(this_month))
        archived_year = sum(v for d, v in valid_days.items() if d.startswith(this_year))

        # Ensure monthTotal and yearTotal include current 24hr live rainfall
        month_total = round(max(archived_month + last24h, last24h), 2)
        year_total = round(max(archived_year + last24h, month_total), 2)

        # The daily archive can under-report vs. the raw-reading-based rolling
        # totals (gaps, or a station whose archive only recently started) —
        # confirmed live on Bethesda Quarry (NRW): "2026 so far" showed 14.8mm
        # while "Last 7 Days" showed 21.0mm, which is impossible since the
        # last 7 days are always a subset of this year.
        month_total = round(max(month_total, month_floor), 2)
        year_total = round(max(year_total, year_floor, month_total), 2)

        wettest_date, wettest_mm = max(valid_days.items(), key=lambda kv: kv[1])

        by_month = {}
        month_day_counts = {}
        for d, v in valid_days.items():
            m = d[:7]
            by_month.setdefault(m, 0.0)
            by_month[m] += v
            month_day_counts[m] = month_day_counts.get(m, 0) + 1
        by_month = {m: round(v, 2) for m, v in by_month.items()}

        # A month only qualifies as a wettest/driest-month candidate once it
        # has both fully elapsed (excluded regardless of day count below) and
        # has enough of its days actually archived — otherwise a station with
        # e.g. 3 archived July days reports "wettest month = driest month =
        # July" fabricated from a month that's only 10% populated (confirmed
        # live on Bethesda Quarry, NRW: both were "2026-07, 7.6mm", the only
        # month with any data at all).
        complete_months = {
            m: v for m, v in by_month.items()
            if m != this_month and month_day_counts[m] / days_in_month(m) >= MONTH_COMPLETENESS_MIN
        }
        wettest_month_key = max(complete_months, key=complete_months.get) if complete_months else None
        driest_month_key = min(complete_months, key=complete_months.get) if complete_months else None

        # Longest run of *consecutive calendar days* (no gaps — a gap means
        # the gauge was offline, not confirmed dry) below DRY_DAY_MM.
        sorted_dates = sorted(valid_days)
        best_len, best_start = 0, None
        run_len, run_start = 0, None
        prev_date = None
        for d in sorted_dates:
            cur = datetime.strptime(d, "%Y-%m-%d")
            is_dry = valid_days[d] < DRY_DAY_MM
            contiguous = prev_date is not None and (cur - prev_date).days == 1
            if is_dry:
                run_len = run_len + 1 if contiguous else 1
                if run_len == 1:
                    run_start = d
                if run_len > best_len:
                    best_len, best_start = run_len, run_start
            else:
                run_len = 0
            prev_date = cur

        records[sid] = {
            "monthTotal": month_total,
            "yearTotal": year_total,
            "wettestDay": {"date": wettest_date, "mm": round(wettest_mm, 2)},
            "wettestMonth": ({"month": wettest_month_key, "mm": by_month[wettest_month_key]}
                             if wettest_month_key else None),
            "driestMonth": ({"month": driest_month_key, "mm": by_month[driest_month_key]}
                            if driest_month_key else None),
            "longestDrySpell": ({"days": best_len, "from": best_start} if best_len > 0 else None),
            "recordsSince": sorted_dates[0],
        }
    return records


def main():
    now = datetime.now(timezone.utc)
    r2 = get_r2() if USE_R2 else None
    if not USE_R2:
        print("Warning: R2 env vars not set — nothing to do (dry run has no local stations source)")
        return

    stations_data = r2_get_json(r2, STATIONS_KEY, None)
    if not stations_data or not stations_data.get("stations"):
        print("Error: rain/stations.json missing or empty — aborting")
        return
    stations = stations_data["stations"]
    print(f"Loaded {len(stations)} stations")

    day_cache = {}
    totals_by_station = compute_rolling_totals(stations, r2, now, day_cache)

    archives = load_archive_years(r2, now)

    # Finalise yesterday shortly after UTC midnight (matches fetch_rain.py's
    # own "fetch yesterday" gate), plus self-heal if yesterday is still
    # missing from the archive later in the day (covers a missed run).
    yest_iso = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yest_year = (now - timedelta(days=1)).year
    yest_missing = not any(
        yest_iso in archives.get(yest_year, {}).get(sid, {}) for sid in list(stations)[:50]
    )
    archives_changed = False
    if (now.hour == 0 and now.minute < 30) or yest_missing:
        archives_changed = finalize_yesterday(stations, r2, now, archives, day_cache)

    records_by_station = compute_records(stations, archives, now, totals_by_station)

    out_stations = {}
    for sid in stations:
        totals = totals_by_station.get(sid, {})
        rec = records_by_station.get(sid, {})
        out_stations[sid] = {
            **totals,
            "thresholdLevel": threshold_level(totals),
            **rec,
        }

    summary = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stations": out_stations,
    }
    r2_put_json(r2, summary, SUMMARY_KEY)
    print(f"Uploaded {SUMMARY_KEY} ({len(out_stations)} stations)")

    if archives_changed:
        year = yest_year
        r2_put_json(r2, archives[year], f"{ARCHIVE_PFX}/daily_{year}.json")
        print(f"Finalised {yest_iso} into daily_{year}.json archive")


if __name__ == "__main__":
    main()
