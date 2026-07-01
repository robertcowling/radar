#!/usr/bin/env python3
"""
parse_obs.py — Loads raw per-station CSVs downloaded by fetch_obs.py,
applies QC, and builds a "latest observation" table: one row per station
(keyed by a stable synthetic sid derived from rounded lat/lon), with the
freshest QC-passing value for each parameter.

QC format (confirmed by sampling ~60 station-hours across the bucket, see
fetch_obs.py docstring): each `<param>_qc` column holds a JSON string of
the form `{"good": []}` or `{"suspect": ["reason 1", "reason 2", ...]}`.
Erroneous values are already excluded from the source dataset entirely
(spec-confirmed), so the only classifications ever seen are "good" and
"suspect".

Dew point: confirmed empty at source (dew_point_temperature_1_minute_mean
present in the header but 100% NaN across an 8-hour sample spread over 8
days, ~7000 rows) — this is a genuine ASDI-beta publishing gap, not a QC
artefact or a one-hour fluke. Since temp + RH come off the same Stevenson
screen, dewpoint is derived here via the Magnus formula when the raw column
is absent but temp_c/rh_pct both passed QC, and tagged qc="derived" so
consumers can tell it apart from a directly-measured value.
"""
import json
import math
import os
from datetime import datetime, timezone

import pandas as pd

from params import PARAMS, IDENTITY_COLS, FRESHNESS_MINUTES_DEFAULT

RAW_DIR = os.path.join(os.path.dirname(__file__), "raw")


def parse_qc(qc_str):
    """Return ('good'|'suspect'|None, [reasons]). None means unparseable/missing."""
    if not isinstance(qc_str, str) or not qc_str.strip():
        return None, []
    try:
        obj = json.loads(qc_str)
    except (json.JSONDecodeError, TypeError):
        return None, []
    if not isinstance(obj, dict) or not obj:
        return None, []
    key = next(iter(obj))
    return key, obj.get(key) or []


def sid_for(lat, lon):
    return f"{lat:.4f}_{lon:.4f}"


def magnus_dewpoint(temp_c, rh_pct):
    """Magnus-Tetens approximation, accurate to ~0.1°C over typical UK
    surface conditions — this is how dew point is derived upstream from
    temp+RH in the first place, so this is not a loss of precision versus
    the (currently unpublished) source column."""
    if rh_pct is None or rh_pct <= 0:
        return None
    a, b = 17.625, 243.04
    gamma = math.log(rh_pct / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


def load_raw_frames(raw_dir=RAW_DIR):
    frames = []
    for fname in os.listdir(raw_dir):
        if not fname.endswith(".csv"):
            continue
        path = os.path.join(raw_dir, fname)
        try:
            df = pd.read_csv(path, delimiter="|")
        except Exception as e:
            print(f"  Skipping unreadable {fname}: {e}")
            continue
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build_latest(df, min_qc="Good", allow_suspect_params=None, freshness_minutes=FRESHNESS_MINUTES_DEFAULT, now=None):
    """Returns {sid: {"lat","lon","name","obs_time","stale","params": {key: {"value","obs_time","qc"}}}}.

    min_qc: "Good" (default) means only qc=="good" values are accepted for
    most parameters. allow_suspect_params: iterable of short param keys
    (from params.PARAMS) that should also accept "suspect" values (with the
    qc flag retained) — matches the spec's "Suspect through with a qc_flag
    retained per parameter for single-param layers where coverage matters
    more than purity."
    """
    if now is None:
        now = datetime.now(timezone.utc)
    allow_suspect_params = set(allow_suspect_params or [])
    stations = {}

    if df.empty:
        return stations

    df["timestep"] = pd.to_datetime(df["timestep"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestep", "latitude", "longitude"])

    for (lat, lon), group in df.groupby(["latitude", "longitude"]):
        sid = sid_for(lat, lon)
        group = group.sort_values("timestep")
        name = group["name"].dropna().iloc[-1] if group["name"].notna().any() else None

        station = {"lat": round(float(lat), 4), "lon": round(float(lon), 4), "name": name, "params": {}}
        newest_ts = None

        for short_key, meta in PARAMS.items():
            col = meta["col"]
            qc_col = f"{col}_qc"
            if col not in group.columns:
                continue
            allow_suspect = short_key in allow_suspect_params

            sub = group[[col, qc_col, "timestep"]].dropna(subset=[col])
            if sub.empty:
                continue

            # Walk newest-first, take the first value that passes QC policy.
            for _, row in sub.iloc[::-1].iterrows():
                qc_class, reasons = parse_qc(row[qc_col])
                if qc_class == "good" or (qc_class == "suspect" and allow_suspect):
                    ts = row["timestep"]
                    station["params"][short_key] = {
                        "value": row[col],
                        "obs_time": ts,
                        "qc": qc_class or "unknown",
                    }
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts
                    break

        # Derive dew point from temp+RH when the raw column is absent (see
        # module docstring — confirmed empty at source, not a QC artefact).
        if "dewpoint_c" not in station["params"] and "temp_c" in station["params"] and "rh_pct" in station["params"]:
            t_entry, rh_entry = station["params"]["temp_c"], station["params"]["rh_pct"]
            td = magnus_dewpoint(t_entry["value"], rh_entry["value"])
            if td is not None:
                derived_ts = min(t_entry["obs_time"], rh_entry["obs_time"])
                station["params"]["dewpoint_c"] = {"value": td, "obs_time": derived_ts, "qc": "derived"}
                if newest_ts is None or derived_ts > newest_ts:
                    newest_ts = derived_ts

        if not station["params"]:
            continue   # nothing passed QC for this station this run

        station["obs_time"] = newest_ts
        station["stale"] = bool(newest_ts is None or (now - newest_ts).total_seconds() > freshness_minutes * 60)

        if sid in stations:
            # Merge across multiple raw hours: keep the freshest value per parameter.
            existing = stations[sid]
            for k, v in station["params"].items():
                if k not in existing["params"] or v["obs_time"] > existing["params"][k]["obs_time"]:
                    existing["params"][k] = v
            if station["obs_time"] and (not existing["obs_time"] or station["obs_time"] > existing["obs_time"]):
                existing["obs_time"] = station["obs_time"]
            existing["stale"] = bool((now - existing["obs_time"]).total_seconds() > freshness_minutes * 60)
        else:
            stations[sid] = station

    return stations


if __name__ == "__main__":
    df = load_raw_frames()
    print(f"Loaded {len(df)} row(s) from {RAW_DIR}")
    stations = build_latest(df)
    print(f"Built latest snapshot for {len(stations)} station(s)")
    stale = sum(1 for s in stations.values() if s["stale"])
    print(f"  {stale} stale station(s)")
