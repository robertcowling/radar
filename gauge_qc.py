#!/usr/bin/env python3
"""
gauge_qc.py — Real-time rain gauge quality control.

Implements the RainGaugeQC framework of Ośródka, Otop & Szturc (2022),
AMT 15, 5581–5597, https://doi.org/10.5194/amt-15-5581-2022, adapted for
UK single-sensor tipping-bucket gauges and the available data environment.

Checks and their paper mapping:
  1. Gross error     (GEC) — single slot ≥25 mm suspicious, ≥50 mm impossible
  2. Range check     (RC)  — exceeds seasonal climatological extreme (UK-wide 1% AEP)
  3. Temporal        (TCC) — isolated spike vs preceding 7-slot median
  4. Spatial         (SCC) — MAD-based outlier within a 100 km domain
  5. Persistent zero (RCC) — stuck at 0 while neighbours/radar show rain
  6. Radar conformity(RCC) — gauge far exceeds radar QPE at same location
  7. Pattern              — near-constant low-amplitude tips (no paper equivalent)
  8. Accumulation anomaly — 24 hr gauge total >> neighbour mean (extended SCC)

A continuous Quality Index (QI, 0.0–1.0) is computed from all check results,
following the QI framework in Ośródka et al. 2022 with UK-specific adaptations:
  · No dual-sensor TCC: UK EA telemetry gauges are single tipping buckets;
    the heated/unheated sensor agreement step cannot be applied.
  · No per-station RC climatology: UK-wide seasonal estimates replace
    30-year per-station exceedance probabilities (unavailable).
  · No terrain-dependent RCC: flat QI deductions used instead of the
    lowland/foothill/mountain weighting in the paper.

An LLM (OpenAI) is called for an independent assessment when ≥1 check fails
or the reading is ≥20 mm.  Re-assessed every 4 consecutive flags (~1 hr).
Gracefully skipped if OPENAI_API_KEY is absent or openai is not installed.
"""

import io, json, math, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from math import atan2, cos, radians, sin, sqrt

import boto3
import h5py
import numpy as np
from botocore import UNSIGNED
from botocore.client import Config
from pyproj import Transformer

# ── UK-calibrated QC thresholds ────────────────────────────────────────────────
HARD_REJECT_MM       = 60.0   # beyond any credible UK 15-min gauge total (UK 30-min record ≈80 mm)
HARD_SUSPECT_MM      = 35.0   # extremely rare for 15 min (~T50–100 for most UK sites)

TEMPORAL_WINDOW      = 7      # preceding slots to compare against (7 × 15 min = 1 h 45)
TEMPORAL_SPIKE_RATIO = 8.0    # ratio above which an isolated spike is flagged
TEMPORAL_ZERO_TRIGGER = 5.0   # mm — flag spike from zero above this
TEMPORAL_MIN_FLAG_MM = 5.0    # don't flag spikes below this absolute value (real showers)

# 20 km neighbourhood — used for persistent-zero and accumulation checks
NN_RADIUS_KM         = 20.0   # radius for find_neighbours() helper

# ── Range Check (RC) — Ośródka et al. 2022 ─────────────────────────────────────
# UK-wide seasonal 15-min extreme thresholds (~T20–50 return period).
# Per-station 30-year climatological data is unavailable; UK-wide estimates used.
# Evidence: UK 5-min record ≈32 mm (Preston, 1893), 30-min record ≈80 mm
# (Eskdalemuir, 1953) — 15-min totals of 35–50 mm are plausible in extreme events.
# FEH/ReFH T100 for 15-min: ~20–35 mm SE England, ~35–55 mm high-rainfall areas.
RANGE_WARM_SUSPECT_MM = 25.0  # Apr–Sep: approx T20–50 across UK
RANGE_COLD_SUSPECT_MM = 15.0  # Oct–Mar: rarer convective events; lower upper bound

# ── Spatial Consistency Check (SCC) — Ośródka et al. 2022 ──────────────────────
SCC_RADIUS_KM    = 100.0  # 100 km domain radius (as per paper)
SCC_MIN_DOMAIN_N = 5      # minimum domain stations required (paper: 3)
SCC_TRIGGER_MM   = 1.0    # skip if gauge reading is below this
SCC_DI_WEAK      = 4.0    # Di > this → weak outlier (~P90)
SCC_DI_MEDIUM    = 8.0    # Di > this → medium outlier (~P95)
SCC_DI_STRONG    = 15.0   # Di > this → strong outlier (~P99)
SCC_MAD_FLOOR    = 0.5    # minimum scale denominator (prevents zero-division)

PZERO_WINDOW_SLOTS   = 24     # look-back window for persistent-zero check (6 hrs)
PZERO_NBR_TRIGGER_MM = 3.0    # mean mm a neighbour must show to count as "active"
PZERO_MIN_ACTIVE     = 3      # at least N neighbours must be active

RADAR_TRIGGER_MM     = 5.0    # only apply radar check above this gauge reading
RADAR_RATIO_LIMIT    = 15.0   # gauge/radar ratio above which it is suspicious
RADAR_MAX_EST_MM     = 0.5    # radar 5 km estimate must be below this to flag

LLM_TRIGGER_MM       = 20.0   # also call LLM if reading ≥ this (even if checks pass)
LLM_MIN_FAILURES     = 1      # require this many check failures to trigger LLM (avoids calls during real rain)

DRIP_WINDOW_SLOTS    = 24     # 6 hr look-back for drip/leak pattern check
DRIP_MIN_NONZERO     = 18     # need 18/24 non-zero slots (~4.5 hr continuous) before flagging
DRIP_MAX_MEAN_MM     = 1.5    # pattern only meaningful at low intensities
DRIP_MAX_CV          = 0.15   # coefficient of variation below this = suspiciously uniform

ACCUM_WINDOW_SLOTS   = 96     # 24 hr accumulation window
ACCUM_MIN_TOTAL_MM   = 15.0   # ignore gauges with trivially low totals
ACCUM_RATIO_LIMIT    = 5.0    # gauge 24 hr total / neighbour mean above this = anomalous
ACCUM_MIN_NBHRS      = 3      # minimum neighbours with data to apply check
ACCUM_MIN_NBR_TOTAL_MM = 5.0  # exclude near-dry neighbours from the comparison mean

# ── Storage keys ───────────────────────────────────────────────────────────────
OUTPUT_KEY        = "gaugecheck/results.json"
LOG_KEY           = "gaugecheck/log.json"
MANIFEST_KEY      = "gaugecheck/manifest.json"
RUNS_KEY_TPL      = "gaugecheck/runs/{ts}.json"
MAX_RUNS          = 672  # 7 days at 15-min cadence
LOG_RETENTION_HRS = 2160   # rolling event log retention (90 days / approx 3 months)
READINGS_PFX      = "rain/readings"
STATIONS_KEY      = "rain/stations.json"
META_KEY          = "rain/meta.json"
MET_OFFICE_BUCKET = "met-office-radar-obs-data"

# ── R2 credentials ─────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL])

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "") or "gpt-5.4"


# ── R2 helpers ─────────────────────────────────────────────────────────────────
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
    except Exception:
        return default


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")


# ── Geometry helpers ───────────────────────────────────────────────────────────
def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _point_in_ring(lon, lat, ring):
    n, inside, j = len(ring), False, len(ring) - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_feature(lon, lat, feature):
    geom = feature["geometry"]
    if geom["type"] == "Polygon":
        return _point_in_ring(lon, lat, geom["coordinates"][0])
    if geom["type"] == "MultiPolygon":
        return any(_point_in_ring(lon, lat, poly[0]) for poly in geom["coordinates"])
    return False


def build_county_lookup(geojson_path):
    with open(geojson_path) as f:
        data = json.load(f)
    return [
        (feat["properties"].get("name", ""),
         feat["properties"].get("region", ""),
         feat)
        for feat in data["features"]
    ]


def lookup_county(county_features, lon, lat):
    for name, region, feat in county_features:
        if _point_in_feature(lon, lat, feat):
            return name, region
    return None, None


# ── Data loading ───────────────────────────────────────────────────────────────
def parse_latest_time(meta):
    ts = meta.get("latest_time", "")
    if not ts:
        return None, None, None
    # meta stores without timezone; treat as UTC
    ts_clean = ts.replace("Z", "").split("+")[0]
    dt = datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    date_str = dt.strftime("%Y%m%d")
    slot_idx = dt.hour * 4 + dt.minute // 15
    return dt, date_str, slot_idx


