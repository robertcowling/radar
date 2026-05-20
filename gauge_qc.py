#!/usr/bin/env python3
"""
gauge_qc.py — Real-time rain gauge quality control.

Runs 7 deterministic checks on the most recent 15-min readings, then
optionally calls an LLM (OpenAI) for an independent assessment of any
suspicious or high-value gauges.  Results are uploaded to R2 as
gaugecheck/results.json.

Checks (UK-calibrated thresholds):
  1. Hard threshold      — single slot ≥25 mm suspicious, ≥50 mm impossible
  2. Temporal spike      — isolated peak vs preceding 7-slot median
  3. Nearest neighbour   — high reading when all 20 km neighbours report near-zero
  4. Persistent zero     — gauge stuck at 0 while neighbours/radar show rain
  5. Radar conformity    — gauge far exceeds radar QPE at same location
  6. Drip/leak pattern   — near-constant low-amplitude tips over ≥3 hrs
                           (classic blocked/leaking tipping bucket signature)
  7. Accumulation anomaly — 24 hr gauge total >> neighbour mean
                            (catches persistent systematic over-recording;
                             only neighbours with ≥5 mm 24 hr total are used
                             to avoid orographic false positives)

LLM is called when ≥2 checks fail OR the reading is ≥20 mm, but only on
the first run it is flagged (consecutive_flags==1) to avoid repeated API
calls for persistently broken hardware.  Gracefully skipped if
OPENAI_API_KEY is absent or the openai package is not installed.

Future enhancement: station-specific adaptive thresholds derived from
historical climatology would replace the fixed hard thresholds above.
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
HARD_REJECT_MM       = 50.0   # physically impossible for the UK
HARD_SUSPECT_MM      = 25.0   # exceeds all known UK 15-min records

TEMPORAL_WINDOW      = 7      # preceding slots to compare against (7 × 15 min = 1 h 45)
TEMPORAL_SPIKE_RATIO = 8.0    # ratio above which an isolated spike is flagged
TEMPORAL_ZERO_TRIGGER = 5.0   # mm — flag spike from zero above this
TEMPORAL_MIN_FLAG_MM = 5.0    # don't flag spikes below this absolute value (real showers)

NN_RADIUS_KM         = 20.0   # search radius for nearest-neighbour check
NN_MIN_NBHRS         = 2      # skip check if fewer qualifying neighbours found
NN_TRIGGER_MM        = 8.0    # only apply NN check above this gauge reading
NN_MAX_NBHR_MM       = 2.0    # all neighbours must be below this to flag

PZERO_WINDOW_SLOTS   = 24     # look-back window for persistent-zero check (6 hrs)
PZERO_NBR_TRIGGER_MM = 3.0    # mean mm a neighbour must show to count as "active"
PZERO_MIN_ACTIVE     = 3      # at least N neighbours must be active

RADAR_TRIGGER_MM     = 5.0    # only apply radar check above this gauge reading
RADAR_RATIO_LIMIT    = 15.0   # gauge/radar ratio above which it is suspicious
RADAR_MAX_EST_MM     = 0.5    # radar 5 km estimate must be below this to flag

LLM_TRIGGER_MM       = 20.0   # also call LLM if reading ≥ this (even if checks pass)
LLM_MIN_FAILURES     = 1      # require this many check failures to trigger LLM (avoids calls during real rain)

DRIP_WINDOW_SLOTS    = 12     # 3 hr look-back for drip/leak pattern check
DRIP_MIN_NONZERO     = 8      # minimum non-zero slots to apply pattern check
DRIP_MAX_MEAN_MM     = 1.5    # pattern only meaningful at low intensities
DRIP_MAX_CV          = 0.25   # coefficient of variation below this = suspiciously uniform

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
LOG_RETENTION_HRS = 48   # rolling event log retention
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


def check_nearest_neighbour(value, neighbours):
    if value < NN_TRIGGER_MM:
        return {"passed": True, "skipped": "below_trigger"}
    with_data = [(sid, d, name, v) for sid, d, name, v in neighbours
                 if v is not None]
    if len(with_data) < NN_MIN_NBHRS:
        return {"passed": True, "skipped": "insufficient_neighbours",
                "n_found": len(with_data)}
    nn_vals = [v for _, _, _, v in with_data]
    nn_mean = sum(nn_vals) / len(nn_vals)
    all_low = all(v < NN_MAX_NBHR_MM for v in nn_vals)
    if all_low:
        return {
            "passed": False,
            "reason": "no_spatial_support",
            "value_mm": value,
            "nn_mean_mm": round(nn_mean, 2),
            "n_neighbours": len(with_data),
            "neighbours": [
                {"id": sid, "name": name, "dist_km": d,
                 "value_mm": round(v, 2)}
                for sid, d, name, v in with_data[:5]
            ],
        }
    return {"passed": True, "nn_mean_mm": round(nn_mean, 2),
            "n_neighbours": len(with_data)}


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


def check_drip_leak(history_drip):
    """Detect a blocked or leaking tipping bucket producing near-constant small tips.

    Classic signature: 8+ consecutive non-zero slots all in a tight range, at low
    intensity — physically implausible because real rain varies considerably in rate.
    history_drip: last DRIP_WINDOW_SLOTS values (oldest first), may contain None.
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
        return {
            "passed": False,
            "reason": "uniform_drip_pattern",
            "nonzero_slots": len(valid),
            "mean_mm": round(mean_val, 3),
            "cv": round(cv, 3),
            "longest_identical_run": longest_identical_run,
        }
    if longest_identical_run >= 6:
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
    """Return extract_5km(lat, lon) -> mm/15min, or None on failure."""
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
        r_px = max(1, round(5000 / xscale))  # 5 km in native pixels (1 km res → 5)

        def extract_5km(lat, lon):
            gx, gy = ll_to_radar.transform(lon, lat)
            col_c  = round((gx - ul_x) / xscale)
            row_c  = round((ul_y - gy) / yscale)
            if not (0 <= col_c < xsize and 0 <= row_c < ysize):
                return None
            r0, r1 = max(0, row_c - r_px), min(ysize - 1, row_c + r_px)
            c0, c1 = max(0, col_c - r_px), min(xsize - 1, col_c + r_px)
            patch = rate[r0:r1 + 1, c0:c1 + 1]
            vals  = patch[patch > 0]
            mean_rate = float(vals.mean()) if len(vals) > 0 else 0.0
            return round(mean_rate / 4.0, 3)  # mm/hr → mm/15min

        return extract_5km
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


