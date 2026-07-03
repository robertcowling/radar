#!/usr/bin/env python3
"""
patch_flood_categories.py — Apply agreed LLM category changes + manual corrections to R2.

These are the finalised classifications from the gpt-4o dry-run (2026-07-03),
with manual overrides applied where the LLM was clearly wrong:

  - Cumbrian areas (Keswick, Cockermouth, Appleby, Windermere) wrongly coastal/groundwater → river
  - Upper Thames areas above Teddington tidal limit (Old Windsor, Newbridge, Hampton) → river
  - River Severn at Gloucester (upstream of tidal reach) → river
  - "Coastal rivers" areas in Northumberland/Lincolnshire wrongly changed to river → kept coastal
  - River Teign and its Estuary at Newton Abbot wrongly changed to river → kept coastal
  - River Darent at Dartford to the Thames estuary → kept coastal
  - River Coquet at Warkworth → kept coastal (lower tidal reach)

Usage:  py patch_flood_categories.py [--dry-run]
"""

import argparse
import json
import os
import sys

# ── R2 config ─────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

SOURCES = {
    "EA":  {
        "index":    "ea/floodareas/index.json",
        "centroids":"ea/floodareas/centroids.geojson",
        "combined": "ea/floodareas/polygons_all.min.geojson",
    },
    "NRW": {
        "index":    "nrw/floodareas/index.json",
        "centroids":"nrw/floodareas/centroids.geojson",
        "combined": "nrw/floodareas/polygons_all.min.geojson",
    },
}

# ── Finalised category assignments ────────────────────────────────────────────
# Format: code → final category.
# Derived from gpt-4o dry-run output with manual overrides noted in docstring.

