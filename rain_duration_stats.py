"""
rain_duration_stats.py — Short-duration (1h/3h/6h/12h) forward-tracked
rainfall records, plus the shared "how much history backs this" basis shape
used across the whole duration-record/percentile feature.

No sub-daily historical archive exists anywhere upstream (see
build_rain_climatology.py's day-based climatology) — raw 15-min readings are
pruned after fetch_rain.py's RETENTION_DAYS=14. So unlike the 24h+ durations,
which get genuine multi-decade context from the daily archives
(build_rain_climatology.py's compute_duration_climatology), these can only be
forward-tracked from live telemetry starting from whenever this module first
runs against a station. Every record produced here is "operational", not
"climatological" — see basis_operational() below — and must be shown as such.

Imported by make_rain_summary.py; not a standalone script.
"""
from datetime import datetime, timezone

# Rolling-window keys (must match WINDOWS_HOURS in make_rain_summary.py).
SHORT_DURATIONS = {"1h": 1, "3h": 3, "6h": 6, "12h": 12}

# Plausibility ceilings (mm) — set with headroom *above* known UK short-
# duration extremes so genuine record-breaking rain is never suppressed, not
# at the record itself. Starting values, not verified against Met Office's
# own published extremes — review before treating as final (flagged in the
# implementation plan). 24h reuses gauge.html's existing UK_DAILY_RECORD_MM.
CEILING_MM = {
    "1h": 100.0,    # UK 1h record ~92mm (Maidenhead, 1901)
    "3h": 160.0,
    "6h": 200.0,
    "12h": 260.0,
    "24h": 341.4,   # matches rain/gauge.html's UK_DAILY_RECORD_MM
}

# Below MIN_DAYS_FOR_DURATION_PERCENTILE (climatological, 24h+) or
# OPERATIONAL_SHORT_RECORD_DAYS (operational, 1h-12h) of history, a stat is
# stamped shortRecord=true. Shared with build_rain_climatology.py.
MIN_DAYS_FOR_DURATION_PERCENTILE = 365   # one seasonal cycle of overlapping windows
OPERATIONAL_SHORT_RECORD_DAYS = 90

# Spatial corroboration for a new all-time candidate that passes the ceiling
# check — a simple, documented heuristic (not gauge_qc.py's MAD-based check,
# deliberately decoupled — see plan). Too few neighbours to judge -> trust it
# rather than block records at isolated gauges.
NEIGHBOUR_MULTIPLIER = 4.0
NEIGHBOUR_MIN_FLOOR_MM = 5.0


# ── Shared "basis" metadata shape (see plan §4) ───────────────────────────────
def basis_operational(tracking_since, now):
    """tracking_since: 'YYYY-MM-DD' string. now: aware/naive UTC datetime."""
    since_dt = datetime.strptime(tracking_since, "%Y-%m-%d")
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    days = max(0, (now_naive - since_dt).days)
    return {
        "kind": "operational",
        "since": tracking_since,
        "days": days,
        "shortRecord": days < OPERATIONAL_SHORT_RECORD_DAYS,
    }


def basis_climatological(from_date, to_date, n):
    """from_date/to_date: 'YYYY-MM-DD' strings. n: count of valid windows."""
    try:
        years = round((datetime.strptime(to_date, "%Y-%m-%d")
                       - datetime.strptime(from_date, "%Y-%m-%d")).days / 365.25, 1)
    except (ValueError, TypeError):
        years = None
    return {
        "kind": "climatological",
        "from": from_date, "to": to_date, "years": years, "n": n,
        "shortRecord": n < MIN_DAYS_FOR_DURATION_PERCENTILE,
    }


def basis_proxy(midas_src_id, midas_name, match_dist_km):
    """NRW/MIDAS proxy — reuses the fields build_nrw_climatology.py already stamps."""
    return {
        "kind": "proxy",
        "midasSrcId": midas_src_id, "midasName": midas_name, "matchDistKm": match_dist_km,
        "shortRecord": False,  # the underlying MIDAS series is itself decades deep
    }


# ── Operational record tracking ───────────────────────────────────────────────
def _default_duration_entry(today_iso, now_iso, value):
    return {
        "allTime": None,
        "allTimeRejected": None,
        "todayOpen": {"mm": round(value, 2), "date": today_iso, "peakAt": now_iso},
        "provisional": False,
        "trackingSince": today_iso,
    }


def _neighbour_values(sid, neighbour_index, rolling_totals, window_key):
    vals = []
    for nb in neighbour_index.get(sid, []):
        t = rolling_totals.get(nb["id"])
        if t and t.get(window_key) is not None:
            vals.append(t[window_key])
    return vals


