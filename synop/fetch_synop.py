#!/usr/bin/env python3
"""
fetch_synop.py — Fetches raw WMO SYNOP (FM-12 AAXX) bulletins for UK stations
from Ogimet's public archive, decodes the latest report per station, and
writes a GeoJSON snapshot for synop.html.

Source: https://www.ogimet.com/display_synopsc2.php (free, public, no key —
Ogimet asks that bulk/automated use stay reasonable; this fetcher makes one
request per run, intended to be run hourly).

Coverage: ~135-140 UK WMO 03xxx stations (a mix of staffed synoptic sites,
airfields, and automatic weather stations) — a different, smaller, but more
"classically synoptic" network than surface_obs/'s Met Office AWS one-minute
feed (present weather, cloud oktas, pressure tendency; no soil/grass temps).

Output schema deliberately mirrors surface_obs/'s latest_obs.geojson property
names (temp_c, dewpoint_c, mslp_hpa, wind_dir_deg, wind_kt, cloud_oktas,
vis_m, rh_pct, obs_time, stale, sid, name) so synop.html can reuse the same
WMO station-plot rendering approach. present_wx here is decoded from the
SYNOP ww code (not a METAR string), with present_wx_symbol carrying the
render glyph id directly — see ww_table.py.

SYNOP group decode reference (WMO Manual on Codes, FM-12-XIV AAXX):
  YYGGiw          day/hour/wind-unit indicator
  IIiii           station ID (already known from the block header)
  iRixhVV         iR ix h VV — precipitation/type indicators + visibility
  Nddff           total cloud oktas, wind dir (tens of deg), wind speed
  1SnTTT          air temperature
  2SnTdTdTd       dew point
  3PPPP           station-level pressure (tenths hPa, mod 1000)
  4PPPP           mean sea level pressure (tenths hPa, mod 1000)
  5appp           pressure tendency (characteristic + 3h change, tenths hPa)
  7wwW1W2         present / past weather
  8NhClCmCh       cloud layer detail (not used here — total N above suffices)
Groups 6 (precip) and 9 (exact time/regional) are only decoded when they
appear in the main section (rare); most UK stations carry them in the
regional "333" section instead, which this fetcher does not parse — a
scope cut, not a bug: precipitation isn't shown on this page.
"""
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from ww_table import decode_ww, TENDENCY_DESC

OGIMET_BASE = "https://www.ogimet.com/display_synopsc2.php"
UA = {"User-Agent": "floodforecast-radar/1.0 (+https://floodforecast.co.uk; synop obs page)"}

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
LATEST_KEY = "synop/latest.geojson"
HISTORY_DIR = "synop/history"

LOOKBACK_HOURS = 4          # window queried — rides out any single missed hourly cycle
FRESHNESS_MINUTES = 150     # stations older than this are flagged stale (matches surface_obs)

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY, R2_BUCKET])


# ── R2 helpers (same pattern as surface_obs/build_snapshot.py) ────────────────

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

def fetch_ogimet_text(start_dt, end_dt, timeout=45):
    params = {
        "lang": "en", "estado": "United K", "tipo": "ALL", "ord": "REV",
        "nil": "SI", "fmt": "txt",
        "ano": start_dt.year, "mes": f"{start_dt.month:02d}", "day": f"{start_dt.day:02d}", "hora": f"{start_dt.hour:02d}",
        "anof": end_dt.year, "mesf": f"{end_dt.month:02d}", "dayf": f"{end_dt.day:02d}", "horaf": f"{end_dt.hour:02d}",
        "send": "send",
    }
    url = OGIMET_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    m = re.search(r"<pre>([\s\S]*?)</pre>", html)
    return m.group(1) if m else html


# ── Station block parsing ─────────────────────────────────────────────────────

_STATION_BLOCK_RE = re.compile(
    r"^# (\d{5}), (.+?) \(United Kingdom\)\n"
    r"# WIGOS Id: [^\n]*\n"
    r"# ICAO index: [^\n]*\n"
    r"# Latitude (\d+)-(\d+)-(\d+)([NS])\. Longitude (\d+)-(\d+)-(\d+)([EW])\. Altitude (-?\d+) m\.\n"
    r"#+\n"
    r"\n"
    r"#+\n"
    r"#\s+SYNOPS from \d+[^\n]*\n"
    r"#+\n"
    r"(?P<body>[\s\S]*?)(?=\n\n#+\n# \d{5}|\Z)",
    re.MULTILINE,
)

