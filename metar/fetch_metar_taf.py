#!/usr/bin/env python3
"""
fetch_metar_taf.py — Fetches the latest METAR and TAF for UK aerodromes from
NOAA's Aviation Weather Center Data API and writes a combined GeoJSON
snapshot for synop.html.

Source: https://aviationweather.gov/api/data/{metar,taf}?bbox=...&format=json
NOAA/NWS data is U.S. government work (17 U.S.C. §105) — public domain, no
key, no registration, no robots.txt restriction. Switched to this from
Ogimet (which has a blanket "Disallow: /" robots.txt in tension with its own
stated reuse terms) — this API is also less work to consume: it returns
already-decoded fields (temp, dewpoint, wind, cloud layers, altimeter,
present-weather string) plus station lat/lon directly, so there's no need
for the raw-bulletin regex decoder or the static airports.json/ICAO lookup
this module used to need.

Queried by an explicit `ids` list (UK_ICAO_CODES below), not a bbox — a bbox
query silently truncates (tested: a UK-covering bbox returned 45 stations
and was missing Gatwick, Luton, London City and Liverpool, all of which
have live data when queried directly by ID). The list was seeded from every
ICAO code seen across Ogimet's METAR+TAF feeds plus AWC's own bbox response,
so it should already be reasonably complete; a station that starts
reporting and isn't on this list simply won't appear here until added.

TAF is not decoded into structured fields — it's a multi-line forecast, not
a single instantaneous value, so the raw bulletin text is carried through
for display in a click-popup instead of station-plot iconography.
"""
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
LATEST_KEY = "metar/latest.geojson"
HISTORY_DIR = "metar/history"

API_BASE = "https://aviationweather.gov/api/data"
UA = {"User-Agent": "floodforecast-radar/1.0 (+https://floodforecast.co.uk; metar/taf obs page)"}

# UK civil + military aerodromes — see module docstring for why this is an
# explicit list rather than a bbox query.
UK_ICAO_CODES = sorted({
    "EGAA", "EGAC", "EGAE", "EGBB", "EGCC", "EGCK", "EGDB", "EGDC", "EGDL",
    "EGDM", "EGDR", "EGDY", "EGFF", "EGGD", "EGGP", "EGGW", "EGHC", "EGHE",
    "EGHH", "EGHI", "EGHQ", "EGJB", "EGJJ", "EGKA", "EGKB", "EGKK", "EGLC",
    "EGLF", "EGLL", "EGMC", "EGMD", "EGNH", "EGNJ", "EGNL", "EGNM", "EGNR",
    "EGNS", "EGNT", "EGNV", "EGNX", "EGOM", "EGOP", "EGOS", "EGOV", "EGPA",
    "EGPB", "EGPC", "EGPD", "EGPE", "EGPF", "EGPH", "EGPI", "EGPK", "EGPL",
    "EGPO", "EGQA", "EGQK", "EGQL", "EGQM", "EGQS", "EGSH", "EGSS", "EGSY",
    "EGTE", "EGUB", "EGUL", "EGUN", "EGUW", "EGVA", "EGVN", "EGVO", "EGVP",
    "EGWC", "EGWU", "EGXC", "EGXE", "EGXS", "EGXT", "EGXV", "EGXW", "EGXZ",
    "EGYD", "EGYE", "EGYH", "EGYM",
})

FRESHNESS_MINUTES = 90   # METAR is half-hourly at most UK stations

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])


# ── R2 helpers (same pattern as synop/fetch_synop.py) ──────────────────────────

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


