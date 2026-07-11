#!/usr/bin/env python3
"""
fetch_nrw_warnings.py — Natural Resources Wales flood warnings fetcher / recorder.

Mirrors fetch_ea_warnings.py's structure and output shape so the front end can
treat EA and NRW warnings as one merged list. Run frequently (driven by an
external scheduler dispatching the GitHub workflow, same as the EA fetcher).

Source: NRW "All Active Flood Warnings and Alerts" API v3
  https://api.naturalresources.wales/floodwarnings/v3/all
Auth: Ocp-Apim-Subscription-Key header — reuses NRW_KEY, the same subscription
key already used for the NRW Telemetry API in fetch_rain.py. If flood-warnings
isn't included in that same API product/subscription in the Azure APIM
portal, this will 401/403 — subscribe to the "Open Data - Live Flood Warnings
and Alerts API - Version 3" product for that key.

NRW's SEVERITYVALUE uses the same 1-4 scale as EA's severityLevel (1=Severe
Flood Warning, 2=Flood Warning, 3=Flood Alert, 4=Warning no longer in force),
so this writes a directly EA-current.json-compatible shape.

Per-area polygons and the area index are static reference data, built
separately by build_nrw_floodareas.py (run manually — there is no live NRW
polygon API, see that script's docstring).

Outputs (R2 keys; small ones also mirrored locally for git/local dev):
  nrw/floods/current.json     — latest snapshot (active + recently stood-down lvl 4)
  nrw/timeline/events.json    — append-only log of state changes (indefinite)
  nrw/counts/series.json      — time series of in-force counts (indefinite)

Attribution (per the NRW API portal's stated terms): "Contains Natural
Resources Wales information © Natural Resources Wales and Database Right.
All rights Reserved." Licensed for reuse (including commercial) under the
Open Government Licence for Public Sector Information.
"""

import json
import os
import urllib.request
import time
from datetime import datetime, timezone, timedelta

from geo_membership import classify_category

# ── Cloudflare R2 config ──────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

# The flood-warnings API is a separate Azure APIM "product" from the
# Telemetry/Rivers-and-seas one NRW_KEY already covers (fetch_rain.py) —
# confirmed live: NRW_KEY alone gets HTTP 401 "invalid subscription key" on
# this endpoint. Subscribed separately to "Open Data - Live Flood Warnings
# and Alerts API - Version 3"; that key lives in NRW_KEY_FLOODWARNING
# (falls back to NRW_KEY in case it's ever made account-wide instead).
NRW_KEY = os.environ.get("NRW_KEY_FLOODWARNING", "") or os.environ.get("NRW_KEY", "")

API_URL     = "https://api.naturalresources.wales/floodwarnings/v3/all"
CURRENT_KEY = "nrw/floods/current.json"
EVENTS_KEY  = "nrw/timeline/events.json"
SERIES_KEY  = "nrw/counts/series.json"
INDEX_KEY   = "nrw/floodareas/index.json"

# Rolling retention for the timeline/series working logs. The permanent issuance
# record lives in flood_history/ (events_master.csv.gz), so trimming these here
# only bounds the client payload; 180 days is generous vs the 2-week scrub window.
RETENTION_DAYS = 180


def within_retention(t, cutoff):
    """True if ISO-8601 timestamp t is at or after cutoff. Keeps anything
    unparseable/missing rather than silently dropping data."""
    try:
        return datetime.fromisoformat(t) >= cutoff
    except (TypeError, ValueError):
        return True

SEV_NAME = {1: "Severe Flood Warning", 2: "Flood Warning",
            3: "Flood Alert", 4: "Warning no longer in force"}


# ── R2 helpers (identical pattern to fetch_ea_warnings.py) ────────────────────
def get_r2_client():
    if not USE_R2:
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    except ImportError:
        print("boto3 not installed. Cloudflare R2 will be bypassed.")
        return None