EA_CHANGES = {
    # ── river → coastal (LLM agreed, geographically confirmed) ───────────────
    "121WAF925":       "coastal",  # Lower River Tees and estuarine tributaries
    "065FWC1101":      "coastal",  # Eastern Portsmouth
    "065WAC155":       "coastal",  # Hayling Island
    "054FWCDV3B2":     "coastal",  # North bank of Lake Lothing (tidal, Lowestoft)
    "121WAF911":       "coastal",  # Ouseburn and estuarine tributaries
    "064WAF362":       "coastal",  # New Romney Sewage Arm
    "065FWC0401":      "coastal",  # Ocean Village, Southampton
    "065WAC151":       "coastal",  # Southampton Water and Hamble
    "065FWC1501":      "coastal",  # North and East Hayling
    "065FWC2601":      "coastal",  # Littlehampton Rope Walk
    "111FWTPORH010":   "coastal",  # Portland Harbour at Ferry Bridge
    "064WAF332":       "coastal",  # Plenty, Swalecliffe and West Brooks (Kent coast)
    "065WAC162":       "coastal",  # Langstone to Emsworth Harbour
    "051FWCDV4B12b":   "coastal",  # Holland Marshes
    "113WACT1D":       "coastal",  # South Devon Estuaries
    "064FWT1Dartford": "coastal",  # Dartford, Crayford and Greenhithe (tidal Thames)
    "012FWTSC2B":      "coastal",  # River Lune at Lancaster Quay (tidal)
    "065FWC0801":      "coastal",  # Fareham
    "065WAC160":       "coastal",  # Fareham to Portchester
    "054FWCDV4B3b":    "coastal",  # West bank River Orwell through Ipswich (tidal)
    "065FWC1901":      "coastal",  # Bosham and West Itchenor
    "065WAC401":       "coastal",  # Thorney Island to West Itchenor
    "064FWC9A":        "coastal",  # Winchelsea, Winchelsea Beach and Pett Level
    "065FWC1201":      "coastal",  # Tipner and Stamshaw
    "054FWCDV3B5":     "coastal",  # Lowestoft Riverside Business Park (tidal lake)
    "065WAF434":       "coastal",  # Lower Adur (tidal)
    "012FWCTL14A":     "coastal",  # Lancashire coastline at Clifton Marsh
    "055FWTNENE3B":    "coastal",  # Holbeach, Fleet, Gedney (The Wash)
    "121WAF912":       "coastal",  # Rivers Derwent, Team, Don and estuarine tributaries
    "051FWCDV4C5":     "coastal",  # Salcott cum Virley (Essex estuary)
    "114FWT1T1CA00":   "coastal",  # Plymouth Barbican
    "065FWC1601":      "coastal",  # Eastoke (Hayling Island)
    "053FWTWASH1A":    "coastal",  # Wash Frontage Gibraltar Point to Freiston Shore
    "064FWT1KentMarsh":"coastal",  # North Kent Marshes
    "054FWCDV3B3":     "coastal",  # South bank of Lake Lothing
    "064FWT1Gravesend":"coastal",  # Gravesend and Northfleet (tidal Thames)
    "065FWC0901":      "coastal",  # Port Solent, Farlington and Brockhampton
    "065FWC2602":      "coastal",  # Littlehampton East Bank
    "111FWTPORH011":   "coastal",  # Portland Harbour at Osprey Quay
    "111FWTPOOH011":   "coastal",  # Poole Harbour at Hamworthy Park
    "051FWCDV4C8":     "coastal",  # Maldon Town waterfront and the Hythe
    "111FWTCHXH011":   "coastal",  # Christchurch Harbour Side
    "065FWC0802":      "coastal",  # Portchester
    "065FWC1001":      "coastal",  # Hilsea
    "053FWTWASH2A":    "coastal",  # Wash Frontage Gibraltar Point to Freiston
    "054FWTBT1F":      "coastal",  # Trinity Broads Billockby to Hemsby
    "053FWTBOS1B":     "coastal",  # Waterside properties Grand Sluice to Docks Boston
    "012FWCTL14B":     "coastal",  # Lancashire coastline Clifton Marsh / Freckleton
    "065WAC161":       "coastal",  # Port Solent to Brockhampton
    "123FWT052":       "coastal",  # River Ouse at Goole Docks (tidal)
    "051FWCDV4B12a":   "coastal",  # Walton on the Naze
    "051FWCDV4C6":     "coastal",  # Tollesbury and adjacent marshland
    "053FWTWASH1B":    "coastal",  # Wash Frontage Wyberton Marsh to Kirton Marsh
    "065FWC0301":      "coastal",  # Calshot, Hythe, Marchwood, Eling
    "013FWTTCH12":     "coastal",  # River Mersey Runcorn to Woolston (tidal)
    "065FWC1401":      "coastal",  # Langstone and Emsworth
    "062FWB53TidalLee":"coastal",  # Lower River Lee West Ham and Canning Town
    "054FWCDV3B4":     "coastal",  # Oulton Broad near Mutford Lock
    "055FWTNENE3A":    "coastal",  # Long Sutton to Newborough (The Wash)
    "111FWTPOOH012":   "coastal",  # Poole Harbour West Quay
    "065FWC1801":      "coastal",  # Thorney Island, Southbourne and Nutbourne
    "012FWCTL30B":     "coastal",  # Lancashire coastline Hesketh
    "012FWCTL26A":     "coastal",  # Lancashire coastline Banks (Old Hollow Farm)
    "012FWCTL26B":     "coastal",  # Lancashire coastline Banks (Banks Marsh)
    "052FWTKLNKL1":    "coastal",  # King's Lynn river frontage (tidal Great Ouse)
    "052FWTKLNKL2":    "coastal",  # Urban area of King's Lynn
    "065FWF3602":      "coastal",  # Barton on Sea, New Milton on the Becton Bunny
    "064FWB6DCB":      "coastal",  # Dartford (tidal Thames)
    # ── coastal → river (LLM agreed, confirmed river flooding) ───────────────
    "011FWFNC3BP":     "river",    # River Eden and Caldew at Carlisle
    # ── river → groundwater (LLM agreed) ─────────────────────────────────────
    "012FWFM20":       "groundwater",  # Fouracres and The Crescent at Maghull
    "011FWFNC3WH":     "groundwater",  # Willowholme Surface Water
    # ── MANUAL OVERRIDES — LLM was wrong ─────────────────────────────────────
    # "Coastal rivers" areas: LLM changed to river, but "coastal" is in the name
    "121WAF920":       "coastal",  # Tyne and Wear coastal rivers
    "121FWB111":       "coastal",  # River Coquet at Lower Stanners, Warkworth (tidal)
    "121WAF918":       "coastal",  # Coastal rivers in North Northumberland
    "121WAF919":       "coastal",  # Coastal rivers in South Northumberland
    "053WAF104ECT":    "coastal",  # Lincolnshire East Coast Rivers
    "113FWB2F1E":      "coastal",  # River Teign and its Estuary at Newton Abbot
    "064FWF7Dartford": "coastal",  # River Darent at Dartford to the Thames estuary
    # Upper Thames above tidal limit (Teddington Lock): LLM wrongly said coastal
    "061FWF23XOldWnd": "river",    # Thames at Old Windsor (upstream of tidal)
    "061FWF23Nwbrdg":  "river",    # Thames Newbridge to Kings Lock (Oxford)
    "061FWF23Hampton": "river",    # Thames at Hampton (just above Teddington)
    # River Severn at Gloucester: upstream of tidal reach
    "031FWBSE600":     "river",    # River Severn at Alney Island, Gloucester
    # Cumbrian freshwater areas: LLM confused by proximity labels
    "011FWFNC6KC":     "river",    # Keswick Campsite (River Greta/Derwent, inland)
    "011FWFNC34":      "river",    # Windermere Lake Levels (freshwater lake)
    # Cumbrian river areas LLM wrongly sent to groundwater
    "011FWFNC1D":      "river",    # Appleby (River Eden)
    "011FWFNC6EP":     "river",    # Elliot Park at Keswick
    "011FWFNC4F":      "river",    # Cockermouth Gote Road and St Leonards
}