def load_day(r2, date_str):
    data = r2_get_json(r2, f"{READINGS_PFX}/{date_str}.json")
    return data.get("readings", {}) if data else {}


def load_radar_day(r2, date_str):
    data = r2_get_json(r2, f"rain/radar_readings/{date_str}.json")
    return data if data else {"readings_5km": {}, "readings_1km": {}}


def get_station_slots(days, station_id, end_date_str, end_slot, n):
    """Return n slot values oldest-first ending at (end_date_str, end_slot) inclusive."""
    result = []
    d = datetime.strptime(end_date_str, "%Y%m%d")
    slot = end_slot
    # Handle negative slot from caller arithmetic (e.g. latest_slot - 1 when slot==0)
    while slot < 0:
        slot += 96
        d -= timedelta(days=1)
    for _ in range(n):
        date_str = d.strftime("%Y%m%d")
        raw = days.get(date_str, {}).get(station_id, {}).get(str(slot))
        result.append(float(raw) if isinstance(raw, (int, float)) else None)
        slot -= 1
        if slot < 0:
            slot = 95
            d -= timedelta(days=1)
    return list(reversed(result))


# ── QC checks ──────────────────────────────────────────────────────────────────
def check_hard_threshold(value):
    if value >= HARD_REJECT_MM:
        return {"passed": False, "reason": "impossible",
                "value_mm": value, "limit_mm": HARD_REJECT_MM}
    if value >= HARD_SUSPECT_MM:
        return {"passed": False, "reason": "exceeds_uk_record",
                "value_mm": value, "limit_mm": HARD_SUSPECT_MM}
    return {"passed": True}


def check_range(value, month):
    """Range Check (RC) — Ośródka et al. 2022.

    Flags readings that exceed seasonal climatological extremes.  In the paper
    this uses per-station 30-year historical statistics; for UK telemetry gauges
    that data is unavailable, so UK-wide warm/cold season thresholds are used
    (see RANGE_WARM/COLD_SUSPECT_MM constants).  Warm season = Apr–Sep.
    """
    warm = 4 <= month <= 9
    threshold = RANGE_WARM_SUSPECT_MM if warm else RANGE_COLD_SUSPECT_MM
    if value >= threshold:
        return {
            "passed": False,
            "reason": "exceeds_seasonal_range",
            "value_mm": value,
            "threshold_mm": threshold,
            "season": "warm" if warm else "cold",
        }
    return {"passed": True, "threshold_mm": threshold}