def r2_get_json(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return default


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")


def write_local(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


def read_local(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def http_get_json(url, headers=None, timeout=45, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def mk_event(t, code, w, from_lvl, to_lvl, kind):
    return {
        "t": t, "code": code, "label": w.get("label", ""),
        "area": w.get("area", ""), "kind": kind, "from": from_lvl, "to": to_lvl,
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not NRW_KEY:
        print("NRW_KEY not set — skipping (not an error, matches EA fetcher's soft-fail style is N/A here since this source is optional).")
        return

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    r2 = get_r2_client()
    print("R2 connected." if r2 else "R2 not configured — local only.")

    # Centroid/description lookup from the reference index (built by build_nrw_floodareas.py)
    index_obj = (r2_get_json(r2, INDEX_KEY) if r2 else None) or read_local("nrw/floodareas/index.json", {})
    ref_by_code = {a["code"]: a for a in index_obj.get("areas", [])}

    print("Fetching current NRW flood warnings/alerts…")
    data = http_get_json(API_URL, headers={"Ocp-Apim-Subscription-Key": NRW_KEY})
    items = data.get("features", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("features", [])
    print(f"  {len(items)} entries returned.")

    warnings = []
    counts = {"severe": 0, "warning": 0, "alert": 0, "stood_down": 0}
    for it in items:
        # Live API returns a GeoJSON FeatureCollection per the docs ("available
        # as a single GeoJSON data structure") — properties carry the fields,
        # geometry (if present) carries the point location.
        props = it.get("properties", it) if isinstance(it, dict) else {}
        code = props.get("FWACODE")
        if not code:
            continue
        lvl = props.get("SEVERITYVALUE")
        try:
            lvl = int(lvl)
        except (TypeError, ValueError):
            lvl = None
        ref = ref_by_code.get(code, {})

        geom = (it.get("geometry") or {}) if isinstance(it, dict) else {}
        coords = geom.get("coordinates")
        lat = coords[1] if coords else ref.get("lat")
        lon = coords[0] if coords else ref.get("long")

        label = props.get("DESCRIPTION") or ref.get("label", "")
        river = ref.get("river", "")
        # NRW's docs specify TIDAL as a string ("True"/"False"), not a real
        # boolean — coerce explicitly rather than relying on truthiness
        # (a literal string "False" is truthy in Python).
        tidal_raw = props.get("TIDAL")
        if isinstance(tidal_raw, str):
            is_tidal = tidal_raw.strip().lower() == "true"
        else:
            is_tidal = bool(tidal_raw) if tidal_raw is not None else None
        w = {
            "code": code,
            "label": label,
            "area": props.get("AREA") or ref.get("area", ""),
            "river": river,
            "counties": ref.get("counties", []),
            "rainfallZones": ref.get("rainfallZones", []),
            "severityLevel": lvl,
            "severity": props.get("SEVERITY") or SEV_NAME.get(lvl, ""),
            "isTidal": is_tidal,
            "category": classify_category(label, ref.get("description", ""), river, is_tidal=is_tidal),
            "message": props.get("RIM_ENGLISH", ""),
            "timeRaised": props.get("TIMERAISED"),
            "timeMessageChanged": props.get("RIM_CHANGED"),
            "timeSeverityChanged": props.get("SEVERITY_CHANGED"),
            "lat": lat,
            "long": lon,
            "areaType": ref.get("type", "warning"),
            "source": "NRW",
            "propertyCount": ref.get("propertyCount", 0),
        }
        warnings.append(w)
        if lvl == 1:
            counts["severe"] += 1
        elif lvl == 2:
            counts["warning"] += 1
        elif lvl == 3:
            counts["alert"] += 1
        elif lvl == 4:
            counts["stood_down"] += 1

    current_obj = {"generated_at": now_iso, "counts": counts, "warnings": warnings}

    # Diff against the previous snapshot → events
    prev = (r2_get_json(r2, CURRENT_KEY) if r2 else None) or read_local("nrw/floods/current.json", {})
    prev_by_code = {w["code"]: w for w in prev.get("warnings", [])}
    now_by_code = {w["code"]: w for w in warnings}

    new_events = []
    for code, w in now_by_code.items():
        old = prev_by_code.get(code)
        lvl = w["severityLevel"]
        if old is None:
            new_events.append(mk_event(now_iso, code, w, None, lvl, "raised"))
        else:
            old_lvl = old.get("severityLevel")
            if old_lvl != lvl:
                kind = "stood_down" if lvl == 4 else (
                    "escalated" if (lvl or 9) < (old_lvl or 9) else "downgraded")
                new_events.append(mk_event(now_iso, code, w, old_lvl, lvl, kind))
    for code, old in prev_by_code.items():
        if code not in now_by_code and old.get("severityLevel") != 4:
            new_events.append(mk_event(now_iso, code, old, old.get("severityLevel"), None, "removed"))

    retention_cutoff = now - timedelta(days=RETENTION_DAYS)
    if new_events:
        events_obj = (r2_get_json(r2, EVENTS_KEY) if r2 else None) or read_local("nrw/timeline/events.json", {"events": []})
        events_obj.setdefault("events", []).extend(new_events)
        events_obj["events"] = [e for e in events_obj["events"] if within_retention(e.get("t"), retention_cutoff)]
        events_obj["updated_at"] = now_iso
        if r2:
            r2_put_json(r2, EVENTS_KEY, events_obj)
        write_local("nrw/timeline/events.json", events_obj)
        print(f"  Appended {len(new_events)} event(s); {len(events_obj['events'])} total (≤{RETENTION_DAYS}d).")

        # Update flood history stats whenever new warnings are raised
        raised = [e for e in new_events if e.get("kind") == "raised" and (e.get("to") or 9) < 4]
        if raised and r2:
            try:
                from update_flood_stats import append_and_rebuild
                history_events = [
                    {
                        "date":   e["t"],
                        "code":   e["code"],
                        "name":   e.get("label", ""),
                        "type":   SEV_NAME.get(e["to"], "Flood Alert"),
                        "area":   e.get("county", ""),
                        "source": "NRW",
                    }
                    for e in raised
                ]
                append_and_rebuild(history_events, r2, R2_BUCKET)
            except Exception as exc:
                print(f"  [flood_stats] Update skipped: {exc}")
    else:
        print("  No state changes.")

    series_obj = (r2_get_json(r2, SERIES_KEY) if r2 else None) or read_local("nrw/counts/series.json", {"points": []})
    series_obj.setdefault("points", []).append({
        "t": now_iso, "severe": counts["severe"], "warning": counts["warning"], "alert": counts["alert"],
    })
    series_obj["points"] = [p for p in series_obj["points"] if within_retention(p.get("t"), retention_cutoff)]
    series_obj["updated_at"] = now_iso
    if r2:
        r2_put_json(r2, SERIES_KEY, series_obj)
    write_local("nrw/counts/series.json", series_obj)
    print(f"  Count point appended; {len(series_obj['points'])} total (≤{RETENTION_DAYS}d).")

    if r2:
        r2_put_json(r2, CURRENT_KEY, current_obj)
    write_local("nrw/floods/current.json", current_obj)
    print(f"  Snapshot saved: severe={counts['severe']} warning={counts['warning']} "
          f"alert={counts['alert']} stood_down={counts['stood_down']}.")
    print("NRW flood warnings update complete.")


if __name__ == "__main__":
    main()