NRW_CHANGES = {
    # ── river → coastal (LLM agreed, all confirmed tidal/harbour areas) ───────
    "101FWTWN320":     "coastal",  # Hawarden Embankment (River Dee estuary)
    "101FWTWN600":     "coastal",  # Y Felinheli (Menai Strait)
    "101FWTWN530":     "coastal",  # Flint (River Dee estuary)
    "101FWTWN610":     "coastal",  # Caernarfon Harbour
    "101FWTWN310":     "coastal",  # Northern Embankment
}


# ── R2 helpers ────────────────────────────────────────────────────────────────
def get_r2():
    if not USE_R2:
        return None
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def r2_get_json(r2, key):
    obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")


def load_json(r2, key):
    if r2:
        return r2_get_json(r2, key)
    with open(key, encoding="utf-8") as f:
        return json.load(f)


def patch_source(source, keys, changes, r2, dry_run):
    print(f"\n-- {source} ({len(changes)} changes) --")

    index_obj    = load_json(r2, keys["index"])
    centroids_obj= load_json(r2, keys["centroids"])
    combined_obj = load_json(r2, keys["combined"])

    # Patch index
    patched = 0
    for area in index_obj.get("areas", []):
        cat = changes.get(area["code"])
        if cat:
            area["category"] = cat
            patched += 1
    print(f"  index: {patched} area(s) updated.")

    # Patch centroids
    for feat in centroids_obj.get("features", []):
        cat = changes.get(feat["properties"].get("code"))
        if cat:
            feat["properties"]["category"] = cat

    # Patch combined overview
    for feat in combined_obj.get("features", []):
        cat = changes.get(feat["properties"].get("code"))
        if cat:
            feat["properties"]["category"] = cat

    if dry_run:
        print("  Dry run — nothing uploaded.")
        return

    r2_put_json(r2, keys["index"], index_obj)
    r2_put_json(r2, keys["centroids"], centroids_obj)
    r2_put_json(r2, keys["combined"], combined_obj)
    body = json.dumps(combined_obj, separators=(",", ":")).encode()
    print(f"  Uploaded index + centroids + combined ({len(body)/1_048_576:.1f} MB).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    r2 = get_r2()
    if not r2:
        sys.exit("R2 credentials not set — cannot proceed.")
    print("Connected to R2." + (" Dry run." if args.dry_run else ""))

    patch_source("EA",  SOURCES["EA"],  EA_CHANGES,  r2, args.dry_run)
    patch_source("NRW", SOURCES["NRW"], NRW_CHANGES, r2, args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
