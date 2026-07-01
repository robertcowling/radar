#!/usr/bin/env python3
"""
fetch_obs.py — Fetches Met Office UK land surface observations from the
public AWS Open Data bucket (no credentials required) and saves the raw
per-hour, per-station CSVs locally for parse_obs.py to consume.

── Discovery findings (bucket explored via anonymous HTTPS ListObjectsV2,
   documented here as required before writing the key-construction logic
   below — do not re-derive these from assumption) ─────────────────────────

Bucket: met-office-land-observations-data (region eu-west-2), public,
listable via plain HTTPS GET to
https://met-office-land-observations-data.s3.eu-west-2.amazonaws.com/
(no aws-cli / boto3 credentials needed; anonymous GET works directly, so
this fetcher does not depend on boto3 for reading the source bucket).

(a) Key layout is hour-partitioned by DIRECTORY, not a flat per-station
    key or a single big file per hour:

        {ingest_ts}_{window_start}_{window_end}/{station_slug}_{uuid}.csv

    All three timestamps are UTC, format YYYYMMDDHHMM. Example:

        202607011859_202607011700_202607011759/
            aberporth_automatic_weather_station_a8042ad2-....csv

    ingest_ts=18:59 covering window 17:00-17:59 confirms the spec's
    ~1-2h latency (data for hour H typically lands in a directory whose
    ingest_ts is ~1h59m after the START of hour H).

(b) One CSV file per station per hour (~250-260 files per full-hour
    directory — matches "250+ stations").

(c) Approx size: ~15-40 KB per station-hour CSV (60 one-minute rows x 66
    columns), so a full hour across ~250 stations is a few MB total.

(d) IMPORTANT — alongside the clean full-hour directories (window_start
    ending :00, window_end ending :59, a 59-minute span), the bucket also
    contains many PARTIAL/incremental directories for the still-filling
    current hour, e.g.:

        202607011859_202607011758_202607011758/   (single minute, 17:58 only)
        202607011859_202607011702_202607011759/   (17:02 -> 17:59, growing)
        202607011859_202607011717_202607011759/   (17:17 -> 17:59, growing)

    These share the same ingest_ts as the eventual full-hour directory but
    cover a narrower, ragged window — they look like lower-latency,
    incomplete top-ups published while the hour is still being ingested.
    This fetcher deliberately IGNORES them and only ever syncs directories
    whose window is a clean full hour (window_end - window_start == 59
    minutes, window_start ends in "00", window_end ends in "59"). This
    trades a little freshness for predictable, complete per-station data —
    matching the spec's framing that data for hour H simply isn't ready
    yet until its full-hour directory appears, which is not an error.

Sanity check performed (see conversation / commit history): downloaded one
station CSV, confirmed `|` delimiter, lowercase columns, confirmed via
`pd.read_csv(f, delimiter="|")` that `df.columns` matches the spec's
parameter list exactly, plus a bonus `name` column (station display name).
One thing NOT as the spec assumed: the `<param>_qc` columns are not plain
"Good"/"Suspect" strings — they're JSON-ish strings, e.g. `{"good": []}`
or `{"suspect": ["2 case(s) of \"qc range check failed...\""]}`. QC parsing
in parse_obs.py accounts for this.
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

BASE = "https://met-office-land-observations-data.s3.eu-west-2.amazonaws.com/"
TIMEOUT_S = 20
MAX_WORKERS = 16
RETRY_DELAYS = (2, 4, 8)   # seconds, per spec: 3 attempts, 2/4/8s backoff

DIR_RE = re.compile(r"^(\d{12})_(\d{12})_(\d{12})/$")

DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "raw")
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.json")


def _get(url, timeout=TIMEOUT_S):
    """GET with retry/backoff (3 attempts, 2/4/8s) on failure — reused for
    both bucket listing and file downloads, per spec (EA Hydrology retry
    pattern: fixed attempt count with escalating sleep)."""
    last_exc = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "radar-surface-obs/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None   # not an error — hour just isn't published yet
            last_exc = e
        except Exception as e:
            last_exc = e
    print(f"  GET failed after retries: {url[:100]} — {last_exc}")
    return None


def _parse_ts(ts):
    return datetime.strptime(ts, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def list_hour_dirs():
    """List all top-level directory prefixes in the bucket, filtered to
    clean full-hour windows (window_start :00 -> window_end :59, 59 min
    span). Returns [(dir_key, window_start_dt, window_end_dt, ingest_dt)],
    newest window_start first."""
    dirs = []
    token = None
    while True:
        url = BASE + "?list-type=2&delimiter=/&max-keys=1000"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token, safe="")
        body = _get(url)
        if body is None:
            break
        xml = body.decode("utf-8")
        for m in re.finditer(r"<Prefix>([^<]*)</Prefix>", xml):
            prefix = m.group(1)
            dm = DIR_RE.match(prefix)
            if not dm:
                continue
            ingest_ts, start_ts, end_ts = dm.groups()
            if not (start_ts.endswith("00") and end_ts.endswith("59")):
                continue
            start_dt, end_dt = _parse_ts(start_ts), _parse_ts(end_ts)
            if (end_dt - start_dt) != timedelta(minutes=59):
                continue
            dirs.append((prefix, start_dt, end_dt, _parse_ts(ingest_ts)))
        if "<NextContinuationToken>" not in xml or "<IsTruncated>true</IsTruncated>" not in xml:
            break
        token = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", xml).group(1)
    dirs.sort(key=lambda d: d[1], reverse=True)
    return dirs


def list_station_files(dir_key):
    keys = []
    token = None
    while True:
        url = BASE + "?list-type=2&prefix=" + urllib.parse.quote(dir_key, safe="") + "&max-keys=1000"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token, safe="")
        body = _get(url)
        if body is None:
            break
        xml = body.decode("utf-8")
        keys.extend(re.findall(r"<Key>([^<]*\.csv)</Key>", xml))
        if "<NextContinuationToken>" not in xml or "<IsTruncated>true</IsTruncated>" not in xml:
            break
        token = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", xml).group(1)
    return keys


def download_station_file(key, out_dir):
    body = _get(BASE + urllib.parse.quote(key, safe="/"))
    if body is None:
        return None
    dest = os.path.join(out_dir, key.replace("/", "__"))
    with open(dest, "wb") as f:
        f.write(body)
    return dest


def load_manifest():
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"processed_dirs": []}


def save_manifest(manifest):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=3, help="Lookback window in hours (default 3, rides out ~1-2h publish latency)")
    ap.add_argument("--full", action="store_true", help="Re-pull the whole rolling 7-day window instead of just --hours")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output directory for raw per-station CSVs")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    manifest = load_manifest()
    processed = set(manifest.get("processed_dirs", []))

    print("Listing hour directories...")
    all_dirs = list_hour_dirs()
    print(f"  {len(all_dirs)} full-hour directories found in bucket")

    now = datetime.now(timezone.utc)
    if args.full:
        cutoff = now - timedelta(days=7)
    else:
        cutoff = now - timedelta(hours=args.hours)
    candidate_dirs = [d for d in all_dirs if d[1] >= cutoff]
    new_dirs = [d for d in candidate_dirs if d[0] not in processed]

    print(f"  {len(candidate_dirs)} directories within lookback window")
    print(f"  {len(new_dirs)} new directories to fetch")

    if not new_dirs:
        print("No new hourly data available yet — this is expected between Met Office ingest cycles, not an error.")
        return

    total_rows = 0
    for dir_key, start_dt, end_dt, ingest_dt in new_dirs:
        print(f"Fetching {dir_key} (hour {start_dt:%Y-%m-%d %H:%M} UTC)...")
        station_keys = list_station_files(dir_key)
        print(f"  {len(station_keys)} station file(s)")
        fetched = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(download_station_file, k, args.out): k for k in station_keys}
            for future in as_completed(futures):
                if future.result():
                    fetched += 1
        print(f"  Downloaded {fetched}/{len(station_keys)} station file(s)")
        total_rows += fetched
        processed.add(dir_key)

    manifest["processed_dirs"] = sorted(processed)[-500:]   # bound manifest growth
    save_manifest(manifest)
    print(f"Done. {len(new_dirs)} hour(s) fetched, {total_rows} station file(s) downloaded total.")


if __name__ == "__main__":
    main()
