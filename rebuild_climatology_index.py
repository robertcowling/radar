#!/usr/bin/env python3
"""
rebuild_climatology_index.py — Regenerate the climatology index files from the
per-station climatology objects already on R2.

The per-station rain/climatology/{ref}.json objects are the authoritative
data; the index files are a derived convenience so the table page can show
record length without fetching ~1300 objects. This rebuilds them by listing
and reading the per-station objects, which makes it both a repair tool (the
EA and SEPA builders originally shared one index key and raced, dropping
entries) and a cheap way to regenerate the index after any change to what the
index carries — without re-pulling decades of upstream data.

Ops cost: LIST pages over one prefix plus one GET per station (both Class B /
free-tier-generous), then two PUTs. No upstream API calls at all.

Usage: py -3 rebuild_climatology_index.py   (needs R2 env vars)
"""

import sys

from fetch_rain import get_r2, R2_BUCKET, USE_R2
from make_rain_summary import r2_get_json, r2_put_json
from build_rain_climatology import CLIMATOLOGY_PFX, INDEX_KEY as EA_INDEX_KEY
from build_sepa_climatology import INDEX_KEY as SEPA_INDEX_KEY


def index_entry(stats):
    clim = stats.get("climatology") or {}
    return {
        "recordStart": stats.get("recordStart"),
        "recordEnd": stats.get("recordEnd"),
        "recordYears": stats.get("recordYears"),
        "qualityGoodPct": stats.get("qualityGoodPct"),
        "annualNormal": round(sum(c["mean"] for c in clim.values()), 1)
                        if len(clim) == 12 else None,
    }


def main():
    if not USE_R2:
        print("Error: R2 env vars not set — aborting")
        sys.exit(1)
    r2 = get_r2()

    keys = []
    paginator = r2.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=CLIMATOLOGY_PFX + "/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                keys.append(obj["Key"])
    print(f"{len(keys)} per-station climatology objects found")

    ea, sepa, skipped = {}, {}, 0
    for i, key in enumerate(keys, 1):
        ref = key.rsplit("/", 1)[-1][:-len(".json")]
        stats = r2_get_json(r2, key, None)
        if not stats or not stats.get("recordStart"):
            skipped += 1
            continue
        (sepa if ref.startswith("sepa_") else ea)[ref] = index_entry(stats)
        if i % 200 == 0:
            print(f"  read {i}/{len(keys)}")

    print(f"EA entries: {len(ea)} | SEPA entries: {len(sepa)} | skipped: {skipped}")
    if not ea and not sepa:
        print("Error: nothing to write — refusing to overwrite indexes with empty data")
        sys.exit(1)

    for name, data, key in (("EA", ea, EA_INDEX_KEY), ("SEPA", sepa, SEPA_INDEX_KEY)):
        if not data:
            print(f"{name}: no entries, leaving {key} untouched")
            continue
        changed = r2_put_json(r2, data, key)
        print(f"{name}: {key} {'uploaded' if changed else 'unchanged'} ({len(data)} stations)")


if __name__ == "__main__":
    main()
