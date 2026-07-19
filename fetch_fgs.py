#!/usr/bin/env python3
"""
fetch_fgs.py — Flood Guidance Statement (FGS) fetcher and R2 archiver.

Queries the public Met Office FFC API for the 5-day Flood Guidance
Statements, parses each statement's per-county "detailed.csv" export
into a compact JSON grid (109 England & Wales counties x 5 days x
overall/rivers/surface/coastal/groundwater), and archives everything
to Cloudflare R2 so successive statements can be compared:

  fgs/index.json          — rolling index of all archived statements
  fgs/archive/{id}.json   — one parsed statement (metadata + county grid)
  fgs/archive/{id}_aoc.jpg— archived copy of the area-of-concern image

The FFC API only keeps the ~50 most recent daily statements; the index
merges new statements into whatever was archived before, so history
accumulates indefinitely. Statements are re-fetched when the API's
last_modified_at changes (amended statements). All PUTs are MD5-skipped
(see CLAUDE.md, R2 Class A budget) so the hourly cron is nearly free.

Per-source cell encoding (same as the FFC CSV): two digits, first =
impact (1 minimal .. 4 severe), second = likelihood (1 very low ..
4 high); comma-separated when a county has more than one area of
concern, e.g. "32,33". Overall risk is VL/L/M/H.
"""

import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone

# ── Cloudflare R2 Config ──────────────────────────────────────────────────────
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL    = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

INDEX_KEY   = "fgs/index.json"
ARCHIVE_PFX = "fgs/archive"

# ── FFC API Config ────────────────────────────────────────────────────────────
FFC_API_KEY = (
    os.environ.get("FFC_API_KEY", "").strip()
    or os.environ.get("FGS_API_KEY", "").strip()
    or "4195a2d9d0a07d54be9178176db1f31b4d8553cdde736c11ec373b661ec520a3"
)
BASE_URL = "https://api.ffc-environment-agency.fgs.metoffice.gov.uk/api/public/v3"

RISK_ORDER = {"VL": 0, "L": 1, "M": 2, "H": 3}


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


def r2_put_bytes(r2, key, body, content_type):
    """PUT with MD5 skip: R2's ETag for a single-part upload is the body MD5,
    so an unchanged object costs only a Class B HEAD."""
    try:
        head = r2.head_object(Bucket=R2_BUCKET, Key=key)
        if head.get("ETag", "").strip('"') == hashlib.md5(body).hexdigest():
            print(f"  {key} unchanged — skipping PUT")
            return
    except Exception:
        pass
    r2.put_object(
        Bucket=R2_BUCKET, Key=key, Body=body,
        ContentType=content_type,
        CacheControl="no-cache, max-age=0",
    )
    print(f"  Uploaded {key}")


def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    r2_put_bytes(r2, key, body, "application/json; charset=utf-8")


def write_local_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def fetch_url(url, timeout=30, api_key=False):
    import urllib.request
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if api_key:
        headers["X-Api-Key"] = FFC_API_KEY
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                print(f"  API returned status {response.status} for {url}")
                return None
            return response.read()
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return None


def parse_detailed_csv(raw):
    """Parse the FFC 'five day export' CSV into (days, counties).

    days     — list of 5 ISO dates (day 1..5)
    counties — {name: {overall: [5], river: [5], surface: [5],
                       coastal: [5], ground: [5]}}
    """
    text = raw.decode("utf-8-sig", errors="replace")
    days = [None] * 5
    counties = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8 or row[1] not in ("1", "2", "3", "4", "5"):
            continue
        name = row[0].strip()
        if not name:
            continue
        d = int(row[1]) - 1
        days[d] = row[2].strip()
        c = counties.setdefault(name, {
            "overall": [""] * 5, "river": [""] * 5, "surface": [""] * 5,
            "coastal": [""] * 5, "ground": [""] * 5,
        })
        c["overall"][d] = row[3].strip()
        c["river"][d]   = row[4].strip()
        c["surface"][d] = row[5].strip()
        c["coastal"][d] = row[6].strip()
        c["ground"][d]  = row[7].strip()
    return days, counties


def flatten_sources(sources):
    """API 'sources' is a list of single-key dicts; flatten to one dict."""
    out = {}
    for item in sources or []:
        if isinstance(item, dict):
            out.update({k: v for k, v in item.items() if isinstance(v, str)})
    return out


def summarise(counties):
    """Per-day counts: counties above very low overall risk, counties with any
    source cell above minimal/very-low ("14"), and the worst overall level."""
    above_vl = [0] * 5
    concern = [0] * 5
    worst = "VL"
    for c in counties.values():
        for d, lvl in enumerate(c["overall"]):
            if RISK_ORDER.get(lvl, 0) > 0:
                above_vl[d] += 1
            if RISK_ORDER.get(lvl, 0) > RISK_ORDER[worst]:
                worst = lvl
            if any(c[src][d] not in ("", "14") for src in
                   ("river", "surface", "coastal", "ground")):
                concern[d] += 1
    return above_vl, concern, worst