def r2_put_json(r2, key, obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    r2.put_object(
        Bucket=R2_BUCKET, Key=key, Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache, max-age=0",
    )
    return len(body)


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_json(endpoint, timeout=30):
    params = {"ids": ",".join(UK_ICAO_CODES), "format": "json"}
    url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── METAR decode ────────────────────────────────────────────────────────────────

_CLOUD_RANK = {"SKC": 0, "CLR": 0, "NSC": 0, "NCD": 0, "FEW": 2, "SCT": 4, "BKN": 6, "OVC": 8, "VV": 9}


def _magnus_rh(temp_c, dewpoint_c):
    a, b = 17.625, 243.04
    if temp_c is None or dewpoint_c is None:
        return None
    e_td = math.exp((a * dewpoint_c) / (b + dewpoint_c))
    e_t = math.exp((a * temp_c) / (b + temp_c))
    return max(0.0, min(100.0, 100.0 * e_td / e_t))


def _decode_visib(visib):
    """AWC's `visib` is statute miles, capped at "6+" — UK reports almost
    always mean >=10km when they hit that cap (a bare 9999 or CAVOK group),
    so that's what's shown here rather than a literal 6-mile conversion."""
    if visib is None:
        return None
    if isinstance(visib, str):
        if visib.endswith("+"):
            return 10000
        try:
            visib = float(visib)
        except ValueError:
            return None
    return round(visib * 1609.34 / 100) * 100


def decode_metar(rec):
    out = {}

    temp, dewp = rec.get("temp"), rec.get("dewp")
    if isinstance(temp, (int, float)):
        out["temp_c"] = float(temp)
    if isinstance(dewp, (int, float)):
        out["dewpoint_c"] = float(dewp)

    wdir, wspd, wgst = rec.get("wdir"), rec.get("wspd"), rec.get("wgst")
    if isinstance(wspd, (int, float)):
        out["wind_kt"] = float(wspd)
        out["wind_dir_deg"] = wdir if isinstance(wdir, (int, float)) else None
    if isinstance(wgst, (int, float)):
        out["wind_gust_kt"] = float(wgst)

    vis = _decode_visib(rec.get("visib"))
    if vis is not None:
        out["vis_m"] = vis

    cover = rec.get("cover")
    if cover in _CLOUD_RANK:
        out["cloud_oktas"] = _CLOUD_RANK[cover]
    clouds = rec.get("clouds") or []
    worst = max(clouds, key=lambda c: _CLOUD_RANK.get(c.get("cover"), -1), default=None)
    if worst and isinstance(worst.get("base"), (int, float)):
        out["cloud_base_ft"] = worst["base"]

    wx = rec.get("wxString")
    if wx:
        out["present_wx"] = wx.split()[0]  # first group, same scope cut as before

    altim = rec.get("altim")
    if isinstance(altim, (int, float)):
        out["qnh_hpa"] = float(altim)

    if out.get("temp_c") is not None and out.get("dewpoint_c") is not None:
        rh = _magnus_rh(out["temp_c"], out["dewpoint_c"])
        if rh is not None:
            out["rh_pct"] = round(rh)

    return out


_TAF_FLAG_RE = re.compile(r"^TAF (AMD|COR) ")


def build_features(metars, tafs, now):
    metars_by_icao = {r["icaoId"]: r for r in metars}
    tafs_by_icao = {}
    for r in tafs:
        icao = r["icaoId"]
        if icao in tafs_by_icao:
            continue  # API already orders most-recent first
        tafs_by_icao[icao] = r

    features = []
    for icao in sorted(set(metars_by_icao) | set(tafs_by_icao)):
        rec = metars_by_icao.get(icao) or tafs_by_icao[icao]
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None:
            continue
        name = (rec.get("name") or icao).split(",")[0].strip()

        props = {"icao": icao, "name": name}

        m = metars_by_icao.get(icao)
        if m:
            obs_time = datetime.fromtimestamp(m["obsTime"], tz=timezone.utc) if m.get("obsTime") else None
            props["metar_raw"] = m.get("rawOb", "")
            if obs_time:
                props["metar_obs_time"] = obs_time.strftime("%Y-%m-%dT%H:%M:00Z")
                props["stale"] = (now - obs_time).total_seconds() > FRESHNESS_MINUTES * 60
            else:
                props["stale"] = True
            for k, v in decode_metar(m).items():
                props[k] = round(v, 1) if isinstance(v, float) else v
        else:
            props["stale"] = True

        t = tafs_by_icao.get(icao)
        if t:
            raw = t.get("rawTAF", "")
            props["taf_raw"] = raw
            issue = t.get("issueTime")
            if issue:
                props["taf_issued"] = issue.replace(".000Z", "Z")
            flag_m = _TAF_FLAG_RE.match(raw)
            if flag_m:
                props["taf_flag"] = flag_m.group(1)

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
            "properties": props,
        })

    return features


def main():
    now = datetime.now(timezone.utc)

    print("Fetching latest METARs (Aviation Weather Center)...")
    try:
        metars = fetch_json("metar")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  METAR fetch failed: {e}")
        metars = []
    print(f"  {len(metars)} station(s)")

    print("Fetching latest TAFs (Aviation Weather Center)...")
    try:
        tafs = fetch_json("taf")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  TAF fetch failed: {e}")
        tafs = []
    print(f"  {len(tafs)} station(s)")

    if not metars and not tafs:
        print("No data from either feed — not writing (API may be unreachable, not necessarily an error).")
        return

    features = build_features(metars, tafs, now)
    if not features:
        print("No UK stations in response — nothing to write.")
        return

    geojson = {
        "type": "FeatureCollection",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "METAR/TAF via NOAA Aviation Weather Center (https://aviationweather.gov).",
        "count": len(features),
        "features": features,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "latest.geojson")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    print(f"Wrote {out_path} ({len(features)} station(s))")

    r2 = get_r2_client()
    if r2:
        n = r2_put_json(r2, LATEST_KEY, geojson)
        print(f"Uploaded {LATEST_KEY} ({n:,} bytes)")
        hour_tag = now.strftime("%Y-%m-%dT%H")
        n = r2_put_json(r2, f"{HISTORY_DIR}/{hour_tag}.geojson", geojson)
        print(f"Uploaded {HISTORY_DIR}/{hour_tag}.geojson ({n:,} bytes)")
    else:
        print("R2 not configured — local file only.")


if __name__ == "__main__":
    main()
