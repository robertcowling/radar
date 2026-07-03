"""
update_flood_stats.py — incremental live update of flood history stats on R2.

Called by fetch_ea_warnings.py / fetch_nrw_warnings.py whenever new "raised"
events are detected.  Downloads events_master.csv.gz from R2, appends the new
events, rebuilds area_stats.json + overall_stats.json, and re-uploads all three.

Public API:
    append_and_rebuild(new_events, r2, bucket)

Each element of new_events must be a dict with:
    date   – ISO datetime string (e.g. "2026-07-03T14:05:00")
    code   – flood area code
    name   – area name / label
    type   – canonical type: "Flood Alert" / "Flood Warning" / "Severe Flood Warning"
    area   – EA area / NRW area string (may be empty string)
    source – "EA" or "NRW"
"""

import io
import json
import sys
import os

import pandas as pd

MASTER_KEY  = "flood_history/events_master.csv.gz"
STATS_KEY   = "flood_history/area_stats.json"
OVERALL_KEY = "flood_history/overall_stats.json"


def append_and_rebuild(new_events: list, r2, bucket: str) -> None:
    """Download master from R2, merge new_events, rebuild stats, re-upload."""
    if not new_events:
        return

    import build_flood_stats as bfs

    # ── 1. Download existing master ────────────────────────────────────────────
    print(f"  [flood_stats] Downloading master ({MASTER_KEY})…")
    obj = r2.get_object(Bucket=bucket, Key=MASTER_KEY)
    master_raw = pd.read_csv(io.BytesIO(obj["Body"].read()))

    # Convert back to the raw-input column format that clean() expects.
    # The master CSV has cleaned columns (date, area, code, name, type_raw, source, …)
    master_frame = master_raw.rename(columns={
        "date":  "DATE",
        "area":  "AREA",
        "code":  "CODE",
        "name":  "WARNING / ALERT AREA NAME",
        "type_raw": "TYPE",
    })[["DATE", "AREA", "CODE", "WARNING / ALERT AREA NAME", "TYPE", "source"]]

    # ── 2. Build new-events frame ──────────────────────────────────────────────
    rows = [
        {
            "DATE":  e["date"],
            "AREA":  e.get("area", ""),
            "CODE":  e["code"],
            "WARNING / ALERT AREA NAME": e["name"],
            "TYPE":  e["type"],
            "source": e["source"],
        }
        for e in new_events
    ]
    new_frame = pd.DataFrame(rows)

    # ── 3. Merge + clean (deduplication handled inside clean()) ───────────────
    print(f"  [flood_stats] Merging {len(new_events)} new event(s)…")
    df = bfs.clean([master_frame, new_frame])

    # ── 4. Rebuild stats ───────────────────────────────────────────────────────
    area_stats = bfs.build_area_stats(df)
    overall    = bfs.build_overall(df, area_stats)

    # ── 5. Upload ──────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    df.to_csv(buf, index=False, compression="gzip")
    r2.put_object(Bucket=bucket, Key=MASTER_KEY, Body=buf.getvalue(),
                  ContentType="text/csv", ContentEncoding="gzip")

    r2.put_object(Bucket=bucket, Key=STATS_KEY,
                  Body=json.dumps(area_stats, separators=(",", ":")).encode(),
                  ContentType="application/json; charset=utf-8")

    r2.put_object(Bucket=bucket, Key=OVERALL_KEY,
                  Body=json.dumps(overall, indent=1).encode(),
                  ContentType="application/json; charset=utf-8")

    print(f"  [flood_stats] Rebuilt + uploaded: {len(df):,} events, "
          f"{len(area_stats):,} areas.", flush=True)