# ── LLM assessment ─────────────────────────────────────────────────────────────
def call_llm(station_id, name, lat, lon, value, slot_time_str,
             checks, history_96, neighbours_info, radar_mm, accums,
             consecutive_flags):
    """Return (verdict, reasoning) or (None, None) if unavailable."""
    if not OPENAI_API_KEY:
        return None, None
    try:
        import openai
    except ImportError:
        print("  openai package not installed — skipping LLM")
        return None, None

    def _fmt_slots(slots):
        parts = []
        for i, v in enumerate(slots):
            parts.append(f"{v:.1f}" if v is not None else "-")
            if (i + 1) % 24 == 0 and i < len(slots) - 1:
                parts.append("\n  ")
        return "[" + " ".join(parts) + "]"

    def _fmt_check(label, c):
        if c.get("skipped"):
            return f"  {label}: SKIP ({c['skipped']})"
        if not c.get("passed", True):
            extras = {k: v for k, v in c.items()
                      if k not in ("passed", "reason")}
            return f"  {label}: FAIL — {c.get('reason', '')} {extras}"
        return f"  {label}: PASS"

    check_block = "\n".join([
        _fmt_check("Hard threshold (≥25 mm limit)", checks["hard_threshold"]),
        _fmt_check("Temporal spike",                checks["temporal"]),
        _fmt_check("Nearest neighbour (20 km)",     checks["nearest_neighbour"]),
        _fmt_check("Persistent zero",               checks["persistent_zero"]),
        _fmt_check("Radar conformity",              checks["radar"]),
        _fmt_check("Drip/leak pattern",             checks["drip_leak"]),
        _fmt_check("24 hr accumulation anomaly",    checks["accum_anomaly"]),
    ])

    accum_str = "  " + "  ".join(
        f"{k}: {v} mm" for k, v in accums.items()
    )

    nbr_block = ""
    for nbr in neighbours_info[:3]:
        hist = nbr.get("history_24", [])
        hist_str = "[" + " ".join(f"{v:.1f}" if v is not None else "-"
                                  for v in hist) + "]"
        nbr_block += (f"\n  {nbr['name']} ({nbr['dist_km']:.1f} km): "
                      f"latest {nbr.get('value_mm', '?')} mm  6hr={hist_str}")

    radar_str = (f"{radar_mm:.2f} mm/15 min" if radar_mm is not None
                 else "unavailable")

    flag_history = (f"This is the first time this gauge has been flagged in this episode."
                    if consecutive_flags == 1
                    else f"This gauge has been flagged for {consecutive_flags} consecutive 15-min runs "
                         f"({consecutive_flags * 15} min = {consecutive_flags * 15 // 60}h "
                         f"{consecutive_flags * 15 % 60}m).")

    prompt = (
        f"You are a UK rainfall quality-control expert advising a flood forecasting team.\n\n"
        f"Station: {name} (ID {station_id}), "
        f"{lat:.4f}°N  {abs(lon):.4f}°{'W' if lon < 0 else 'E'}\n"
        f"Latest reading: {value:.1f} mm in 15-min slot at {slot_time_str} UTC\n"
        f"{flag_history}\n\n"
        f"Accumulation totals ending at this slot:\n{accum_str}\n\n"
        f"Automated QC check results (computed externally — do not recalculate):\n"
        f"{check_block}\n\n"
        f"Radar QPE at station: {radar_str}\n\n"
        f"This gauge last 24 hr (96 × 15-min slots, oldest→newest, mm, '-'=missing):\n"
        f"  {_fmt_slots(history_96)}\n\n"
        f"Nearest neighbours (latest reading + last 6-hr history):"
        f"{nbr_block if nbr_block else chr(10) + '  None available'}\n\n"
        f"Based on the above, assess whether the reading at {slot_time_str} is genuine "
        f"rainfall or a sensor/telemetry fault. Consider the UK climate context.\n"
        f"Give a 2–3 sentence explanation.\n"
        f"On the final line write exactly one of:\n"
        f"VERDICT: GENUINE\nVERDICT: SUSPECT\nVERDICT: UNCERTAIN"
    )

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp   = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
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
        events[key] = {
            'station_id':       g['station_id'],
            'name':             g['name'],
            'county':           g.get('county'),
            'lat':              g['lat'],
            'lon':              g['lon'],
            'first_flagged_at': g['first_flagged_at'],
            'last_seen':        now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'consecutive_flags': g['consecutive_flags'],
            'checks_flag':      g['checks_flag'],
            'latest_value_mm':  g['latest_value_mm'],
            'checks':           g['checks'],
            'llm_verdict':      g.get('llm_verdict'),
            'llm_reasoning':    g.get('llm_reasoning'),
            'accums':           g.get('accums', {}),
        }

    events = {k: v for k, v in events.items() if v['last_seen'] > cutoff}
    log_data = {
        'updated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'events': sorted(events.values(), key=lambda e: e['last_seen'], reverse=True),
    }
    r2_put_json(r2, LOG_KEY, log_data)
    print(f"Updated log ({len(log_data['events'])} events in last {LOG_RETENTION_HRS}h).")


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
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

    # Previous results for consecutive_flags tracking
    prev_results = r2_get_json(r2, OUTPUT_KEY, {})
    prev_flags   = {g["station_id"]: g for g in prev_results.get("gauges", [])}

    # Load 2 days of readings (covers all windows: 7-slot, 24-slot, 96-slot)
    print("Loading gauge readings...")
    days = {}
    for delta in range(2):
        d        = latest_dt - timedelta(days=delta)
        date_str = d.strftime("%Y%m%d")
        days[date_str] = load_day(r2, date_str)

    # Build most-recent reading for ALL registered stations, looking back up to 3 slots (45 min).
    # Many telemetry stations have 15–45 min latency, so checking the exact latest slot
    # would silently skip the majority of the network.
    LOOKBACK_SLOTS = 3
    latest_readings = {}
    for sid in stations:
        for back in range(LOOKBACK_SLOTS + 1):
            slot_b = latest_slot - back
            date_b = latest_date_str
            if slot_b < 0:
                slot_b += 96
                date_b = (latest_dt - timedelta(days=1)).strftime("%Y%m%d")
            val = days.get(date_b, {}).get(sid, {}).get(str(slot_b))
            if isinstance(val, (int, float)) and val >= 0:
                latest_readings[sid] = float(val)
                break
    print(f"  {len(latest_readings)} stations with a reading in the last {LOOKBACK_SLOTS+1} slots")

    # Radar (optional)
    print("Loading radar H5...")
    extract_radar = None
    try:
        s3 = boto3.client("s3", region_name="eu-west-2",
                          config=Config(signature_version=UNSIGNED))
        extract_radar = load_latest_radar(s3)
        print("  Radar " + ("OK" if extract_radar else "unavailable"))
    except Exception as e:
        print(f"  Radar setup error: {e}")

    # QC loop
    print(f"Running QC on {len(latest_readings)} active stations...")
    flagged_gauges    = []
    station_summaries = []
    total_checked     = 0

    for station_id, latest_value in latest_readings.items():
        if station_id not in stations:
            continue
        total_checked += 1
        s   = stations[station_id]
        lat = s["lat"]
        lon = s["lon"]

        # Slot histories
        history_7     = get_station_slots(days, station_id, latest_date_str,
                                          latest_slot - 1, TEMPORAL_WINDOW)
        history_pzero = get_station_slots(days, station_id, latest_date_str,
                                          latest_slot, PZERO_WINDOW_SLOTS)
        history_drip  = get_station_slots(days, station_id, latest_date_str,
                                          latest_slot, DRIP_WINDOW_SLOTS)
        history_96    = get_station_slots(days, station_id, latest_date_str,
                                          latest_slot, 96)

        # Accumulation totals (always computed for every station)
        def _ac(n): return round(sum(v for v in history_96[-n:] if v is not None and v >= 0), 1)
        accums = {'1hr': _ac(4), '3hr': _ac(12), '6hr': _ac(24), '12hr': _ac(48), '24hr': _ac(96)}

        # Station summary entry (flag filled in below if flagged)
        summary_entry = {
            'id': station_id, 'n': s.get('name', station_id),
            'lat': round(lat, 5), 'lon': round(lon, 5),
            'c': s.get('county'), 'mm': round(latest_value, 2), 'ac': accums,
        }
        station_summaries.append(summary_entry)

        # Neighbours and their 6-hr means (for persistent-zero check)
        neighbours = find_neighbours(station_id, stations, latest_readings)
        nbr_6hr_means = []
        for nbr_id, _, _, _ in neighbours:
            h = get_station_slots(days, nbr_id, latest_date_str,
                                  latest_slot, PZERO_WINDOW_SLOTS)
            valid_vals = [v for v in h if v is not None and v >= 0]
            nbr_6hr_means.append(
                sum(valid_vals) / len(valid_vals) if valid_vals else None
            )

        # Radar estimate
        radar_mm = extract_radar(lat, lon) if extract_radar else None

        # Seven checks
        checks = {
            "hard_threshold":      check_hard_threshold(latest_value),
            "temporal":            check_temporal(latest_value, history_7),
            "nearest_neighbour":   check_nearest_neighbour(latest_value, neighbours),
            "persistent_zero":     check_persistent_zero(
                                       latest_value, history_pzero,
                                       nbr_6hr_means, radar_mm),
            "radar":               check_radar(latest_value, radar_mm),
            "drip_leak":           check_drip_leak(history_drip),
            "accum_anomaly":       check_accumulation_anomaly(
                                       station_id, history_96, neighbours,
                                       days, latest_date_str, latest_slot),
        }

        # Only record this gauge if at least one check failed or reading is high
        if not any_check_failed(checks) and latest_value < LLM_TRIGGER_MM:
            continue

        # Consecutive-flags tracking
        prev = prev_flags.get(station_id, {})
        was_flagged_before = (prev and
                              prev.get("checks_flag") in ("FLAGGED", "ELEVATED"))
        if was_flagged_before:
            consecutive_flags = prev.get("consecutive_flags", 0) + 1
            first_flagged_at  = prev.get("first_flagged_at",
                                         now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            consecutive_flags = 1
            first_flagged_at  = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # LLM (only on first flagged run to avoid repeated calls for stuck hardware)
        llm_verdict   = None
        llm_reasoning = None
        llm_from_run  = None

        if should_call_llm(checks, latest_value):
            prev_verdict = prev.get("llm_verdict") if prev else None
            if prev_verdict:
                # Carry forward existing assessment (avoid repeated LLM calls for stuck hardware)
                llm_verdict   = prev_verdict
                llm_reasoning = prev.get("llm_reasoning")
                llm_from_run  = prev.get("llm_from_run")
            else:
                # First flag, or prior call failed — call LLM now
                neighbours_info = []
                for nbr_id, dist_km, nbr_name, nbr_val in neighbours[:3]:
                    h24 = get_station_slots(days, nbr_id, latest_date_str,
                                            latest_slot, 24)
                    neighbours_info.append({
                        "name":       nbr_name,
                        "dist_km":    dist_km,
                        "value_mm":   round(nbr_val, 2) if nbr_val is not None else None,
                        "history_24": h24,
                    })
                llm_verdict, llm_reasoning = call_llm(
                    station_id, s.get("name", station_id), lat, lon,
                    latest_value, latest_dt.strftime("%Y-%m-%dT%H:%M"),
                    checks, history_96, neighbours_info, radar_mm,
                    accums, consecutive_flags,
                )
                if llm_verdict:
                    llm_from_run = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        checks_flag = "FLAGGED" if any_check_failed(checks) else "ELEVATED"
        summary_entry['f'] = checks_flag

        flagged_gauges.append({
            "station_id":        station_id,
            "name":              s.get("name", station_id),
            "county":            s.get("county"),
            "region":            s.get("region"),
            "lat":               round(lat, 5),
            "lon":               round(lon, 5),
            "accums":            accums,
            "latest_value_mm":   round(latest_value, 2),
            "latest_slot_time":  latest_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checks_flag":       checks_flag,
            "checks":            checks,
            "consecutive_flags": consecutive_flags,
            "first_flagged_at":  first_flagged_at,
            "llm_verdict":       llm_verdict,
            "llm_reasoning":     llm_reasoning,
            "llm_from_run":      llm_from_run,
        })

    # Sort: newest flags first, then persistent
    flagged_gauges.sort(key=lambda g: g["consecutive_flags"])

    flagged_by_checks = sum(1 for g in flagged_gauges
                            if g["checks_flag"] == "FLAGGED")
    flagged_by_llm    = sum(1 for g in flagged_gauges
                            if g.get("llm_verdict") == "SUSPECT")

    result = {
        "generated_at":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest_reading_time": latest_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_checked":      total_checked,
        "flagged_by_checks":  flagged_by_checks,
        "flagged_by_llm":     flagged_by_llm,
        "gauges":             flagged_gauges,
        "summary":            station_summaries,
    }

    print(
        f"Results: {total_checked} checked  |  "
        f"{flagged_by_checks} flagged by checks  |  "
        f"{flagged_by_llm} flagged by LLM  |  "
        f"{len(flagged_gauges)} total entries"
    )
    print("Uploading gaugecheck/results.json...")
    r2_put_json(r2, OUTPUT_KEY, result)

    # Archive timestamped snapshot and update manifest.
    # Round to 15-min floor so 10-min runs overwrite the same slot.
    slot_mins = (now.hour * 60 + now.minute) // 15 * 15
    run_ts = now.strftime("%Y%m%d") + "T{:02d}{:02d}Z".format(slot_mins // 60, slot_mins % 60)
    r2_put_json(r2, RUNS_KEY_TPL.format(ts=run_ts), result)
    manifest = r2_get_json(r2, MANIFEST_KEY, {"runs": []})
    runs_list = manifest.get("runs", [])
    if run_ts not in runs_list:
        runs_list.insert(0, run_ts)
    r2_put_json(r2, MANIFEST_KEY, {"runs": runs_list[:MAX_RUNS]})
    print(f"Archived run {run_ts} ({min(len(runs_list), MAX_RUNS)} in manifest).")
    update_log(r2, flagged_gauges, now)
    print("Done.")


if __name__ == "__main__":
    main()