def load_existing_index(r2):
    if os.path.exists("fgs/index.json"):
        try:
            with open("fgs/index.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  Failed to read local index: {e}")
    if r2:
        try:
            res = r2.get_object(Bucket=R2_BUCKET, Key=INDEX_KEY)
            return json.loads(res["Body"].read().decode("utf-8"))
        except Exception:
            pass
    return {"statements": []}


def build_statement(st, days, counties, aoc_url):
    return {
        "id": st.get("id"),
        "issued_at": st.get("issued_at"),
        "last_modified_at": st.get("last_modified_at"),
        "next_issue_due_at": st.get("next_issue_due_at"),
        "headline": st.get("headline") or "",
        "amendments": st.get("amendments") or "",
        "future_forecast": st.get("future_forecast") or "",
        "flood_risk_trend": st.get("flood_risk_trend") or {},
        "sources": flatten_sources(st.get("sources")),
        "public_forecast": {
            k: (st.get("public_forecast") or {}).get(k, "")
            for k in ("england_forecast", "wales_forecast_english")
        },
        "pdf_url": st.get("pdf_url") or "",
        "aoc_url": aoc_url,
        "days": days,
        "counties": counties,
    }


def main():
    now = datetime.now(timezone.utc)
    r2 = get_r2_client()
    print("Connected to Cloudflare R2." if r2 else
          "Cloudflare R2 not configured. Operating in LOCAL ONLY mode.")

    raw = fetch_url(f"{BASE_URL}/statements", api_key=True)
    if not raw:
        print("Could not fetch FGS statement list; nothing to do.")
        return
    api_statements = json.loads(raw.decode("utf-8")).get("statements", [])
    print(f"API returned {len(api_statements)} statements.")

    index = load_existing_index(r2)
    by_id = {e["id"]: e for e in index.get("statements", [])}

    changed = 0
    for st in api_statements:
        sid = st.get("id")
        if sid is None:
            continue
        prev = by_id.get(sid)
        if prev and prev.get("last_modified_at") == st.get("last_modified_at"):
            continue  # already archived, not amended

        print(f"Processing statement {sid} issued {st.get('issued_at')}"
              + (" (amended)" if prev else ""))
        csv_url = st.get("detailed_csv_url")
        csv_raw = fetch_url(csv_url) if csv_url else None
        if not csv_raw:
            print(f"  No detailed CSV for statement {sid}; skipping.")
            continue
        days, counties = parse_detailed_csv(csv_raw)
        if not counties:
            print(f"  Detailed CSV for statement {sid} parsed empty; skipping.")
            continue

        # Archive the area-of-concern image (upstream assets may expire).
        aoc_url = st.get("area_of_concern_url") or ""
        if aoc_url and r2:
            aoc_bytes = fetch_url(aoc_url)
            if aoc_bytes:
                aoc_key = f"{ARCHIVE_PFX}/{sid}_aoc.jpg"
                try:
                    r2_put_bytes(r2, aoc_key, aoc_bytes, "image/jpeg")
                    aoc_url = f"{R2_PUBLIC_URL}/{aoc_key}" if R2_PUBLIC_URL else aoc_url
                except Exception as e:
                    print(f"  Error uploading AoC image: {e}")

        doc = build_statement(st, days, counties, aoc_url)
        write_local_json(f"fgs/archive/{sid}.json", doc)
        if r2:
            try:
                r2_put_json(r2, f"{ARCHIVE_PFX}/{sid}.json", doc)
            except Exception as e:
                print(f"  Error uploading statement {sid}: {e}")

        above_vl, concern, worst = summarise(counties)
        by_id[sid] = {
            "id": sid,
            "issued_at": st.get("issued_at"),
            "last_modified_at": st.get("last_modified_at"),
            "headline": doc["headline"],
            "flood_risk_trend": doc["flood_risk_trend"],
            "days": days,
            "above_vl": above_vl,
            "concern": concern,
            "worst": worst,
        }
        changed += 1

    entries = sorted(by_id.values(), key=lambda e: e.get("issued_at") or "", reverse=True)
    index_doc = {"generated_at": now.isoformat(), "statements": entries}
    write_local_json("fgs/index.json", index_doc)
    if r2:
        try:
            r2_put_json(r2, INDEX_KEY, index_doc)
        except Exception as e:
            print(f"Error uploading index: {e}")

    print(f"FGS sync complete: {changed} statement(s) archived/updated, "
          f"{len(entries)} in index.")


if __name__ == "__main__":
    main()
