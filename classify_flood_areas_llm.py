#!/usr/bin/env python3
"""
classify_flood_areas_llm.py — LLM-powered flood area category refinement.

Reads the existing EA and NRW index.json files from R2 (or local cache),
runs every area through OpenAI to classify as river / coastal / groundwater,
then patches category back into index.json, centroids.geojson, and
polygons_all.min.geojson for both sources.

Token budget (gpt-4o-mini, ~60 in + 5 out per area, 100 per batch):
  ~315 k tokens total for all 4628 areas — well inside 1 M/day limit.

Usage:
  py classify_flood_areas_llm.py [--dry-run] [--source EA|NRW]
"""

import argparse
import json
import os
import sys
import time
from openai import OpenAI

# ── R2 config ─────────────────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL      = os.environ.get("OPENAI_MODEL", "gpt-4o")
BATCH_SIZE = 100   # areas per OpenAI call

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

SYSTEM_PROMPT = """\
You are classifying UK flood warning/alert areas into one of three categories:
- "river"      — flooding from rivers, streams, or ordinary watercourses
- "coastal"    — tidal, coastal, estuarial, or sea flooding (including large tidal bays
                 like The Wash, the Humber estuary, or named sea/channel bodies)
- "groundwater"— groundwater or surface-water (pluvial) flooding

Rules:
- Use the area name as the primary signal. If the name contains "groundwater", always
  return "groundwater". If the name contains "tidal", "coastal", "coast", or "estuary"
  return "coastal".
- Well-known tidal / coastal features (The Wash, Humber, Severn Estuary, Thames Estuary,
  Morecambe Bay, Cardigan Bay, The Solent, etc.) → "coastal".
- If the river/sea field names an open sea, channel, or large estuary → "coastal".
- River names with no coastal/tidal signal → "river".
- When in doubt between river and coastal, prefer "river".
- Respond ONLY with a JSON array, one object per input area, in the same order:
  [{"code":"...", "category":"river|coastal|groundwater"}, ...]
"""


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
        try:
            return r2_get_json(r2, key)
        except Exception:
            pass
    try:
        with open(key, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def classify_batch(client, areas, model=MODEL):
    """Send one batch of area dicts to the LLM and return {code: category}."""
    lines = []
    for a in areas:
        label = a.get("label", "")
        river = a.get("river", "")
        desc  = (a.get("description", "") or "")[:120]  # cap description length
        lines.append(f'code={a["code"]} | name="{label}" | river/sea="{river}" | desc="{desc}"')
    user_msg = "\n".join(lines)

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=BATCH_SIZE * 30,
            )
            raw = resp.choices[0].message.content
            parsed = json.loads(raw)
            # Model may return {"results": [...]} or just [...]
            if isinstance(parsed, dict):
                parsed = next(iter(parsed.values()))
            return {item["code"]: item["category"] for item in parsed}
        except Exception as e:
            print(f"    Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return {}


def process_source(source, keys, client, r2, dry_run, model=MODEL):
    print(f"\n── {source} ──")
    index_obj = load_json(r2, keys["index"])
    if not index_obj:
        print(f"  index.json not found for {source}, skipping.")
        return

    areas = index_obj.get("areas", [])
    print(f"  {len(areas)} areas to classify.")

    # Batch through OpenAI
    new_cats = {}
    for i in range(0, len(areas), BATCH_SIZE):
        batch = areas[i:i + BATCH_SIZE]
        print(f"  Batch {i//BATCH_SIZE + 1}/{-(-len(areas)//BATCH_SIZE)} ({len(batch)} areas)…", end=" ", flush=True)
        result = classify_batch(client, batch, model=model)
        new_cats.update(result)
        ok = sum(1 for a in batch if a["code"] in result)
        print(f"{ok}/{len(batch)} classified.")

    # Count changes
    changed = sum(1 for a in areas if new_cats.get(a["code"]) and new_cats[a["code"]] != a.get("category"))
    print(f"  {len(new_cats)}/{len(areas)} returned; {changed} category changes.")

    if dry_run:
        # Show a sample of changes
        for a in areas:
            nc = new_cats.get(a["code"])
            if nc and nc != a.get("category"):
                print(f"    {a['code']}  {a.get('category','?')} → {nc}  [{a.get('label','')}]")
        print("  Dry run — nothing uploaded.")
        return

    # Patch index
    for a in areas:
        nc = new_cats.get(a["code"])
        if nc:
            a["category"] = nc
    if r2:
        r2_put_json(r2, keys["index"], index_obj)
        print(f"  Uploaded {keys['index']}")

    # Patch centroids
    centroids_obj = load_json(r2, keys["centroids"])
    if centroids_obj:
        for feat in centroids_obj.get("features", []):
            nc = new_cats.get(feat["properties"].get("code"))
            if nc:
                feat["properties"]["category"] = nc
        if r2:
            r2_put_json(r2, keys["centroids"], centroids_obj)
            print(f"  Uploaded {keys['centroids']}")

    # Patch combined overview
    combined_obj = load_json(r2, keys["combined"])
    if combined_obj:
        for feat in combined_obj.get("features", []):
            nc = new_cats.get(feat["properties"].get("code"))
            if nc:
                feat["properties"]["category"] = nc
        if r2:
            r2_put_json(r2, keys["combined"], combined_obj)
            body = json.dumps(combined_obj, separators=(",", ":")).encode()
            print(f"  Uploaded {keys['combined']} ({len(body)/1_048_576:.1f} MB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify but print changes without uploading to R2")
    ap.add_argument("--model", default=MODEL,
                    help=f"OpenAI model (default: {MODEL})")
    ap.add_argument("--source", choices=["EA", "NRW"], default=None,
                    help="Only process one source (default: both)")
    args = ap.parse_args()

    if not OPENAI_KEY:
        sys.exit("OPENAI_API_KEY not set.")

    r2 = get_r2()
    if r2:
        print("Connected to Cloudflare R2.")
    else:
        print("R2 not configured — will try local files.")
    if args.dry_run:
        print("Dry-run mode — no uploads.")

    client = OpenAI(api_key=OPENAI_KEY)
    model = args.model
    print(f"Using model: {model}")
    sources = [args.source] if args.source else list(SOURCES.keys())
    for src in sources:
        process_source(src, SOURCES[src], client, r2, args.dry_run, model=model)

    print("\nDone.")


if __name__ == "__main__":
    main()
