#!/usr/bin/env python3
"""
fetch_lightning_wis2.py — Fetches UK lightning strike data from the Met Office
ATDnet flash-location product (SFUK37), mirrored on the WMO WIS2 Global Cache
(public S3 bucket, no auth/API key required).

Writes two things to R2:
  lightning_wis2/lightning_latest.json      Rolling WINDOW_MINUTES snapshot,
                                             same {fetched_at, window_minutes,
                                             count, strikes} schema as
                                             fetch_lightning.py (the current
                                             OpenWeather-backed source) so it
                                             can be swapped in as a drop-in
                                             replacement for the live map.
  lightning_wis2/history/{YYYY-MM-DD}.json  Append-only daily archive of every
                                             distinct strike seen that UTC day
                                             (merged across runs, deduped by
                                             id). Pruned after RETENTION_DAYS.

Bucket layout (discovered by listing):
  s3://wis2globalcache/data/uk-metoffice/data/core/weather/experimental/
      upper-air-observations/lightning/

Each object is one WMO bulletin. Two payload kinds live side by side under the
same prefix:
  - BUFR-encoded ATDnet sensor heartbeats/status pings (magic b"BUFR") — not
    strikes, skipped.
  - Plain-text SFUK37 "ATDnet flash location" bulletins (magic
    b"Nominal product time") — a small header followed by 0+ CSV rows:
      date,time,microseconds,FLP ID,quality,latitude,longitude,error

Each real bulletin is stored under two different keys (a human-readable
"A_SFUK37EGRR..." key and an opaque "address:<base64>" key) — both are fetched
and deduplicated by (date, time, microseconds, FLP ID) since either naming can
be the only copy of a given message. Run cadence is expected to be every
5-15 min (triggered externally, same mechanism as rain_update.yml /
radar_update.yml), so FETCH_LOOKBACK_MINUTES only needs a small buffer over
that to avoid gaps from publish lag.
"""
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

BASE = "https://wis2globalcache.s3.us-east-1.amazonaws.com/"
PREFIX = ("data/uk-metoffice/data/core/weather/experimental/"
          "upper-air-observations/lightning/")

WINDOW_MINUTES = 90            # matches the front-end fade curve for the "latest" snapshot
FETCH_LOOKBACK_MINUTES = 30    # small buffer over the run cadence to absorb publish lag
RETENTION_DAYS = 365
MAX_WORKERS = 24
TIMEOUT_S = 15

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])

LATEST_KEY  = "lightning_wis2/lightning_latest.json"
HISTORY_DIR = "lightning_wis2/history"

ROW_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{4}),(\d{2}:\d{2}:\d{2}),(\d+),(\S+),(\S+),(-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)\s*$",
    re.MULTILINE,
)


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


def r2_get_json(r2, key):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def r2_put_json(r2, key, obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    r2.put_object(
        Bucket=R2_BUCKET, Key=key, Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache, max-age=0",
    )
    return len(body)


def list_recent_keys(cutoff):
    """Paginate ListObjectsV2 under PREFIX; return keys with LastModified >= cutoff."""
    keys = []
    token = None
    while True:
        url = BASE + "?list-type=2&max-keys=1000&prefix=" + urllib.parse.quote(PREFIX, safe="")
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token, safe="")
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            xml = r.read().decode("utf-8")
        for m in re.finditer(r"<Key>([^<]*)</Key><LastModified>([^<]*)</LastModified>", xml):
            key, lm = m.group(1), m.group(2)
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in lm else "%Y-%m-%dT%H:%M:%SZ"
            lm_dt = datetime.strptime(lm, fmt).replace(tzinfo=timezone.utc)
            if lm_dt >= cutoff:
                keys.append(key)
        if "<NextContinuationToken>" not in xml or "<IsTruncated>true</IsTruncated>" not in xml:
            break
        token = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", xml).group(1)
    return keys


def fetch_object(key):
    url = BASE + urllib.parse.quote(key, safe="/:")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            return r.read()
    except Exception as e:
        print(f"  fetch failed {key[-40:]}: {e}")
        return None