def check_temporal(value, history_7):
    """history_7: the 7 slots immediately preceding the current one (oldest first)."""
    valid = [v for v in history_7 if v is not None and v >= 0]
    if len(valid) < 3:
        return {"passed": True, "skipped": "insufficient_history"}
    median_prev = sorted(valid)[len(valid) // 2]
    if value < TEMPORAL_MIN_FLAG_MM:
        return {"passed": True, "skipped": "below_min_absolute"}
    if median_prev < 0.01:
        if value >= TEMPORAL_ZERO_TRIGGER:
            return {"passed": False, "reason": "spike_from_zero",
                    "value_mm": value, "preceding_median_mm": 0.0}
        return {"passed": True}
    ratio = value / median_prev
    if ratio >= TEMPORAL_SPIKE_RATIO:
        return {"passed": False, "reason": "isolated_spike",
                "value_mm": value, "preceding_median_mm": round(median_prev, 2),
                "spike_ratio": round(ratio, 1)}
    return {"passed": True, "spike_ratio": round(ratio, 1)}


def find_neighbours(station_id, stations, latest_readings, radius_km=NN_RADIUS_KM, k=5):
    s = stations[station_id]
    nbrs = []
    for sid, st in stations.items():
        if sid == station_id:
            continue
        dist = _haversine_km(s["lat"], s["lon"], st["lat"], st["lon"])
        if dist <= radius_km:
            nbrs.append((sid, round(dist, 1), st.get("name", sid),
                         latest_readings.get(sid)))
    nbrs.sort(key=lambda x: x[1])
    return nbrs[:k]


def check_spatial_consistency(value, station_id, stations, latest_readings):
    """Spatial Consistency Check (SCC) — Ośródka et al. 2022.

    Detects spatial outliers using a MAD-based deviation index (Di) over a
    100 km domain.  Outliers are classified weak/medium/strong using fixed Di
    thresholds chosen to approximate the empirical P90/P95/P99 percentiles used
    in the paper.  Note: the paper also uses four offset 25 km sub-domains for
    robustness; we use a single domain here.

    UK adaptation: without dual-sensor TCC confirmation, isolated genuine
    convective events may receive a mild QI reduction from this check — the
    LLM assessment provides contextual disambiguation.
    """
    if value < SCC_TRIGGER_MM:
        return {"passed": True, "skipped": "below_trigger"}

    s = stations[station_id]
    domain_vals = []
    for sid, st in stations.items():
        if sid == station_id:
            continue
        dist = _haversine_km(s["lat"], s["lon"], st["lat"], st["lon"])
        if dist <= SCC_RADIUS_KM:
            v = latest_readings.get(sid)
            if v is not None:
                domain_vals.append(v)

    if len(domain_vals) < SCC_MIN_DOMAIN_N:
        return {"passed": True, "skipped": "insufficient_domain_stations",
                "n_found": len(domain_vals)}

    n = len(domain_vals)
    vals_sorted = sorted(domain_vals)
    q50 = vals_sorted[n // 2]
    mad = sorted(abs(v - q50) for v in domain_vals)[n // 2]

    scale = max(mad, SCC_MAD_FLOOR)
    di = abs(value - q50) / scale

    if di > SCC_DI_STRONG:
        outlier_class = "strong"
    elif di > SCC_DI_MEDIUM:
        outlier_class = "medium"
    elif di > SCC_DI_WEAK:
        outlier_class = "weak"
    else:
        return {
            "passed": True,
            "di": round(di, 1),
            "domain_median_mm": round(q50, 2),
            "domain_n": n,
        }

    return {
        "passed": False,
        "reason": "spatial_outlier",
        "outlier_class": outlier_class,
        "di": round(di, 1),
        "value_mm": value,
        "domain_median_mm": round(q50, 2),
        "domain_mad": round(mad, 3),
        "domain_n": n,
    }


def check_persistent_zero(value, history_pzero, nbr_6hr_means, radar_mm):
    if value > 0.2:
        return {"passed": True, "skipped": "gauge_nonzero"}
    valid = [v for v in history_pzero if v is not None]
    if len(valid) < PZERO_WINDOW_SLOTS // 2:
        return {"passed": True, "skipped": "insufficient_history"}
    zero_frac = sum(1 for v in valid if v < 0.1) / len(valid)
    if zero_frac < 0.8:
        return {"passed": True, "skipped": "not_persistently_zero"}
    active = [m for m in nbr_6hr_means
              if m is not None and m >= PZERO_NBR_TRIGGER_MM]
    if len(active) < PZERO_MIN_ACTIVE:
        return {"passed": True, "skipped": "neighbours_also_dry"}
    radar_flag = radar_mm is not None and radar_mm >= 2.0
    return {
        "passed": False,
        "reason": "stuck_zero",
        "zero_slots_in_window": sum(1 for v in valid if v < 0.1),
        "window_slots": len(valid),
        "active_neighbours": len(active),
        "radar_mm_15min": round(radar_mm, 2) if radar_mm is not None else None,
        "radar_confirms": radar_flag,
    }


def check_radar(value, radar_mm):
    if radar_mm is None:
        return {"passed": True, "skipped": "radar_unavailable"}
    if value < RADAR_TRIGGER_MM:
        return {"passed": True, "skipped": "below_trigger"}
    ratio = value / max(radar_mm, 0.01)
    if radar_mm < RADAR_MAX_EST_MM and ratio > RADAR_RATIO_LIMIT:
        return {"passed": False, "reason": "radar_mismatch",
                "gauge_mm": value,
                "radar_mm_15min": round(radar_mm, 3),
                "ratio": round(ratio, 0)}
    return {"passed": True,
            "gauge_mm": value,
            "radar_mm_15min": round(radar_mm, 3),
            "ratio": round(ratio, 1)}


def check_drip_leak(history_drip, nbr_mean_mm=None):
    """Detect a blocked or leaking tipping bucket producing near-constant small tips.

    Classic signature: 8+ consecutive non-zero slots all in a tight range, at low
    intensity — physically implausible because real rain varies considerably in rate.
    history_drip: last DRIP_WINDOW_SLOTS values (oldest first), may contain None.
    nbr_mean_mm: mean 15-min reading across nearby neighbours over the drip window;
                 if neighbours are also consistently wet, uniform low readings may be
                 genuine widespread drizzle rather than a faulty gauge.
    """
    valid = [v for v in history_drip if v is not None and v > 0]
    if len(valid) < DRIP_MIN_NONZERO:
        return {"passed": True, "skipped": "insufficient_nonzero_slots"}
    mean_val = sum(valid) / len(valid)
    if mean_val > DRIP_MAX_MEAN_MM:
        return {"passed": True, "skipped": "intensity_too_high_for_drip"}
    # Coefficient of variation: std / mean
    variance = sum((v - mean_val) ** 2 for v in valid) / len(valid)
    std_val  = variance ** 0.5
    cv       = std_val / max(mean_val, 0.01)
    # Also check for perfectly identical consecutive values (stuck counter)
    longest_identical_run = 1
    cur_run = 1
    for i in range(1, len(valid)):
        if abs(valid[i] - valid[i - 1]) < 0.001:
            cur_run += 1
            longest_identical_run = max(longest_identical_run, cur_run)
        else:
            cur_run = 1
    if cv < DRIP_MAX_CV:
        # Suppress if neighbours are also consistently wet — could be widespread drizzle.
        # Stuck-counter case (cv==0) is still flagged regardless.
        if cv > 0 and nbr_mean_mm is not None and nbr_mean_mm >= 0.15:
            return {"passed": True, "skipped": "widespread_light_rain",
                    "mean_mm": round(mean_val, 3), "cv": round(cv, 3),
                    "nbr_mean_mm": round(nbr_mean_mm, 3)}
        return {
            "passed": False,
            "reason": "uniform_drip_pattern",
            "nonzero_slots": len(valid),
            "mean_mm": round(mean_val, 3),
            "cv": round(cv, 3),
            "longest_identical_run": longest_identical_run,
        }
    if longest_identical_run >= 12:
        return {
            "passed": False,
            "reason": "stuck_counter",
            "nonzero_slots": len(valid),
            "mean_mm": round(mean_val, 3),
            "longest_identical_run": longest_identical_run,
        }
    return {"passed": True, "mean_mm": round(mean_val, 3), "cv": round(cv, 3)}


def check_accumulation_anomaly(station_id, history_96, neighbours, days,
                               latest_date_str, latest_slot):
    """Compare 24 hr gauge total against nearest neighbours' 24 hr totals.

    A gauge accumulating far more than all its neighbours over 24 hours suggests
    systematic over-recording (e.g., continuous drip, faulty counter).
    """
    gauge_total = sum(v for v in history_96 if v is not None and v >= 0)
    if gauge_total < ACCUM_MIN_TOTAL_MM:
        return {"passed": True, "skipped": "total_below_threshold",
                "gauge_24hr_mm": round(gauge_total, 1)}

    nbr_totals = []
    for nbr_id, _, _, _ in neighbours:
        h96 = get_station_slots(days, nbr_id, latest_date_str, latest_slot,
                                ACCUM_WINDOW_SLOTS)
        t = sum(v for v in h96 if v is not None and v >= 0)
        if t >= ACCUM_MIN_NBR_TOTAL_MM:
            nbr_totals.append(t)

    if len(nbr_totals) < ACCUM_MIN_NBHRS:
        return {"passed": True, "skipped": "insufficient_neighbour_data",
                "gauge_24hr_mm": round(gauge_total, 1)}

    nbr_mean = sum(nbr_totals) / len(nbr_totals)
    if nbr_mean < 3.0:
        return {"passed": True, "skipped": "neighbours_also_dry",
                "gauge_24hr_mm": round(gauge_total, 1)}

    ratio = gauge_total / nbr_mean
    if ratio > ACCUM_RATIO_LIMIT:
        return {
            "passed": False,
            "reason": "accumulation_far_exceeds_neighbours",
            "gauge_24hr_mm": round(gauge_total, 1),
            "neighbour_mean_24hr_mm": round(nbr_mean, 1),
            "ratio": round(ratio, 1),
            "n_neighbours": len(nbr_totals),
        }
    return {"passed": True,
            "gauge_24hr_mm": round(gauge_total, 1),
            "neighbour_mean_24hr_mm": round(nbr_mean, 1),
            "ratio": round(ratio, 1)}


# ── Radar H5 loading ──────────────────────────────────────────────────────────
def build_radar_extractor(h5_path):
    """Return extract(lat, lon, radius_km=5.0) -> mm/15min, or None on failure."""
    try:
        with h5py.File(h5_path, "r") as f:
            where    = f["where"].attrs
            xsize    = int(where["xsize"])
            ysize    = int(where["ysize"])
            xscale   = float(where["xscale"])
            yscale   = float(where["yscale"])
            proj_str = where["projdef"]
            if isinstance(proj_str, bytes):
                proj_str = proj_str.decode()
            ul_lon = float(where["UL_lon"])
            ul_lat = float(where["UL_lat"])
            raw   = f["dataset1/data1/data"][:]
            dwhat = f["dataset1/data1/what"].attrs
            gain, offset = float(dwhat["gain"]), float(dwhat["offset"])
            nodata, undetect = float(dwhat["nodata"]), float(dwhat["undetect"])

        rate = np.zeros_like(raw, dtype=np.float32)
        good = (raw != nodata) & (raw != undetect)
        rate[good] = (raw[good].astype(np.float32) * gain + offset).clip(min=0)

        ll_to_radar = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
        ul_x, ul_y  = ll_to_radar.transform(ul_lon, ul_lat)

        def extract(lat, lon, radius_km=5.0):
            gx, gy = ll_to_radar.transform(lon, lat)
            col_c  = round((gx - ul_x) / xscale)
            row_c  = round((ul_y - gy) / yscale)
            if not (0 <= col_c < xsize and 0 <= row_c < ysize):
                return None
            r_p = max(1, round(radius_km * 1000 / xscale))
            r0, r1 = max(0, row_c - r_p), min(ysize - 1, row_c + r_p)
            c0, c1 = max(0, col_c - r_p), min(xsize - 1, col_c + r_p)
            patch = rate[r0:r1 + 1, c0:c1 + 1]
            vals  = patch[patch > 0]
            mean_rate = float(vals.mean()) if len(vals) > 0 else 0.0
            return round(mean_rate / 4.0, 3)  # mm/hr → mm/15min

        return extract
    except Exception as e:
        print(f"  Radar parse error: {e}")
        return None


def load_latest_radar(s3):
    now = datetime.utcnow()
    for delta in range(3):
        date = now - timedelta(days=delta)
        prefix = f"radar/{date.strftime('%Y/%m/%d')}/"
        try:
            resp = s3.list_objects_v2(Bucket=MET_OFFICE_BUCKET, Prefix=prefix)
            keys = sorted(
                obj["Key"] for obj in resp.get("Contents", [])
                if obj["Key"].endswith("radar_rainrate_composite_1km_UK.h5")
            )
            if keys:
                key = keys[-1]
                print(f"  Radar key: {key}")
                with tempfile.NamedTemporaryFile(suffix=".h5", delete=False) as tmp:
                    tmp_path = tmp.name
                s3.download_file(MET_OFFICE_BUCKET, key, tmp_path)
                extractor = build_radar_extractor(tmp_path)
                os.unlink(tmp_path)
                return extractor
        except Exception as e:
            print(f"  Radar S3 error ({date.strftime('%Y-%m-%d')}): {e}")
    return None


def load_radar_for_time(s3, target_dt):
    """Download and build radar extractor for the exact target_dt (UTC)."""
    key = f"radar/{target_dt.strftime('%Y/%m/%d')}/{target_dt.strftime('%Y%m%d%H%M')}_ODIM_ng_radar_rainrate_composite_1km_UK.h5"
    try:
        with tempfile.NamedTemporaryFile(suffix=".h5", delete=False) as tmp:
            tmp_path = tmp.name
        s3.download_file(MET_OFFICE_BUCKET, key, tmp_path)
        extractor = build_radar_extractor(tmp_path)
        os.unlink(tmp_path)
        return extractor
    except Exception as e:
        print(f"  Specific radar S3 download failed for {target_dt.strftime('%Y-%m-%d %H:%M UTC')}: {e}")
        return None


def load_ukv_gauge_data(r2, target_dt):
    """Load UKV gauge-point values for the forecast step nearest to target_dt.

    Returns ({station_id: {accum_1h, accum_3h, ...}}, step_desc_str) or ({}, None).
    """
    meta = r2_get_json(r2, "ukv_meta.json", {})
    runs = meta.get("runs", [])
    if not runs:
        return {}, None
    run = max(runs, key=lambda r: r["run_ts"])
    run_ts = run["run_ts"]
    try:
        run_dt = datetime.strptime(run_ts, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    except Exception:
        return {}, None
    best_step, best_diff = None, float("inf")
    for step in run.get("steps", []):
        valid_dt = run_dt + timedelta(hours=step.get("offset_hours", 0))
        diff = abs((valid_dt - target_dt).total_seconds())
        if diff < best_diff:
            best_diff, best_step = diff, step
    if best_step is None or best_diff > 6 * 3600:
        return {}, None
    key = f"ukv_gauge/{run_ts}/{best_step['offset']}.json"
    data = r2_get_json(r2, key, {})
    step_desc = f"run {run_ts}, T+{best_step.get('offset_hours')}h"
    return data, step_desc


# ── UK extreme rainfall reference ─────────────────────────────────────────────
# Source: Wikipedia "United Kingdom weather records", checked 2026-05-21.
# Used in the LLM prompt as ground-truth plausibility context.
UK_EXTREME_RAINFALL = """\
UK extreme rainfall records (Wikipedia, verified 2026-05-21):
  Duration      Amount      Location                            Date
  5 min         32 mm       Preston, Lancashire                 10 Aug 1893
  30 min        80 mm       Eskdalemuir, Dumfries & Galloway   26 Jun 1953
  60 min        92 mm       Maidenhead, Berkshire               12 Jul 1901
  90 min       117 mm       Dunsop Valley, Lancashire            8 Aug 1967
  120 min      193 mm       Walshaw Dean Lodge, W. Yorkshire    19 May 1989
  24 hr        341 mm       Honister Pass, Cumbria               4 Dec 2015
These constrain 15-min plausibility: 32 mm in 5 min implies ~50 mm/15 min is
possible in the most extreme historic UK convective events.  60 mm/15 min would
be extraordinary; ≥ 60 mm warrants immediate scrutiny for sensor error.\
"""


# ── LLM assessment ─────────────────────────────────────────────────────────────
def call_llm(station_id, name, lat, lon, value, slot_time_str,
             checks, history_96, neighbours_info, radar_mm, accums,
             consecutive_flags, month_name, season,
             radar_30km_mm=None, spell_summary=None, regional_wet=None,
             check_pattern_str=None, ukv_1h_mm=None, ukv_step_info=None,
             qi=None, radar_adj=None):
    """Return (verdict, reasoning) or (None, None) if unavailable."""
    if not OPENAI_API_KEY:
        return None, None
    try:
        import openai
    except ImportError:
        print("  openai package not installed — skipping LLM")
        return None, None

    def _fmt_slots_compressed(slots):
        if not slots:
            return "[]"
        parts = []
        i = 0
        n = len(slots)
        while i < n:
            v = slots[i]
            count = 1
            if v == 0.0 or v is None:
                target = v
                while i + count < n and slots[i + count] == target:
                    count += 1
                if count >= 3:  # Compress runs of 3 or more slots
                    label = "dry" if target == 0.0 else "null"
                    parts.append(f"{count}x {label}")
                    i += count
                    continue
            if v is None:
                parts.append("-")
            else:
                s_val = f"{v:.1f}"
                if s_val.endswith(".0"):
                    s_val = s_val[:-2]
                parts.append(s_val)
            i += 1
        return "[" + ", ".join(parts) + "]"

    def _fmt_check(label, c):
        if c.get("skipped"):
            return f"  {label}: SKIP ({c['skipped']})"
        if not c.get("passed", True):
            extras = {k: v for k, v in c.items()
                      if k not in ("passed", "reason")}
            return f"  {label}: FAIL — {c.get('reason', '')} {extras}"
        return f"  {label}: PASS"

    qi_str = (f"Quality index (QI): {qi:.2f}  "
              f"({'reject' if qi < 0.25 else 'poor' if qi < 0.50 else 'uncertain' if qi < 0.75 else 'good'})\n"
              if qi is not None else "")

    check_block = "\n".join([
        _fmt_check("Gross error check (GEC)",            checks["hard_threshold"]),
        _fmt_check("Range check (RC, seasonal)",          checks.get("range_check", {"skipped": "not_in_run"})),
        _fmt_check("Temporal consistency (TCC)",          checks["temporal"]),
        _fmt_check("Spatial consistency (SCC, 100 km)",   checks.get("spatial", {"skipped": "not_in_run"})),
        _fmt_check("Persistent zero (RCC false-zero)",    checks["persistent_zero"]),
        _fmt_check("Radar conformity (RCC false-pos.)",   checks["radar"]),
        _fmt_check("Drip/leak pattern",                   checks["drip_leak"]),
        _fmt_check("Accumulation anomaly",                checks["accum_anomaly"]),
    ])

    accum_str = "  " + "  ".join(
        f"{k}: {v} mm" for k, v in accums.items()
    )

    nbr_block = ""
    for nbr in neighbours_info[:3]:
        hist = nbr.get("history_24", [])
        hist_str = _fmt_slots_compressed(hist)
        ukv_nbr = nbr.get("ukv_1h_mm")
        ukv_tag = f"  UKV={ukv_nbr:.1f}mm" if ukv_nbr is not None else ""
        nbr_block += (f"\n  {nbr['name']} ({nbr['dist_km']:.1f} km): "
                      f"latest {nbr.get('value_mm', '?')}mm  6hr={hist_str}{ukv_tag}")

    radar_5km_str = (f"{radar_mm:.2f} mm/15 min" if radar_mm is not None
                     else "unavailable")
    radar_30km_str = (f"{radar_30km_mm:.2f} mm/15 min" if radar_30km_mm is not None
                      else "unavailable")

    radar_adj_str = ""
    if radar_adj:
        b5 = radar_adj.get("before_5km")
        b1 = radar_adj.get("before_1km")
        a5 = radar_adj.get("after_5km")
        a1 = radar_adj.get("after_1km")
        
        b5_val = f"{b5:.2f} mm" if b5 is not None else "unavailable"
        b1_val = f"{b1:.2f} mm" if b1 is not None else "unavailable"
        a5_val = f"{a5:.2f} mm" if a5 is not None else "unavailable"
        a1_val = f"{a1:.2f} mm" if a1 is not None else "unavailable"
        
        radar_adj_str = (
            f"  Radar a timestep before (15 min ago):\n"
            f"    1 km site: {b1_val}  |  5 km area: {b5_val}\n"
            f"  Radar a timestep after (15 min later, if available):\n"
            f"    1 km site: {a1_val}  |  5 km area: {a5_val}\n"
        )

    flag_history = (f"This is the first time this gauge has been flagged in this episode."
                    if consecutive_flags == 1
                    else f"This gauge has been flagged for {consecutive_flags} consecutive 15-min runs "
                     f"({consecutive_flags * 15} min = {consecutive_flags * 15 // 60}h "
                     f"{consecutive_flags * 15 % 60}m).")

    spell_block = ""
    if spell_summary:
        spell_block = (
            f"\nWet/dry spell context:\n"
            f"  Max 15-min slot in last 24 hr: {spell_summary['max_15min_mm']} mm\n"
            f"  Last significant rain (≥1 mm/slot): {spell_summary['hrs_since_significant_rain']}\n"
            f"  Current spell: {spell_summary['current_spell']}\n"
        )

    regional_block = ""
    if regional_wet:
        regional_block = (
            f"\nRegional context (50 km radius, all EA stations):\n"
            f"  {regional_wet['total']} stations reporting  |  "
            f"{regional_wet['any_rain']} with any rain  |  "
            f"{regional_wet['heavy_rain']} with ≥2 mm/slot\n"
        )

    ukv_block = ""
    if ukv_1h_mm is not None:
        ukv_block = (
            f"\nUKV NWP model at station ({ukv_step_info or 'latest run'}):\n"
            f"  Forecast 1-hr accumulation: {ukv_1h_mm:.1f} mm\n"
        )

    pattern_block = (f"\nCheck failure pattern: {check_pattern_str}\n"
                     if check_pattern_str else "")

    system_prompt = (
        f"You are a UK rainfall quality-control expert advising a flood forecasting team.\n\n"
        f"Reference — {UK_EXTREME_RAINFALL}\n\n"
        f"Guidance on interpreting specific checks:\n\n"
        f"Drip/leak flag:\n"
        f"  Tipping bucket gauges record rainfall in discrete 0.1 mm tips. In genuine light\n"
        f"  rain (0.3–0.5 mm/hr) every slot naturally records exactly 0.1 mm, so runs of\n"
        f"  6–12 consecutive identical low values are a normal tipping-bucket artefact of\n"
        f"  resolution, not proof of a hardware fault. The flag is most reliable when:\n"
        f"    (a) the 24-hr history shows no coherent preceding rain event;\n"
        f"    (b) neighbours and radar are dry throughout the flagged period; and\n"
        f"    (c) the near-constant low-rate pattern persists for many hours with\n"
        f"        no realistic variation and no credible weather explanation.\n"
        f"  If a genuine rain event is visible in the 24-hr history, or neighbours/radar\n"
        f"  support recent rainfall, a drip flag on a low-value tail is likely genuine\n"
        f"  post-frontal drizzle or the dying phase of a real event — not a fault.\n"
        f"  Upland and west-facing sites (Lake District, Welsh mountains, Scottish Highlands,\n"
        f"  Dartmoor, Pennines) experience extended low-intensity drizzle far more commonly\n"
        f"  than lowland sites; apply extra benefit of the doubt at such locations.\n"
        f"  When drip/leak is the only failing check, lean toward UNCERTAIN or GENUINE\n"
        f"  unless the full evidence picture is clearly inconsistent with real rainfall.\n\n"
        f"Spatial consistency flag:\n"
        f"  A high Di score during a known narrow convective band or orographic shower\n"
        f"  is expected — isolated intense cells routinely outpace their nearest neighbours.\n"
        f"  Treat a spatial flag as weaker evidence of a fault when the 24-hr history and\n"
        f"  radar both show a coherent event, even if neighbours are relatively dry.\n\n"
        f"Radar temporal consistency:\n"
        f"  Weigh the temporal continuity of adjacent radar timesteps. A genuine rain event\n"
        f"  should typically show persistent or moving radar reflectivity in adjacent timesteps\n"
        f"  (15 mins before/after), whereas isolated spikes in time or space may indicate radar\n"
        f"  clutter, beam attenuation, or telemetry anomalies.\n\n"
        f"UKV NWP model context:\n"
        f"  Treat UKV point-data from the ~1.5 km NWP model as soft corroborating evidence only.\n"
        f"  UKV often underestimates localised convective peaks and should not override observational\n"
        f"  evidence from the gauge or radar QPE.\n\n"
        f"Instructions:\n"
        f"Based on the provided telemetry, assess whether the latest station reading is genuine "
        f"rainfall or a sensor/telemetry fault. Consider the UK climate context.\n"
        f"Give a 2–3 sentence explanation.\n"
        f"On the final line write exactly one of:\n"
        f"VERDICT: GENUINE\nVERDICT: SUSPECT\nVERDICT: UNCERTAIN"
    )

    user_prompt = (
        f"Station: {name} (ID {station_id}), "
        f"{lat:.4f}°N  {abs(lon):.4f}°{'W' if lon < 0 else 'E'}\n"
        f"Season: {season} ({month_name})\n"
        f"Latest reading: {value:.1f} mm in 15-min slot at {slot_time_str} UTC\n"
        f"{flag_history}\n\n"
        f"Accumulation totals ending at this slot:\n{accum_str}\n"
        f"{spell_block}"
        f"\nAutomated QC check results (computed externally):\n"
        f"{check_block}\n"
        f"{qi_str}"
        f"{pattern_block}"
        f"\nRadar QPE at station:\n"
        f"  5 km radius:  {radar_5km_str}\n"
        f"  30 km radius: {radar_30km_str}  (near-zero confirms spatial isolation)\n"
        f"{radar_adj_str}"
        f"{ukv_block}"
        f"{regional_block}"
        f"\nThis gauge last 24 hr (96 × 15-min slots, oldest→newest, mm, compressed):\n"
        f"  {_fmt_slots_compressed(history_96)}\n\n"
        f"Nearest neighbours (latest reading + last 6-hr slot history + UKV 1-hr forecast):\n"
        f"  {nbr_block if nbr_block else 'None available'}\n"
    )

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp   = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_completion_tokens=300,
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        verdict = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("VERDICT:"):
                v = line.split(":", 1)[1].strip().upper()
                if v in ("GENUINE", "SUSPECT", "UNCERTAIN"):
                    verdict = v
                break
        reasoning = " ".join(
            l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("VERDICT:")
        )
        return verdict, reasoning
    except Exception as e:
        print(f"  LLM error ({station_id}): {e}")
        return None, None


# ── LLM context helpers ────────────────────────────────────────────────────────
def compute_spell_summary(history_96):
    """Pre-compute wet/dry spell context from 96-slot history (oldest first)."""
    recent = [(i, v) for i, v in enumerate(reversed(history_96)) if v is not None]
    max_mm = max((v for _, v in recent), default=0.0)

    hrs_since = None
    for i, v in recent:
        if v >= 1.0:
            hrs_since = round(i * 15 / 60, 1)
            break
    hrs_since_str = f"{hrs_since} hr ago" if hrs_since is not None else ">24 hr ago"

    if not recent:
        spell_str = "unknown"
    else:
        is_wet = recent[0][1] >= 0.1
        count = 0
        for _, v in recent:
            if (v >= 0.1) == is_wet:
                count += 1
            else:
                break
        spell_str = f"{'wet' if is_wet else 'dry'} for {round(count * 15 / 60, 1)} hr"

    return {
        "max_15min_mm":              round(max_mm, 2),
        "hrs_since_significant_rain": hrs_since_str,
        "current_spell":              spell_str,
    }


def regional_wetness_count(station_id, stations, latest_readings, radius_km=50.0):
    """Count stations within radius reporting any rain / heavy rain."""
    s = stations[station_id]
    total = reporting_any = reporting_heavy = 0
    for sid, st in stations.items():
        if sid == station_id:
            continue
        if _haversine_km(s["lat"], s["lon"], st["lat"], st["lon"]) <= radius_km:
            total += 1
            v = latest_readings.get(sid) or 0
            if v > 0:
                reporting_any += 1
            if v >= 2.0:
                reporting_heavy += 1
    return {"total": total, "any_rain": reporting_any, "heavy_rain": reporting_heavy}


def check_failure_pattern(checks, prev_checks):
    """Describe how the set of failing checks has changed since the previous run."""
    _names = {"hard_threshold": "GEC", "range_check": "Range",
              "temporal": "Temporal", "spatial": "Spatial",
              "persistent_zero": "Zero", "radar": "Radar",
              "drip_leak": "Pattern", "accum_anomaly": "Accum"}

    def _failing(c):
        return {_names[k] for k, v in c.items()
                if not v.get("passed", True) and not v.get("skipped")}

    now   = _failing(checks)
    prev  = _failing(prev_checks) if prev_checks else set()
    new_f = now - prev
    cleared = prev - now
    parts = []
    if now:
        parts.append("Failing: " + ", ".join(sorted(now)))
    if new_f:
        parts.append("New this run: " + ", ".join(sorted(new_f)))
    if cleared:
        parts.append("Cleared: " + ", ".join(sorted(cleared)))
    if not now:
        parts.append("No deterministic failures (triggered by high reading value)")
    return " | ".join(parts)


# ── QI scoring ─────────────────────────────────────────────────────────────────
def compute_qi(checks):
    """Compute quality index [0.0, 1.0] from check results.

    Implements the QI reduction framework of Ośródka et al. (2022) with
    UK-specific adaptations documented in the module docstring.

      QI = 1.0          fully trusted
      QI = 0.75–0.99    minor concern (one weak signal)
      QI = 0.50–0.74    moderate concern
      QI = 0.25–0.49    significant concern — use with caution
      QI = 0.0–0.24     reject
    """
    qi = 1.0

    # GEC — hard physical impossible limit → immediate reject
    ht = checks.get("hard_threshold", {})
    if not ht.get("passed", True) and not ht.get("skipped"):
        if ht.get("reason") == "impossible":
            return 0.0
        qi -= 0.50  # "exceeds_uk_record" (≥25 mm): very suspicious

    # RC — Ośródka et al. 2022 Table 1: QI -= 0.25
    rc = checks.get("range_check", {})
    if not rc.get("passed", True) and not rc.get("skipped"):
        qi -= 0.25

    # TCC analog — Ośródka et al. 2022: QI -= 0.25 for sensor disagreement
    tc = checks.get("temporal", {})
    if not tc.get("passed", True) and not tc.get("skipped"):
        qi -= 0.25

    # RCC false-zero — paper: QI = 0; UK adaptation: QI -= 0.75 (radar
    # quality not formally assessed so we avoid a hard reject)
    pz = checks.get("persistent_zero", {})
    if not pz.get("passed", True) and not pz.get("skipped"):
        qi -= 0.75

    # RCC false-positive — Ośródka et al. 2022: QI -= 0.25 (1-sensor, lowland)
    rd = checks.get("radar", {})
    if not rd.get("passed", True) and not rd.get("skipped"):
        qi -= 0.25

    # SCC — Ośródka et al. 2022: -0.10 / -0.25 / -0.50 by outlier class
    scc = checks.get("spatial", {})
    if not scc.get("passed", True) and not scc.get("skipped"):
        cls = scc.get("outlier_class", "strong")
        qi -= {"weak": 0.10, "medium": 0.25, "strong": 0.50}.get(cls, 0.25)

    # Pattern check (no paper equivalent) — pragmatic hardware fault check
    dl = checks.get("drip_leak", {})
    if not dl.get("passed", True) and not dl.get("skipped"):
        # stuck counter severity analogous to TCC stuck (QI=0 in paper)
        qi -= 0.50 if dl.get("reason") == "stuck_counter" else 0.25

    # Accumulation anomaly (extended SCC analog; not in paper)
    aa = checks.get("accum_anomaly", {})
    if not aa.get("passed", True) and not aa.get("skipped"):
        qi -= 0.25

    return max(0.0, round(qi, 2))


# ── Utilities ──────────────────────────────────────────────────────────────────
def any_check_failed(checks):
    return any(
        not v.get("passed", True) and not v.get("skipped")
        for v in checks.values()
    )


def count_failures(checks):
    return sum(1 for v in checks.values()
               if not v.get("passed", True) and not v.get("skipped"))


def should_call_llm(checks, value):
    return count_failures(checks) >= LLM_MIN_FAILURES or value >= LLM_TRIGGER_MM


# ── Rolling event log ──────────────────────────────────────────────────────────
def update_log(r2, flagged_gauges, now):
    existing  = r2_get_json(r2, LOG_KEY, {})
    events    = {(e['station_id'], e['first_flagged_at']): e
                 for e in existing.get('events', [])}
    cutoff    = (now - timedelta(hours=LOG_RETENTION_HRS)).strftime('%Y-%m-%dT%H:%M:%SZ')

    for g in flagged_gauges:
        key = (g['station_id'], g['first_flagged_at'])
        existing_event = events.get(key, {})
        instances = existing_event.get('instances', [])

        # If existing event doesn't have an instances list yet, migrate it
        if not instances and existing_event:
            instances = [{
                'slot_time':     existing_event.get('latest_slot_time', existing_event.get('first_flagged_at')),
                'value_mm':      existing_event.get('latest_value_mm'),
                'qi':            existing_event.get('qi'),
                'checks_flag':   existing_event.get('checks_flag'),
                'checks':        existing_event.get('checks', {}),
                'llm_verdict':   existing_event.get('llm_verdict'),
                'llm_reasoning': existing_event.get('llm_reasoning'),
            }]

        new_instance = {
            'slot_time':     g['latest_slot_time'],
            'value_mm':      g['latest_value_mm'],
            'qi':            g.get('qi'),
            'checks_flag':   g['checks_flag'],
            'checks':        g['checks'],
            'llm_verdict':   g.get('llm_verdict'),
            'llm_reasoning': g.get('llm_reasoning'),
        }

        # Key by slot_time to prevent duplicate additions
        instance_map = {inst['slot_time']: inst for inst in instances}
        instance_map[new_instance['slot_time']] = new_instance
        sorted_instances = sorted(instance_map.values(), key=lambda inst: inst['slot_time'])

        events[key] = {
            'station_id':        g['station_id'],
            'name':              g['name'],
            'county':            g.get('county'),
            'lat':               g['lat'],
            'lon':               g['lon'],
            'first_flagged_at':  g['first_flagged_at'],
            'last_seen':         now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'latest_slot_time':  g['latest_slot_time'],
            'consecutive_flags': g['consecutive_flags'],
            'checks_flag':       g['checks_flag'],
            'qi':                g.get('qi'),
            'latest_value_mm':   g['latest_value_mm'],
            'checks':            g['checks'],
            'llm_verdict':       g.get('llm_verdict'),
            'llm_reasoning':     g.get('llm_reasoning'),
            'accums':            g.get('accums', {}),
            'instances':         sorted_instances,
        }

    events = {k: v for k, v in events.items() if v['last_seen'] > cutoff}
    log_data = {
        'updated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'events': sorted(events.values(), key=lambda e: e['last_seen'], reverse=True),
    }
    r2_put_json(r2, LOG_KEY, log_data)
    print(f"Updated log ({len(log_data['events'])} events in last {LOG_RETENTION_HRS}h).")


def cleanup_old_gaugecheck_runs(r2, active_runs):
    """Delete run archive JSONs from gaugecheck/runs/ that are no longer in the active manifest runs list."""
    print("Cleaning up old gauge QC runs from R2...")
    try:
        paginator = r2.get_paginator("list_objects_v2")
        existing_keys = []
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="gaugecheck/runs/"):
            for obj in page.get("Contents", []):
                existing_keys.append(obj["Key"])
        
        active_keys = {f"gaugecheck/runs/{ts}.json" for ts in active_runs}
        to_delete = [{"Key": key} for key in existing_keys if key not in active_keys]
        
        if not to_delete:
            print("  No old gauge QC runs to delete.")
            return

        deleted = 0
        for i in range(0, len(to_delete), 1000):
            batch = to_delete[i:i+1000]
            r2.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": batch})
            deleted += len(batch)
            print(f"  Deleted {len(batch)} old gauge QC run files from R2")
            
        print(f"Gauge QC runs cleanup complete. Purged {deleted} old runs.")
    except Exception as e:
        print(f"Warning: gauge QC runs cleanup failed: {e}")


def cleanup_old_radar_readings(r2, retention_days=14):
    """Delete radar readings JSONs from rain/radar_readings/ older than retention_days."""
    print("Cleaning up old radar readings from R2...")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y%m%d")
    deleted = 0
    try:
        paginator = r2.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="rain/radar_readings/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                fname = key.rsplit("/", 1)[-1].replace(".json", "")
                if len(fname) == 8 and fname.isdigit() and fname < cutoff:
                    try:
                        r2.delete_object(Bucket=R2_BUCKET, Key=key)
                        print(f"  Deleted old radar readings: {key}")
                        deleted += 1
                    except Exception as e:
                        print(f"  Warning: could not delete {key}: {e}")
        print(f"Radar readings cleanup complete. Purged {deleted} old files.")
    except Exception as e:
        print(f"Warning: radar readings cleanup failed: {e}")


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Real-time rain gauge quality control.")
    parser.add_argument("--backfill-days", type=int, default=None,
                        help="Reprocess data for the last N days instead of the last 4 slots.")
    args = parser.parse_args()

    if not USE_R2:
        print("R2 credentials not set — aborting.")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    r2  = get_r2()

    # County boundaries (used to enrich station metadata)
    print("Loading county boundaries...")
    county_features = build_county_lookup("uk-counties.geojson")
    print(f"  {len(county_features)} county polygons")

    # Station registry
    print("Loading stations...")
    sdata = r2_get_json(r2, STATIONS_KEY)
    if not sdata:
        try:
            with open("rain/stations.json") as f:
                sdata = json.load(f)
        except Exception:
            print("Could not load stations.json — aborting.")
            sys.exit(1)
    stations = sdata.get("stations", {})
    print(f"  {len(stations)} stations")

    print("  Enriching with county data...")
    for sid, s in stations.items():
        county, region = lookup_county(county_features, s["lon"], s["lat"])
        s["county"] = county
        s["region"] = region

    # Meta (latest reading time)
    meta = r2_get_json(r2, META_KEY, {})
    latest_dt, latest_date_str, latest_slot = parse_latest_time(meta)
    if latest_dt is None:
        print("No latest_time in meta — aborting.")
        sys.exit(1)
    print(f"Latest reading time: {latest_dt.isoformat()}")

    if args.backfill_days is not None:
        print(f"Backfill mode: reprocessing last {args.backfill_days} days...")
        num_slots = args.backfill_days * 96
        offsets = list(range(num_slots, -1, -1))
        load_days_count = args.backfill_days + 2
    else:
        offsets = [3, 2, 1, 0]
        load_days_count = 2

    # Load readings (covers lookback windows for offsets)
    print(f"Loading gauge readings ({load_days_count} days)...")
    days = {}
    for delta in range(load_days_count):
        d        = latest_dt - timedelta(days=delta)
        date_str = d.strftime("%Y%m%d")
        days[date_str] = load_day(r2, date_str)

    # Radar (optional)
    print("Loading radar H5...")
    extract_radar = None
    s3 = None
    try:
        s3 = boto3.client("s3", region_name="eu-west-2",
                          config=Config(signature_version=UNSIGNED))
        extract_radar = load_latest_radar(s3)
        print("  Radar " + ("OK" if extract_radar else "unavailable"))
    except Exception as e:
        print(f"  Radar setup error: {e}")

    # UKV gauge-point forecasts (optional — used to enrich LLM context)
    print("Loading UKV gauge data...")
    ukv_gauge_data, ukv_step_info = load_ukv_gauge_data(r2, latest_dt)
    print(f"  UKV: {len(ukv_gauge_data)} stations loaded"
          + (f" ({ukv_step_info})" if ukv_step_info else " (unavailable)"))

    # Initialize prev_flags for the oldest slot by loading the run preceding it
    t_prev = latest_dt - timedelta(minutes=(offsets[0] + 1) * 15)
    slot_mins = (t_prev.hour * 60 + t_prev.minute) // 15 * 15
    ts_prev = t_prev.strftime("%Y%m%d") + "T{:02d}{:02d}Z".format(slot_mins // 60, slot_mins % 60)
    prev_run = r2_get_json(r2, RUNS_KEY_TPL.format(ts=ts_prev), {})
    prev_flags = {g["station_id"]: g for g in prev_run.get("gauges", [])} if prev_run else {}

    for offset in offsets:
        target_dt = latest_dt - timedelta(minutes=offset * 15)
        target_date_str = target_dt.strftime("%Y%m%d")
        target_slot = target_dt.hour * 4 + target_dt.minute // 15
        
        target_slot_mins = (target_dt.hour * 60 + target_dt.minute) // 15 * 15
        target_run_ts = target_dt.strftime("%Y%m%d") + "T{:02d}{:02d}Z".format(target_slot_mins // 60, target_slot_mins % 60)

        print(f"\n--- Processing slot {target_run_ts} (offset {offset * 15}m ago) ---")

        # Load radar extractor for this specific target_dt
        slot_extract_radar = None
        if s3:
            print(f"  Loading specific radar for {target_run_ts}...")
            slot_extract_radar = load_radar_for_time(s3, target_dt)
            if not slot_extract_radar:
                print("    Fallback to latest radar...")
                slot_extract_radar = extract_radar

        # Load existing LLM verdicts for this slot from R2 to reuse them and avoid API calls
        existing_llm = {}
        target_run = r2_get_json(r2, RUNS_KEY_TPL.format(ts=target_run_ts), {})
        if target_run:
            for g in target_run.get("gauges", []):
                existing_llm[g["station_id"]] = (
                    g.get("llm_verdict"),
                    g.get("llm_reasoning"),
                    g.get("llm_from_run")
                )

        # Build exact slot readings for ALL registered stations at target_slot (no lookback fallback)
        latest_readings = {}
        for sid in stations:
            val = days.get(target_date_str, {}).get(sid, {}).get(str(target_slot))
            if isinstance(val, (int, float)) and val >= 0:
                latest_readings[sid] = float(val)

        # QC loop for target_slot
        flagged_gauges    = []
        station_summaries = []
        total_checked     = 0
        radar_updates_5km = {}
        radar_updates_1km = {}

        for station_id, latest_value in latest_readings.items():
            if station_id not in stations:
                continue
            total_checked += 1
            s   = stations[station_id]
            lat = s["lat"]
            lon = s["lon"]

            # Slot histories ending at target_slot
            history_7     = get_station_slots(days, station_id, target_date_str,
                                              target_slot - 1, TEMPORAL_WINDOW)
            history_pzero = get_station_slots(days, station_id, target_date_str,
                                              target_slot, PZERO_WINDOW_SLOTS)
            history_drip  = get_station_slots(days, station_id, target_date_str,
                                              target_slot, DRIP_WINDOW_SLOTS)
            history_96    = get_station_slots(days, station_id, target_date_str,
                                              target_slot, 96)

            # Accumulation totals ending at target_slot
            def _ac(n): return round(sum(v for v in history_96[-n:] if v is not None and v >= 0), 1)
            accums = {'1hr': _ac(4), '3hr': _ac(12), '6hr': _ac(24), '12hr': _ac(48), '24hr': _ac(96)}

            # Radar estimate
            radar_5km = slot_extract_radar(lat, lon, radius_km=5.0) if slot_extract_radar else None
            radar_1km = slot_extract_radar(lat, lon, radius_km=1.0) if slot_extract_radar else None
            radar_mm = radar_5km

            if radar_5km is not None:
                radar_updates_5km[station_id] = radar_5km
            if radar_1km is not None:
                radar_updates_1km[station_id] = radar_1km

            # Station summary entry
            summary_entry = {
                'id': station_id, 'n': s.get('name', station_id),
                'lat': round(lat, 5), 'lon': round(lon, 5),
                'c': s.get('county'), 'mm': round(latest_value, 2), 'ac': accums,
                'rad_5km': round(radar_5km, 2) if radar_5km is not None else None,
                'rad_1km': round(radar_1km, 2) if radar_1km is not None else None,
            }
            station_summaries.append(summary_entry)

            # Neighbours and their 6-hr means relative to target_slot
            neighbours = find_neighbours(station_id, stations, latest_readings)
            nbr_6hr_means = []
            for nbr_id, _, _, _ in neighbours:
                h = get_station_slots(days, nbr_id, target_date_str,
                                      target_slot, PZERO_WINDOW_SLOTS)
                valid_vals = [v for v in h if v is not None and v >= 0]
                nbr_6hr_means.append(
                    sum(valid_vals) / len(valid_vals) if valid_vals else None
                )

            # Eight checks
            _nbr_mean = (sum(v for v in nbr_6hr_means if v is not None)
                         / max(sum(1 for v in nbr_6hr_means if v is not None), 1)
                         if nbr_6hr_means else None)
            checks = {
                "hard_threshold":  check_hard_threshold(latest_value),
                "range_check":     check_range(latest_value, target_dt.month),
                "temporal":        check_temporal(latest_value, history_7),
                "spatial":         check_spatial_consistency(
                                       latest_value, station_id, stations, latest_readings),
                "persistent_zero": check_persistent_zero(
                                       latest_value, history_pzero,
                                       nbr_6hr_means, radar_mm),
                "radar":           check_radar(latest_value, radar_mm),
                "drip_leak":       check_drip_leak(history_drip, nbr_mean_mm=_nbr_mean),
                "accum_anomaly":   check_accumulation_anomaly(
                                       station_id, history_96, neighbours,
                                       days, target_date_str, target_slot),
            }
            qi = compute_qi(checks)

            # Only record this gauge if at least one check failed or reading is high
            if not any_check_failed(checks) and latest_value < LLM_TRIGGER_MM:
                continue

            # Consecutive-flags tracking
            prev = prev_flags.get(station_id, {})
            was_flagged_before = (prev and
                                  prev.get("checks_flag") in ("FLAGGED", "ELEVATED"))
            if was_flagged_before:
                prev_slot_time = prev.get("latest_slot_time")
                current_slot_time = target_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if prev_slot_time and current_slot_time > prev_slot_time:
                    consecutive_flags = prev.get("consecutive_flags", 0) + 1
                else:
                    consecutive_flags = prev.get("consecutive_flags", 1)
                first_flagged_at  = prev.get("first_flagged_at",
                                             target_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            else:
                consecutive_flags = 1
                first_flagged_at  = target_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            llm_verdict   = None
            llm_reasoning = None
            llm_from_run  = None

            _month  = target_dt.strftime("%B")
            _season = {12:"Winter",1:"Winter",2:"Winter",3:"Spring",4:"Spring",5:"Spring",
                       6:"Summer",7:"Summer",8:"Summer",9:"Autumn",10:"Autumn",11:"Autumn"}[target_dt.month]

            # Reuse existing LLM verdict if we are re-running a historical slot and a valid verdict exists
            if offset > 0 and station_id in existing_llm and existing_llm[station_id][0] is not None:
                llm_verdict, llm_reasoning, llm_from_run = existing_llm[station_id]
            elif should_call_llm(checks, latest_value):
                # Call LLM for every new flagged slot (offset == 0) in real-time
                neighbours_info = []
                for nbr_id, dist_km, nbr_name, nbr_val in neighbours[:3]:
                    h24 = get_station_slots(days, nbr_id, target_date_str,
                                            target_slot, 24)
                    nbr_ukv = ukv_gauge_data.get(nbr_id, {}).get("accum_1h")
                    neighbours_info.append({
                        "name":       nbr_name,
                        "dist_km":    dist_km,
                        "value_mm":   round(nbr_val, 2) if nbr_val is not None else None,
                        "history_24": h24,
                        "ukv_1h_mm":  nbr_ukv,
                    })

                radar_30km_mm   = slot_extract_radar(lat, lon, radius_km=30.0) if slot_extract_radar else None
                _spell          = compute_spell_summary(history_96)
                _regional       = regional_wetness_count(station_id, stations, latest_readings)
                _pattern        = check_failure_pattern(checks, prev.get("checks") if prev else None)
                _ukv_1h         = ukv_gauge_data.get(station_id, {}).get("accum_1h")

                # Load radar readings for adjacent slots
                radar_adj = {"before_5km": None, "before_1km": None, "after_5km": None, "after_1km": None}
                try:
                    t_b = target_dt - timedelta(minutes=15)
                    tb_date_str = t_b.strftime("%Y%m%d")
                    tb_slot = t_b.hour * 4 + t_b.minute // 15
                    tb_data = load_radar_day(r2, tb_date_str)
                    radar_adj["before_5km"] = tb_data.get("readings_5km", {}).get(station_id, {}).get(str(tb_slot))
                    radar_adj["before_1km"] = tb_data.get("readings_1km", {}).get(station_id, {}).get(str(tb_slot))
                    
                    t_a = target_dt + timedelta(minutes=15)
                    ta_date_str = t_a.strftime("%Y%m%d")
                    ta_slot = t_a.hour * 4 + t_a.minute // 15
                    ta_data = load_radar_day(r2, ta_date_str)
                    radar_adj["after_5km"] = ta_data.get("readings_5km", {}).get(station_id, {}).get(str(ta_slot))
                    radar_adj["after_1km"] = ta_data.get("readings_1km", {}).get(station_id, {}).get(str(ta_slot))
                except Exception as e:
                    print(f"  Error loading adjacent radar readings: {e}")

                llm_verdict, llm_reasoning = call_llm(
                    station_id, s.get("name", station_id), lat, lon,
                    latest_value, target_dt.strftime("%Y-%m-%dT%H:%M"),
                    checks, history_96, neighbours_info, radar_mm,
                    accums, consecutive_flags, _month, _season,
                    radar_30km_mm=radar_30km_mm, spell_summary=_spell,
                    regional_wet=_regional, check_pattern_str=_pattern,
                    ukv_1h_mm=_ukv_1h, ukv_step_info=ukv_step_info,
                    qi=qi, radar_adj=radar_adj,
                )
                if llm_verdict:
                    llm_from_run = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            checks_flag = "FLAGGED" if any_check_failed(checks) else "ELEVATED"
            summary_entry['f']  = checks_flag
            summary_entry['qi'] = qi

            flagged_gauges.append({
                "station_id":        station_id,
                "name":              s.get("name", station_id),
                "county":            s.get("county"),
                "region":            s.get("region"),
                "lat":               round(lat, 5),
                "lon":               round(lon, 5),
                "accums":            accums,
                "latest_value_mm":   round(latest_value, 2),
                "latest_slot_time":  target_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "checks_flag":       checks_flag,
                "qi":                qi,
                "checks":            checks,
                "consecutive_flags": consecutive_flags,
                "first_flagged_at":  first_flagged_at,
                "llm_verdict":       llm_verdict,
                "llm_reasoning":     llm_reasoning,
                "llm_from_run":      llm_from_run,
            })

        # Sort: newest flags first
        flagged_gauges.sort(key=lambda g: g["consecutive_flags"])

        flagged_by_checks = sum(1 for g in flagged_gauges
                                if g["checks_flag"] == "FLAGGED")
        flagged_by_llm    = sum(1 for g in flagged_gauges
                                if g.get("llm_verdict") == "SUSPECT")

        result = {
            "generated_at":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latest_reading_time": target_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_checked":      total_checked,
            "flagged_by_checks":  flagged_by_checks,
            "flagged_by_llm":     flagged_by_llm,
            "gauges":             flagged_gauges,
            "summary":            station_summaries,
        }

        print(
            f"Results for slot {target_run_ts}: {total_checked} checked  |  "
            f"{flagged_by_checks} flagged by checks  |  "
            f"{flagged_by_llm} flagged by LLM  |  "
            f"{len(flagged_gauges)} total entries"
        )

        # 1. Update timestamped run archive for this slot on R2
        print(f"Uploading run archive: {RUNS_KEY_TPL.format(ts=target_run_ts)}...")
        r2_put_json(r2, RUNS_KEY_TPL.format(ts=target_run_ts), result)

        # 2. Update the active event log for this slot (both historical and current)
        update_log(r2, flagged_gauges, target_dt)

        # ── Update R2 Radar Readings time series ─────────────────────────────
        if slot_extract_radar and (radar_updates_5km or radar_updates_1km):
            r2_key = f"rain/radar_readings/{target_date_str}.json"
            radar_data = r2_get_json(r2, r2_key, {"date": target_date_str, "readings_5km": {}, "readings_1km": {}})
            
            # Ensure keys exist
            if "readings_5km" not in radar_data: radar_data["readings_5km"] = {}
            if "readings_1km" not in radar_data: radar_data["readings_1km"] = {}
            
            for sid, val in radar_updates_5km.items():
                st_5 = radar_data["readings_5km"].setdefault(sid, {})
                st_5[str(target_slot)] = round(val, 3)
                
            for sid, val in radar_updates_1km.items():
                st_1 = radar_data["readings_1km"].setdefault(sid, {})
                st_1[str(target_slot)] = round(val, 3)
                
            r2_put_json(r2, r2_key, radar_data)
            print(f"  Uploaded radar readings time series to {r2_key}")

        # 3. If this is the latest slot (offset == 0), update results.json and manifest
        if offset == 0:
            print("Updating live results.json...")
            r2_put_json(r2, OUTPUT_KEY, result)

            manifest_data = r2_get_json(r2, MANIFEST_KEY, {"runs": []})
            runs_list = manifest_data.get("runs", [])
            if target_run_ts not in runs_list:
                runs_list.insert(0, target_run_ts)
            active_runs = runs_list[:MAX_RUNS]
            r2_put_json(r2, MANIFEST_KEY, {"runs": active_runs})
            print(f"Archived run {target_run_ts} ({min(len(runs_list), MAX_RUNS)} in manifest).")
            
            # Clean up old run JSONs and radar readings from R2
            cleanup_old_gaugecheck_runs(r2, active_runs)
            cleanup_old_radar_readings(r2)
            
            print("Done.")

        # Update prev_flags for the next target_dt in our loop
        prev_flags = {g["station_id"]: g for g in flagged_gauges}


if __name__ == "__main__":
    main()
