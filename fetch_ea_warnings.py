#!/usr/bin/env python3
"""
fetch_ea_warnings.py — Environment Agency flood warnings fetcher / recorder.

Run frequently (driven by an external scheduler dispatching the GitHub workflow).
Each run:
  1. Fetches all current flood warnings/alerts  (/id/floods?min-severity=1)
  2. Writes a compact "current" snapshot enriched with centroids
  3. Diffs against the previous snapshot and appends state-change events
  4. Appends an in-force count point (severe / warning / alert) for the chart

Per-area polygons and the area index are static reference data, built separately
by fetch_ea_floodareas.py — so each run here only writes a few KB.

Severity levels (EA): 1 Severe Flood Warning, 2 Flood Warning, 3 Flood Alert,
4 Warning no longer in force.

Outputs (R2 keys; small ones also mirrored locally for git/local dev):
  ea/floods/current.json     — latest snapshot (active + recently stood-down lvl 4)
  ea/timeline/events.json    — append-only log of state changes (indefinite)
  ea/counts/series.json      — time series of in-force counts (indefinite)

Attribution: this uses Environment Agency flood and river level data from the
real-time data API (Beta).
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
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

API_ROOT    = "https://environment.data.gov.uk/flood-monitoring"
CURRENT_KEY = "ea/floods/current.json"
EVENTS_KEY  = "ea/timeline/events.json"
SERIES_KEY  = "ea/counts/series.json"
INDEX_KEY   = "ea/floodareas/index.json"

UA = {"User-Agent": "floodforecast-radar/1.0 (+https://floodforecast.co.uk)"}

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


# ── R2 helpers ────────────────────────────────────────────────────────────────
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


def http_get_json(url, timeout=45, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    r2 = get_r2_client()
    print("R2 connected." if r2 else "R2 not configured — local only.")

    # Centroid lookup from the reference index (R2, fallback local)
    index_obj = (r2_get_json(r2, INDEX_KEY) if r2 else None) or read_local("ea/floodareas/index.json", {})
    centroids = {}
    for a in index_obj.get("areas", []):
        centroids[a["code"]] = a

    # 1. Fetch all current warnings (severity 1..4)
    print("Fetching current flood warnings…")
    floods = http_get_json(f"{API_ROOT}/id/floods?min-severity=1")
    items = floods.get("items", [])
    print(f"  {len(items)} entries returned.")

    warnings = []
    counts = {"severe": 0, "warning": 0, "alert": 0, "stood_down": 0}
    for it in items:
        fa = it.get("floodArea", {}) or {}
        code = it.get("floodAreaID") or fa.get("notation")
        if not code:
            continue
        lvl = it.get("severityLevel")
        ref = centroids.get(code, {})
        label = ref.get("label") or it.get("description", "")
        description = it.get("description", "")
        river = fa.get("riverOrSea") or ref.get("river", "")
        is_tidal = it.get("isTidal")
        w = {
            "code": code,
            "label": label,
            "description": description,
            "county": fa.get("county") or ref.get("county", ""),
            "counties": ref.get("counties") or ([fa["county"]] if fa.get("county") else []),
            "rainfallZones": ref.get("rainfallZones", []),
            "river": river,
            "eaArea": it.get("eaAreaName") or ref.get("eaArea", ""),
            "severityLevel": lvl,
            "severity": it.get("severity") or SEV_NAME.get(lvl, ""),
            "isTidal": is_tidal,
            # Live isTidal is authoritative when present — takes priority over
            # the static reference's text-based heuristic (see geo_membership.py).
            "category": classify_category(label, description, river, is_tidal=is_tidal),
            "message": it.get("message", ""),
            "timeRaised": it.get("timeRaised"),
            "timeMessageChanged": it.get("timeMessageChanged"),
            "timeSeverityChanged": it.get("timeSeverityChanged"),
            "lat": ref.get("lat"),
            "long": ref.get("long"),
            "areaType": ref.get("type", "warning" if code[3:5].upper() == "FW" else "alert"),
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

    # 2. Diff against the previous snapshot → events
    prev = (r2_get_json(r2, CURRENT_KEY) if r2 else None) or read_local("ea/floods/current.json", {})
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
    # areas that vanished entirely (dropped off the feed) → ended
    for code, old in prev_by_code.items():
        if code not in now_by_code and old.get("severityLevel") != 4:
            new_events.append(mk_event(now_iso, code, old, old.get("severityLevel"), None, "removed"))

    retention_cutoff = now - timedelta(days=RETENTION_DAYS)
    if new_events:
        events_obj = (r2_get_json(r2, EVENTS_KEY) if r2 else None) or read_local("ea/timeline/events.json", {"events": []})
        events_obj.setdefault("events", []).extend(new_events)
        events_obj["events"] = [e for e in events_obj["events"] if within_retention(e.get("t"), retention_cutoff)]
        events_obj["updated_at"] = now_iso
        if r2:
            r2_put_json(r2, EVENTS_KEY, events_obj)
        write_local("ea/timeline/events.json", events_obj)
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
                        "source": "EA",
                    }
                    for e in raised
                ]
                append_and_rebuild(history_events, r2, R2_BUCKET)
            except Exception as exc:
                print(f"  [flood_stats] Update skipped: {exc}")
    else:
        print("  No state changes.")

    # 3. Append an in-force count point (rolling RETENTION_DAYS window)
    series_obj = (r2_get_json(r2, SERIES_KEY) if r2 else None) or read_local("ea/counts/series.json", {"points": []})
    series_obj.setdefault("points", []).append({
        "t": now_iso,
        "severe": counts["severe"],
        "warning": counts["warning"],
        "alert": counts["alert"],
    })
    series_obj["points"] = [p for p in series_obj["points"] if within_retention(p.get("t"), retention_cutoff)]
    series_obj["updated_at"] = now_iso
    if r2:
        r2_put_json(r2, SERIES_KEY, series_obj)
    write_local("ea/counts/series.json", series_obj)
    print(f"  Count point appended; {len(series_obj['points'])} total (≤{RETENTION_DAYS}d).")

    # 4. Save current snapshot
    if r2:
        r2_put_json(r2, CURRENT_KEY, current_obj)
    write_local("ea/floods/current.json", current_obj)
    print(f"  Snapshot saved: severe={counts['severe']} warning={counts['warning']} "
          f"alert={counts['alert']} stood_down={counts['stood_down']}.")
    print("EA flood warnings update complete.")


def mk_event(t, code, w, from_lvl, to_lvl, kind):
    return {
        "t": t,
        "code": code,
        "label": w.get("label", ""),
        "county": w.get("county", ""),
        "kind": kind,
        "from": from_lvl,
        "to": to_lvl,
    }


if __name__ == "__main__":
    main()