def parse_bulletin(body):
    """Parse an SFUK37 plain-text ATDnet bulletin into a list of strike dicts.
    Returns [] for BUFR heartbeats or unrecognised content."""
    if not body or not body.startswith(b"Nominal product time"):
        return []
    text = body.decode("ascii", errors="replace")
    strikes = []
    for m in ROW_RE.finditer(text):
        date_s, time_s, micros_s, flp_id, quality, lat_s, lon_s, err_s = m.groups()
        try:
            dt = datetime.strptime(f"{date_s} {time_s}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
            dt += timedelta(microseconds=int(micros_s))
        except ValueError:
            continue
        strikes.append({
            "id":       f"{dt.isoformat()}_{flp_id}",
            "datetime": dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "lat":      float(lat_s),
            "lon":      float(lon_s),
            "quality":  quality,
            "error":    float(err_s),
        })
    return strikes


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=FETCH_LOOKBACK_MINUTES)
    window_start = now - timedelta(minutes=WINDOW_MINUTES)

    print(f"Listing objects modified since {cutoff.isoformat()}...")
    keys = list_recent_keys(cutoff)
    print(f"  {len(keys)} recent object(s) to fetch")

    seen_ids = set()
    fetched_strikes = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_object, k): k for k in keys}
        for future in as_completed(futures):
            body = future.result()
            for strike in parse_bulletin(body):
                if strike["id"] not in seen_ids:
                    seen_ids.add(strike["id"])
                    fetched_strikes.append(strike)
    print(f"Parsed {len(fetched_strikes)} unique strike(s) from this run's lookback window.")

    r2 = get_r2_client()

    # ── Rolling "latest" snapshot (last WINDOW_MINUTES, for the live map) ──────
    latest_strikes = [s for s in fetched_strikes
                       if datetime.strptime(s["datetime"], "%Y-%m-%dT%H:%M:%S.%fZ")
                          .replace(tzinfo=timezone.utc) >= window_start]
    latest_strikes.sort(key=lambda s: s["datetime"], reverse=True)
    latest_payload = {
        "source":         "Met Office ATDnet (via WMO WIS2 Global Cache)",
        "fetched_at":     now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_minutes": WINDOW_MINUTES,
        "count":          len(latest_strikes),
        "strikes":        latest_strikes,
    }

    # ── Append-only daily archive(s), merged + deduped across runs ─────────────
    # A run's lookback window can span a UTC day boundary, so strikes may land
    # in up to two archive days.
    by_day = {}
    for s in fetched_strikes:
        day = s["datetime"][:10]
        by_day.setdefault(day, []).append(s)

    if r2:
        n = r2_put_json(r2, LATEST_KEY, latest_payload)
        print(f"Uploaded {LATEST_KEY} ({n:,} bytes, {len(latest_strikes)} strikes).")

        for day, day_strikes in by_day.items():
            archive_key = f"{HISTORY_DIR}/{day}.json"
            existing = r2_get_json(r2, archive_key) or {"date": day, "strikes": []}
            existing_ids = {s["id"] for s in existing["strikes"]}
            added = [s for s in day_strikes if s["id"] not in existing_ids]
            if not added:
                continue
            existing["strikes"].extend(added)
            existing["strikes"].sort(key=lambda s: s["datetime"])
            existing["count"] = len(existing["strikes"])
            existing["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            n = r2_put_json(r2, archive_key, existing)
            print(f"  {archive_key}: +{len(added)} new (total {existing['count']}, {n:,} bytes)")

        # Retention: delete the single archive day that just fell off the back
        # of the window — no bucket listing needed since files are date-named.
        expire_day = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        expire_key = f"{HISTORY_DIR}/{expire_day}.json"
        try:
            r2.delete_object(Bucket=R2_BUCKET, Key=expire_key)
            print(f"  Pruned {expire_key} (older than {RETENTION_DAYS} days).")
        except Exception as e:
            print(f"  Prune skipped for {expire_key}: {e}")
    else:
        os.makedirs("lightning_wis2/history", exist_ok=True)
        with open("lightning_wis2/lightning_latest.json", "w", encoding="utf-8") as f:
            json.dump(latest_payload, f)
        for day, day_strikes in by_day.items():
            path = f"lightning_wis2/history/{day}.json"
            existing = {"date": day, "strikes": []}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f)
            existing_ids = {s["id"] for s in existing["strikes"]}
            existing["strikes"].extend(s for s in day_strikes if s["id"] not in existing_ids)
            existing["strikes"].sort(key=lambda s: s["datetime"])
            existing["count"] = len(existing["strikes"])
            existing["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f)
        print("Saved locally (R2 not configured).")


if __name__ == "__main__":
    main()
