#!/usr/bin/env python3
"""
fetch_lightning.py — Fetches lightning strike data from OpenWeather Lightning API
and uploads to Cloudflare R2 for display on the UK radar map.

Queries a grid of points covering the UK+Ireland area (77 tiles at 50km radius),
deduplicates by strike UUID, and stores a rolling 90-minute snapshot to R2.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

# ── Config ────────────────────────────────────────────────────────────────────
# Note: the docs show demo.openweathermap.org — if api. doesn't work, switch to demo.
OW_API_KEY       = os.environ.get("OW_API_KEY", "").strip()
OW_BASE          = "https://api.openweathermap.org/lightning/1.0/data"
RADIUS_KM        = 50
WINDOW_MINUTES   = 90
MAX_WORKERS      = 20
TIMEOUT_S        = 15

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

LIGHTNING_KEY = "lightning/lightning_latest.json"

# Grid of (lat, lon) tile centres covering UK + Ireland.
# 1.0° lat (~111 km) × 1.5° lon (~96 km at 55°N) gives continuous coverage
# with the 50 km query radius.
GRID_LATS = [50.0 + i for i in range(11)]          # 50 → 60
GRID_LONS = [-8.0 + i * 1.5 for i in range(7)]     # -8.0 → +1.0
GRID_POINTS = [(lat, lon) for lat in GRID_LATS for lon in GRID_LONS]


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
        print("Warning: boto3 not installed — R2 upload skipped.")
        return None


def fetch_tile(lat, lon, start_iso, end_iso):
    """Fetch strikes for one grid tile; returns list of strike dicts."""
    params = urlencode({
        "lat": lat,
        "lon": lon,
        "radius": RADIUS_KM,
        "start_date": start_iso,
        "end_date": end_iso,
        "apikey": OW_API_KEY,
    })
    url = f"{OW_BASE}?{params}"
    try:
        with urlopen(url, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read()).get("lightnings", [])
    except HTTPError as e:
        if e.code == 404:
            return []   # No strikes in this tile — normal
        print(f"  HTTP {e.code} at ({lat}, {lon})")
        return []
    except (URLError, Exception) as e:
        print(f"  Error at ({lat}, {lon}): {e}")
        return []


def main():
    if not OW_API_KEY:
        print("Error: OW_API_KEY environment variable not set.")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=WINDOW_MINUTES)
    start_iso = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Fetching lightning {start_iso} → {end_iso}")
    print(f"Grid: {len(GRID_POINTS)} tiles at {RADIUS_KM} km radius")

    seen_ids = set()
    all_strikes = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_tile, lat, lon, start_iso, end_iso): (lat, lon)
            for lat, lon in GRID_POINTS
        }
        for future in as_completed(futures):
            for strike in future.result():
                sid = strike.get("id")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    all_strikes.append({
                        "id":       sid,
                        "datetime": strike["datetime"],
                        "lat":      strike["lat"],
                        "lon":      strike["lon"],
                        "quality":  strike.get("quality", ""),
                        "error":    strike.get("error", 0),
                    })

    all_strikes.sort(key=lambda s: s["datetime"], reverse=True)
    print(f"Found {len(all_strikes)} unique strikes.")

    payload = {
        "fetched_at":     end_iso,
        "window_minutes": WINDOW_MINUTES,
        "count":          len(all_strikes),
        "strikes":        all_strikes,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    r2 = get_r2_client()
    if r2:
        r2.put_object(
            Bucket=R2_BUCKET,
            Key=LIGHTNING_KEY,
            Body=body,
            ContentType="application/json; charset=utf-8",
            CacheControl="no-cache, max-age=0",
        )
        print(f"Uploaded {LIGHTNING_KEY} to R2 ({len(body):,} bytes).")
    else:
        os.makedirs("lightning", exist_ok=True)
        with open("lightning/lightning_latest.json", "wb") as f:
            f.write(body)
        print("Saved locally (R2 not configured).")


if __name__ == "__main__":
    main()