def _corroborated(value, neighbour_vals):
    """True if `value` is plausible given nearby concurrent readings, or if
    there simply aren't enough neighbours to judge (don't block an isolated
    gauge's genuine record just because it has few neighbours)."""
    if len(neighbour_vals) < 2:
        return True
    if value < NEIGHBOUR_MIN_FLOOR_MM:
        return True
    sorted_v = sorted(neighbour_vals)
    mid = sorted_v[len(sorted_v) // 2]
    if mid <= 0.5:
        return value <= NEIGHBOUR_MIN_FLOOR_MM * 2
    return value <= mid * NEIGHBOUR_MULTIPLIER


def _resolve_provisional(rec, sid, window_key, neighbour_index, rolling_totals):
    """A pending provisional candidate from an earlier run — check again this
    run in case neighbours have since caught up (corroborating a genuine
    fast-moving cell) or the reading still looks isolated (leave it pending;
    it is never auto-rejected here, only ever explicitly ceiling-rejected)."""
    cand = rec.get("_provisionalCandidate")
    if not cand:
        return False
    neighbour_vals = _neighbour_values(sid, neighbour_index, rolling_totals, window_key)
    if _corroborated(cand["mm"], neighbour_vals):
        current_max = rec["allTime"]["mm"] if rec.get("allTime") else -1.0
        if cand["mm"] > current_max:
            rec["allTime"] = {"mm": cand["mm"], "at": cand["at"], "trackingSince": rec.get("trackingSince")}
        rec["provisional"] = False
        rec.pop("_provisionalCandidate", None)
        return True
    return False


def update_duration_records(records, stations, rolling_totals, neighbour_index, now):
    """Per station x {1h,3h,6h,12h}: update all-time max / today's open peak.

    Returns (records, dirty) — dirty is True only if something actually
    changed this run, so the caller can skip the R2 PUT (on top of the
    existing MD5-skip in r2_put_json) when nothing moved.
    """
    stations_out = records.setdefault("stations", {})
    dirty = False
    today_iso = now.strftime("%Y-%m-%d")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for sid in stations:
        totals = rolling_totals.get(sid)
        if not totals:
            continue
        station_out = stations_out.setdefault(sid, {})
        for dur_key, hours in SHORT_DURATIONS.items():
            window_key = f"last{hours}h"
            value = totals.get(window_key)
            if value is None:
                continue

            rec = station_out.get(dur_key)
            if rec is None:
                # Seed today's open peak; allTime is left for the normal
                # candidate-evaluation path just below (current_max starts
                # at -1 for a brand new record, so this first value still
                # goes through the same ceiling/corroboration check as any
                # other candidate rather than being trusted unconditionally).
                rec = _default_duration_entry(today_iso, now_iso, value)
                station_out[dur_key] = rec
                dirty = True

            today_open = rec.get("todayOpen")
            if today_open is None or today_open.get("date") != today_iso:
                rec["todayOpen"] = {"mm": round(value, 2), "date": today_iso, "peakAt": now_iso}
                dirty = True
            elif value > today_open["mm"]:
                rec["todayOpen"] = {"mm": round(value, 2), "date": today_iso, "peakAt": now_iso}
                dirty = True

            current_max = rec["allTime"]["mm"] if rec.get("allTime") else -1.0
            if value <= current_max:
                if rec.get("provisional"):
                    dirty = _resolve_provisional(rec, sid, window_key, neighbour_index, rolling_totals) or dirty
                continue

            ceiling = CEILING_MM.get(dur_key)
            if ceiling and value > ceiling:
                rejected = rec.get("allTimeRejected")
                if not rejected or value > rejected["mm"]:
                    rec["allTimeRejected"] = {"mm": round(value, 2), "at": now_iso, "reason": "exceeds_ceiling"}
                    dirty = True
                continue

            neighbour_vals = _neighbour_values(sid, neighbour_index, rolling_totals, window_key)
            if _corroborated(value, neighbour_vals):
                rec["allTime"] = {"mm": round(value, 2), "at": now_iso,
                                   "trackingSince": rec.get("trackingSince", today_iso)}
                rec["provisional"] = False
                rec.pop("_provisionalCandidate", None)
                dirty = True
            else:
                # Only a genuinely new/changed pending candidate counts as a
                # change — an already-provisional value repeating run after
                # run (still uncorroborated, still the same reading) must not
                # mark dirty on every single run just because `now_iso`
                # ticked forward, or this file would never pass the
                # MD5-skip in r2_put_json while a candidate sits pending.
                prev = rec.get("_provisionalCandidate")
                if not rec.get("provisional") or not prev or prev["mm"] != round(value, 2):
                    rec["provisional"] = True
                    rec["_provisionalCandidate"] = {"mm": round(value, 2), "at": now_iso}
                    dirty = True

    records["generated_at"] = now_iso
    return records, dirty


def finalize_daily_peaks(records, peaks_archive, today_iso):
    """Roll any station/duration whose todayOpen has fallen behind the
    current UTC date into the cold peaks archive (keyed by that entry's own
    date, so a missed run of several days self-heals rather than losing
    data), then clear todayOpen so update_duration_records() reopens it
    fresh. Returns True if the archive changed (caller PUTs only then, on
    top of r2_put_json's own MD5-skip)."""
    changed = False
    for sid, durs in records.get("stations", {}).items():
        for dur_key, rec in durs.items():
            today_open = rec.get("todayOpen")
            if not today_open:
                continue
            d = today_open.get("date")
            if not d or d >= today_iso:
                continue
            day_bucket = peaks_archive.setdefault(sid, {}).setdefault(dur_key, {})
            if d not in day_bucket:
                day_bucket[d] = today_open["mm"]
                changed = True
            rec["todayOpen"] = None
    return changed
