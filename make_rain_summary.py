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
  rain/neighbours.json            — cached k-nearest-neighbour index per
                                     station, rebuilt only when stations.json's
                                     coordinates change
  rain/duration_records.json      — forward-tracked 1h/3h/6h/12h all-time
                                     records per station (no history exists
                                     before this file started; see
                                     rain_duration_stats.py)
  rain/duration_peaks_{YYYY}.json — cold archive of one daily peak per
                                     station per short duration, appended to
                                     once per day
  rain/duration_stats.json        — per-station, per-duration (all 8: 1h to
                                     7d) cross-gauge percentile/record context
                                     for rain/gauge.html — kept separate from
                                     rain/summary.json so rain/table.html's
                                     every-load fetch doesn't carry data only
                                     the detail page uses

Called every 15 min by rain_update.yml, right after fetch_rain.py.
"""

import bisect
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone

import boto3

from rain_geo import build_neighbour_index, stations_content_hash
from rain_duration_stats import (
    CEILING_MM, basis_operational, update_duration_records, finalize_daily_peaks,
)

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

NEIGHBOURS_KEY        = "rain/neighbours.json"
DURATION_RECORDS_KEY  = "rain/duration_records.json"
DURATION_PEAKS_PFX    = "rain/duration_peaks"
DURATION_STATS_KEY    = "rain/duration_stats.json"
# Slim per-network percentile+max indexes written by the three
# build_*_climatology.py scripts (rain/duration_climatology_{ea,sepa,nrw}.json).
DURATION_CLIMATOLOGY_KEYS = {
    "ea": "rain/duration_climatology_ea.json",
    "sepa": "rain/duration_climatology_sepa.json",
    "nrw": "rain/duration_climatology_nrw.json",
}
NEIGHBOUR_RADIUS_KM = 50
NEIGHBOUR_K = 10

# All 8 durations rain/duration_stats.json carries cross-gauge context for.
# Distinct from WINDOWS_HOURS (internal 15-min-rollup keys) because it also
# needs a "7d" label matching rain/gauge.html's DURATIONS/rank-on-record
# framing, not "last7d".
DURATION_LABELS = {
    "1h": "last1h", "3h": "last3h", "6h": "last6h", "12h": "last12h",
    "24h": "last24h", "48h": "last48h", "72h": "last72h", "7d": "last7d",
}
CLIMATOLOGICAL_LABELS = {"24h", "48h", "72h", "7d"}

# Rolling-window totals used both for the table/detail pages and for the
# heavy/extreme threshold check.
WINDOWS_HOURS = {"last1h": 1, "last3h": 3, "last6h": 6, "last12h": 12, "last24h": 24, "last48h": 48, "last72h": 72}
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


EXPECTED_SLOTS_24H = 96  # one 15-min reading every slot across 24h (EA/NRW)


def compute_rolling_totals(stations, r2, now, day_cache):
    """Per-station last1h/3h/6h/24h/7d/14d totals, summed slot-by-slot across
    the day files needed to cover a 14-day lookback (14d is internal-only,
    see WINDOW_14D_DAYS), plus a completeness fraction (slots actually
    present vs EXPECTED_SLOTS_24H over the last 24h) used by rain/table.html's
    "Data gaps" filter — which previously always saw every station as 100%
    complete since nothing ever populated this field."""
    totals = {sid: {k: 0.0 for k in list(WINDOWS_HOURS) + ["last7d", "last14d"]} for sid in stations}
    slot_counts_24h = {sid: 0 for sid in stations}
    # SEPA gauges report hourly, not every 15 minutes (see fetch_rain.py's
    # `source` field and rain/index.html's detectCadence()) — a
    # fully-reporting SEPA station only ever fills 24 of the 96 slots
    # EXPECTED_SLOTS_24H assumes, so completeness read ~0.25 for every SEPA
    # gauge regardless of actual data quality, which permanently failed
    # rain/table.html's default "Max 10% gaps" (90%) filter for the entire
    # network. Same bug make_gauge_bias.py had (fixed 2026-07, same
    # `cadence_divisor` naming) — no shared cadence-detection utility exists
    # yet, so each pipeline still scales for it independently.
    cadence_divisor = {sid: (4 if stations.get(sid, {}).get("source") == "sepa" else 1) for sid in stations}
    cutoffs = {k: now - timedelta(hours=h) for k, h in WINDOWS_HOURS.items()}
    cutoffs["last7d"] = now - timedelta(days=WINDOW_7D_DAYS)
    cutoffs["last14d"] = now - timedelta(days=WINDOW_14D_DAYS)
    cutoff_24h = cutoffs["last24h"]

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
                if ts >= cutoff_24h:
                    slot_counts_24h[sid] += 1

    for sid, t in totals.items():
        for key in t:
            t[key] = round(t[key], 2)
        expected = EXPECTED_SLOTS_24H / cadence_divisor[sid]
        t["completeness"] = round(min(1.0, slot_counts_24h[sid] / expected), 3)
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

        # Exclude unphysical corrupted daily gauge spikes. Was a hardcoded
        # 250mm — below the real UK daily record (341.4mm, Honister Pass,
        # 5 Dec 2015, the same UK_DAILY_RECORD_MM rain/gauge.html already
        # treats as plausible), which would have silently rejected a
        # genuine UK-record-breaking day. Now shares CEILING_MM["24h"] with
        # the short-duration record tracker (rain_duration_stats.py) so
        # there is one ceiling value, not two disagreeing ones.
        valid_days = {d: v for d, v in days.items() if 0.0 <= v <= CEILING_MM["24h"]} or days

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


def get_or_build_neighbour_index(r2, stations):
    """Cached k-nearest-neighbour index (see rain_geo.build_neighbour_index).
    Rebuilding is ~1.7M haversine pairs for ~1,300 stations — cheap once, but
    not something to redo every 15-min run, so it's gated on a content hash
    of station coordinates rather than any fixed cadence."""
    cur_hash = stations_content_hash(stations)
    cached = r2_get_json(r2, NEIGHBOURS_KEY, None)
    if cached and cached.get("stations_hash") == cur_hash and cached.get("index"):
        return cached["index"]
    print("  Rebuilding neighbour index (stations.json coordinates changed or no cache)...")
    index = build_neighbour_index(stations, k=NEIGHBOUR_K, radius_km=NEIGHBOUR_RADIUS_KM)
    r2_put_json(r2, {"stations_hash": cur_hash, "index": index}, NEIGHBOURS_KEY)
    return index


def _climatological_band(value, percentiles):
    """value against a station's own {p50,p75,p90,p95,p99} ladder -> a coarse
    band string, not a false-precision percentile (see plan §1.3 — with
    decades of daily data almost every meaningful rain event already sits
    above p99, so a literal number is both alarmist and uninformative)."""
    if not percentiles:
        return None
    order = [("p50", percentiles.get("p50")), ("p75", percentiles.get("p75")),
             ("p90", percentiles.get("p90")), ("p95", percentiles.get("p95")),
             ("p99", percentiles.get("p99"))]
    order = [(label, v) for label, v in order if v is not None]
    if not order:
        return None
    if value < order[0][1]:
        return f"<{order[0][0]}"
    for i in range(len(order) - 1):
        if order[i][1] <= value < order[i + 1][1]:
            return f"{order[i][0]}-{order[i + 1][0]}"
    return f">{order[-1][0]}"


def _network_of(stations, sid):
    return stations.get(sid, {}).get("source") or "ea"


def compute_cross_gauge_percentiles(stations, totals_by_station, neighbour_index,
                                     duration_climatology_by_network, duration_records, now):
    """Per station, per duration (all 8: 1h-7d): this-run rank among all UK
    stations and among this station's neighbours, a climatological percentile
    band (24h+ only, from the per-network duration-climatology index), and
    neighboursRecordsExceeded (how many of this station's neighbours' own
    all-time records this live value beats — a richer "relative to nearby
    gauges" framing than a same-run rank alone, see plan §3)."""
    out = {sid: {} for sid in stations}
    today_iso = now.strftime("%Y-%m-%d")

    for label, window_key in DURATION_LABELS.items():
        pairs = [(sid, t[window_key]) for sid, t in totals_by_station.items()
                  if t.get(window_key) is not None]
        values_sorted = sorted(v for _, v in pairs)
        n = len(values_sorted)
        values_by_id = dict(pairs)

        for sid, value in pairs:
            cadence = "hourly" if stations.get(sid, {}).get("source") == "sepa" else "15min"
            idx = bisect.bisect_right(values_sorted, value)
            rank_uk = round(100.0 * idx / n, 1) if n > 1 else None

            neighbours = neighbour_index.get(sid, [])
            nb_vals = sorted(values_by_id[nb["id"]] for nb in neighbours if nb["id"] in values_by_id)
            rank_neighbours = (round(100.0 * bisect.bisect_right(nb_vals, value) / len(nb_vals), 1)
                                if len(nb_vals) > 1 else None)

            entry = {"value": round(value, 2), "rankUk": rank_uk,
                     "rankNeighbours": rank_neighbours, "cadence": cadence}

            if label in CLIMATOLOGICAL_LABELS:
                net = _network_of(stations, sid)
                station_dc = duration_climatology_by_network.get(net, {}).get(sid, {}).get(label)
                entry["climPercentileBand"] = (
                    _climatological_band(value, station_dc.get("percentiles")) if station_dc else None)
            else:
                rec = duration_records.get("stations", {}).get(sid, {}).get(label)
                if rec:
                    rec = dict(rec)
                    rec.pop("_provisionalCandidate", None)  # internal bookkeeping only
                    rec["basis"] = basis_operational(rec.get("trackingSince", today_iso), now)
                entry["record"] = rec

            # neighboursRecordsExceeded: how many neighbours' *own* all-time
            # records this live value beats — for 1h-12h from duration_records
            # (this run's own consolidated file), for 24h+ from each
            # neighbour's own network's slim duration-climatology index.
            exceeded, of = 0, 0
            for nb in neighbours:
                nb_id = nb["id"]
                if label in CLIMATOLOGICAL_LABELS:
                    nb_net = _network_of(stations, nb_id)
                    nb_dc = duration_climatology_by_network.get(nb_net, {}).get(nb_id, {}).get(label)
                    nb_max = nb_dc.get("max", {}).get("mm") if nb_dc else None
                else:
                    nb_rec = duration_records.get("stations", {}).get(nb_id, {}).get(label)
                    nb_max = nb_rec.get("allTime", {}).get("mm") if nb_rec and nb_rec.get("allTime") else None
                if nb_max is None:
                    continue
                of += 1
                if value > nb_max:
                    exceeded += 1
            entry["neighboursRecordsExceeded"] = {"count": exceeded, "of": of} if of else None

            out[sid][label] = entry

    return out


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

    # ── Short-duration (1h/3h/6h/12h) operational records ──────────────────
    neighbour_index = get_or_build_neighbour_index(r2, stations)

    duration_records = r2_get_json(r2, DURATION_RECORDS_KEY, {"stations": {}}) or {"stations": {}}
    duration_records, records_dirty = update_duration_records(
        duration_records, stations, totals_by_station, neighbour_index, now)

    # Roll any station/duration whose "today" has fallen behind into the cold
    # per-year peaks archive — cheap in-memory no-op most runs (only changes
    # once/day per station/duration), gated the same way finalize_yesterday()
    # already is above rather than a fixed schedule, so a missed run self-heals.
    today_iso = now.strftime("%Y-%m-%d")
    peaks_year = now.year
    peaks_key = f"{DURATION_PEAKS_PFX}_{peaks_year}.json"
    peaks_archive = r2_get_json(r2, peaks_key, {}) or {}
    peaks_changed = finalize_daily_peaks(duration_records, peaks_archive, today_iso)
    # A UTC-year rollover can leave yesterday's peak needing the *prior*
    # year's file — finalize_daily_peaks only ever archives under the entry's
    # own date, so re-check against last year's file too on Jan 1st.
    if now.month == 1 and now.day == 1:
        prev_key = f"{DURATION_PEAKS_PFX}_{peaks_year - 1}.json"
        prev_archive = r2_get_json(r2, prev_key, {}) or {}
        if finalize_daily_peaks(duration_records, prev_archive, today_iso):
            r2_put_json(r2, prev_archive, prev_key)

    if records_dirty:
        r2_put_json(r2, duration_records, DURATION_RECORDS_KEY)
        print(f"Uploaded {DURATION_RECORDS_KEY}")
    if peaks_changed:
        r2_put_json(r2, peaks_archive, peaks_key)
        print(f"Uploaded {peaks_key}")

    # ── Cross-gauge percentiles (nearby + UK-wide) for all 8 durations ─────
    duration_climatology_by_network = {
        net: (r2_get_json(r2, key, {}) or {}) for net, key in DURATION_CLIMATOLOGY_KEYS.items()
    }
    duration_stats = compute_cross_gauge_percentiles(
        stations, totals_by_station, neighbour_index,
        duration_climatology_by_network, duration_records, now)
    r2_put_json(r2, {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stations": duration_stats},
                DURATION_STATS_KEY)
    print(f"Uploaded {DURATION_STATS_KEY} ({len(duration_stats)} stations)")


if __name__ == "__main__":
    main()