_REPORT_RE = re.compile(
    r"^(?P<ts>\d{12}) (?P<wigos>\S+) (?P<sid>\d{5}) AAXX (?P<rest>[\s\S]*?)==",
    re.MULTILINE,
)


def _dms_to_decimal(d, m, s, hemi):
    val = int(d) + int(m) / 60.0 + int(s) / 3600.0
    return -val if hemi in ("S", "W") else val


def parse_station_blocks(text):
    """Yields (station_id, name, lat, lon, altitude_m, body_text)."""
    for m in _STATION_BLOCK_RE.finditer(text):
        sid, name, latd, latm, lats, lath, lond, lonm, lons, lonh, alt = m.groups()[:11]
        lat = _dms_to_decimal(latd, latm, lats, lath)
        lon = _dms_to_decimal(lond, lonm, lons, lonh)
        yield sid, name.strip(), lat, lon, int(alt), m.group("body")


def latest_report_tokens(body_text):
    """Returns (obs_time, tokens_after_AAXX) for the newest non-NIL report in
    this station's body (ord=REV means the body is already newest-first)."""
    for rm in _REPORT_RE.finditer(body_text):
        rest = rm.group("rest").split()
        if not rest or rest[0] == "NIL" or (len(rest) == 1 and rest[0].startswith("NIL")):
            continue
        ts = datetime.strptime(rm.group("ts"), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        return ts, rest
    return None, None


# ── Group decoders ────────────────────────────────────────────────────────────

def _decode_ppp_group(tok):
    """3PPPP / 4PPPP — pressure in tenths hPa, mod 1000 (see module docstring)."""
    digits = tok[1:5]
    if "/" in digits:
        return None
    ppp = int(digits)
    return (ppp + 10000) / 10.0 if ppp < 5000 else ppp / 10.0


def _decode_temp_group(tok):
    """1SnTTT / 2SnTdTdTd — signed temperature in tenths of degC."""
    sn, ttt = tok[1], tok[2:5]
    if "/" in ttt:
        return None
    val = int(ttt) / 10.0
    return -val if sn == "1" else val


def _decode_tendency_group(tok):
    """5appp — pressure tendency: characteristic code a (0-8) + 3h change (tenths hPa)."""
    a, ppp = tok[1], tok[2:5]
    if a == "/" or "/" in ppp:
        return None, None
    a = int(a)
    change = int(ppp) / 10.0
    if a in (5, 6, 7, 8):
        change = -change
    elif a == 4:
        change = 0.0
    return change, TENDENCY_DESC.get(a)


_VV_TABLE_BREAKS = [
    (0, 0, 50), (1, 50, None), (56, 80, None), (81, 88, None),
]


def _decode_vv(vv_str):
    """WMO Code Table 4377 (visibility, hVV group)."""
    if "/" in vv_str:
        return None
    vv = int(vv_str)
    if vv == 0:
        return 50
    if 1 <= vv <= 50:
        return vv * 100
    if 56 <= vv <= 80:
        return (vv - 50) * 1000
    if 81 <= vv <= 88:
        return int((30 + (vv - 80) * 5) * 1000)
    if vv == 89:
        return 75000
    special = {90: 50, 91: 50, 92: 200, 93: 500, 94: 1000, 95: 2000, 96: 4000, 97: 10000, 98: 20000, 99: 50000}
    return special.get(vv)


def _magnus_rh(temp_c, dewpoint_c):
    a, b = 17.625, 243.04
    if temp_c is None or dewpoint_c is None:
        return None
    e_td = math.exp((a * dewpoint_c) / (b + dewpoint_c))
    e_t = math.exp((a * temp_c) / (b + temp_c))
    return max(0.0, min(100.0, 100.0 * e_td / e_t))


def decode_report(tokens):
    """tokens: list of strings after 'AAXX' up to (not including) '=='.
    Returns a dict of decoded fields — missing/undecodable groups are simply
    absent from the result rather than raising."""
    out = {}
    if len(tokens) < 4:
        return out

    iw = None
    if tokens[0] and tokens[0][-1].isdigit():
        iw = int(tokens[0][-1])

    # tokens[1] = IIiii (station id, already known) — skip.
    irixhvv = tokens[2]
    if len(irixhvv) == 5:
        vv_code = irixhvv[3:5]
        vis = _decode_vv(vv_code)
        if vis is not None:
            out["vis_m"] = vis

    nddff = tokens[3]
    if len(nddff) == 5:
        n, dd, ff = nddff[0], nddff[1:3], nddff[3:5]
        if n != "/":
            out["cloud_oktas"] = int(n)
        if "/" not in dd and "/" not in ff:
            dd_i, ff_i = int(dd), int(ff)
            if dd_i <= 36:
                speed = float(ff_i)
                if iw in (0, 1):
                    speed *= 1.94384  # m/s -> kt
                out["wind_dir_deg"] = 0 if dd_i == 0 and ff_i == 0 else dd_i * 10
                out["wind_kt"] = round(speed, 1)

    SECTION_MARKERS = {"333", "444", "555", "666", "777"}
    for tok in tokens[4:]:
        if tok in SECTION_MARKERS:
            break
        if len(tok) < 5:
            continue
        indicator = tok[0]
        if indicator == "1":
            val = _decode_temp_group(tok)
            if val is not None:
                out["temp_c"] = val
        elif indicator == "2":
            val = _decode_temp_group(tok)
            if val is not None:
                out["dewpoint_c"] = val
        elif indicator == "3":
            val = _decode_ppp_group(tok)
            if val is not None:
                out["pressure_station_hpa"] = val
        elif indicator == "4":
            val = _decode_ppp_group(tok)
            if val is not None:
                out["mslp_hpa"] = val
        elif indicator == "5":
            change, desc = _decode_tendency_group(tok)
            if change is not None:
                out["pressure_tendency_hpa3h"] = change
                out["pressure_tendency_desc"] = desc
        elif indicator == "7" and len(tok) >= 3:
            ww_str = tok[1:3]
            if "/" not in ww_str:
                desc, symbol = decode_ww(int(ww_str))
                if desc:
                    out["present_wx"] = desc
                    out["present_wx_symbol"] = symbol

    if "rh_pct" not in out and out.get("temp_c") is not None and out.get("dewpoint_c") is not None:
        rh = _magnus_rh(out["temp_c"], out["dewpoint_c"])
        if rh is not None:
            out["rh_pct"] = round(rh)

    return out


# ── Build snapshot ─────────────────────────────────────────────────────────────

def build_features(text, now):
    features = []
    n_stations = n_nil = n_decoded = 0
    for sid, name, lat, lon, alt_m, body in parse_station_blocks(text):
        n_stations += 1
        obs_time, tokens = latest_report_tokens(body)
        if obs_time is None:
            n_nil += 1
            continue
        fields = decode_report(tokens)
        if not fields:
            continue
        n_decoded += 1
        stale = (now - obs_time).total_seconds() > FRESHNESS_MINUTES * 60
        props = {
            "sid": sid,
            "name": name,
            "altitude_m": alt_m,
            "obs_time": obs_time.strftime("%Y-%m-%dT%H:%M:00Z"),
            "stale": stale,
        }
        for k, v in fields.items():
            if isinstance(v, float):
                v = round(v, 1)
            props[k] = v
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
            "properties": props,
        })
    print(f"  {n_stations} station block(s) found, {n_nil} NIL/no-report, {n_decoded} decoded")
    return features


def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=LOOKBACK_HOURS)
    print(f"Fetching Ogimet SYNOP bulletins for UK, {start:%Y-%m-%d %H:%M} -> {now:%Y-%m-%d %H:%M} UTC...")
    try:
        text = fetch_ogimet_text(start, now)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Ogimet fetch failed: {e}")
        return

    features = build_features(text, now)
    if not features:
        print("No decodable stations — not writing (Ogimet may be between updates, not necessarily an error).")
        return

    geojson = {
        "type": "FeatureCollection",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "SYNOP observations via Ogimet (https://www.ogimet.com), sourced from the WMO Global "
                        "Telecommunication System — decoded from raw WMO FM-12 AAXX bulletins.",
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
