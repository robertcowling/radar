#!/usr/bin/env python3
"""
fetch_metar_taf.py — Fetches the latest METAR and TAF bulletins for UK
aerodromes from Ogimet's public archive and writes a combined GeoJSON
snapshot for metar.html.

Sources (free, public, no key — one request per run, run hourly):
  https://www.ogimet.com/ultimos_metars2.php?estado=United+K&fmt=txt
  https://www.ogimet.com/ultimos_tafs2.php?estado=United+K&fmt=txt

Coverage: ICAO-coded UK aerodromes (civil + military), a different set from
both surface_obs/ (Met Office AWS one-minute network) and synop/ (WMO 03xxx
synoptic stations) — though there's overlap, since major airports report on
all three. Station coordinates come from metar/airports.json, a static
reference built by build_airports.py (see that script's docstring for how
to regenerate it when a new ICAO code starts appearing in these feeds).

METAR decode is intentionally pragmatic, not full ICAO Annex 3 grammar:
wind, visibility (incl. CAVOK), first present-weather group, worst cloud
layer (as an oktas approximation for the station-plot glyph), temp/dewpoint,
and QNH. RVR, trend groups (BECMG/TEMPO/NOSIG at the end of a METAR), and
RMK free text are not parsed — not needed for a station-plot display.
present_wx is left as the raw METAR code (e.g. "-RA", "SHRA") so metar.html
can reuse surface_obs.html's existing wx_symbols.json lookup unchanged.

TAF is not decoded into structured fields — it's a multi-line forecast, not
a single instantaneous value, so the raw bulletin text is carried through
for display in a click-popup instead of station-plot iconography.
"""
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

AIRPORTS_PATH = os.path.join(os.path.dirname(__file__), "airports.json")
OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
LATEST_KEY = "metar/latest.geojson"
HISTORY_DIR = "metar/history"

METAR_URL = "https://www.ogimet.com/ultimos_metars2.php?lang=en&estado=United+K&fmt=txt&iord=yes&Send=Send"
TAF_URL   = "https://www.ogimet.com/ultimos_tafs2.php?lang=en&estado=United+K&fmt=txt&iord=yes&Send=Send"
UA = {"User-Agent": "floodforecast-radar/1.0 (+https://floodforecast.co.uk; metar/taf obs page)"}

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


def load_airports():
    with open(AIRPORTS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_ogimet_text(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    m = re.search(r"<pre>([\s\S]*?)</pre>", html)
    return m.group(1) if m else html


# ── METAR decode ────────────────────────────────────────────────────────────────

_METAR_REPORT_RE = re.compile(
    r"^(?P<ts>\d{12}) (?:METAR|SPECI) (?P<icao>[A-Z]{4}) (?P<rest>[\s\S]*?)=",
    re.MULTILINE,
)

_WIND_RE = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)$")
_VARWIND_RE = re.compile(r"^\d{3}V\d{3}$")
_VIS_RE = re.compile(r"^\d{4}$")
_CLOUD_RE = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?/{0,3}$")
_TEMPDEW_RE = re.compile(r"^(M?\d{2})/(M?\d{2})$")
_QNH_RE = re.compile(r"^Q(\d{4})$")
_WX_RE = re.compile(
    r"^(?:\+|-|VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?"
    r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|DS|SS){1,3}$"
)

_CLOUD_RANK = {"FEW": 2, "SCT": 4, "BKN": 6, "OVC": 8, "VV": 9}


def _magnus_rh(temp_c, dewpoint_c):
    a, b = 17.625, 243.04
    if temp_c is None or dewpoint_c is None:
        return None
    e_td = math.exp((a * dewpoint_c) / (b + dewpoint_c))
    e_t = math.exp((a * temp_c) / (b + temp_c))
    return max(0.0, min(100.0, 100.0 * e_td / e_t))


def _parse_tempdew(tok):
    def val(s):
        return -float(s[1:]) if s.startswith("M") else float(s)
    m = _TEMPDEW_RE.match(tok)
    if not m:
        return None, None
    t_str, td_str = m.groups()
    if "/" in t_str or "/" in td_str:
        return None, None
    return val(t_str), (val(td_str) if td_str.replace("M", "").isdigit() else None)


def decode_metar(rest_text):
    tokens = rest_text.split()
    out = {}
    if not tokens or tokens[0] == "NIL":
        return out

    cloud_layer = None  # highest-rank (cover, base_ft)
    zero_cloud = False
    for tok in tokens[1:]:  # tokens[0] is the DDHHMMZ group
        if tok == "RMK":
            break
        if tok in ("AUTO", "COR", "NOSIG"):
            continue
        if tok == "CAVOK":
            out["vis_m"] = 10000
            zero_cloud = True
            continue
        m = _WIND_RE.match(tok)
        if m:
            dir_str, speed, gust, unit = m.groups()
            speed_kt = float(speed) * (1.94384 if unit == "MPS" else 1.0)
            out["wind_kt"] = round(speed_kt, 1)
            out["wind_dir_deg"] = None if dir_str == "VRB" else int(dir_str)
            if gust:
                out["wind_gust_kt"] = round(float(gust) * (1.94384 if unit == "MPS" else 1.0), 1)
            continue
        if _VARWIND_RE.match(tok):
            continue
        if "vis_m" not in out and _VIS_RE.match(tok):
            vis = int(tok)
            out["vis_m"] = 10000 if vis == 9999 else vis
            continue
        m = _CLOUD_RE.match(tok)
        if m:
            cover, base, _kind = m.groups()
            rank = _CLOUD_RANK[cover]
            if cloud_layer is None or rank > cloud_layer[0]:
                base_ft = None if base == "///" else int(base) * 100
                cloud_layer = (rank, base_ft)
            continue
        if tok in ("NCD", "NSC"):
            zero_cloud = True
            continue
        m = _TEMPDEW_RE.match(tok)
        if m and "temp_c" not in out:
            t, td = _parse_tempdew(tok)
            if t is not None:
                out["temp_c"] = t
            if td is not None:
                out["dewpoint_c"] = td
            continue
        m = _QNH_RE.match(tok)
        if m:
            out["qnh_hpa"] = float(m.group(1))
            continue
        if "present_wx" not in out and _WX_RE.match(tok):
            out["present_wx"] = tok
            continue

    if cloud_layer is not None:
        out["cloud_oktas"] = cloud_layer[0]
        if cloud_layer[1] is not None:
            out["cloud_base_ft"] = cloud_layer[1]
    elif zero_cloud:
        out["cloud_oktas"] = 0

    if out.get("temp_c") is not None and out.get("dewpoint_c") is not None:
        rh = _magnus_rh(out["temp_c"], out["dewpoint_c"])
        if rh is not None:
            out["rh_pct"] = round(rh)

    return out


def parse_metars(text):
    """Returns {icao: (obs_time, raw_text, decoded_fields)} — latest report
    per station (ogimet's iord=yes already orders newest-first)."""
    stations = {}
    for m in _METAR_REPORT_RE.finditer(text):
        icao = m.group("icao")
        if icao in stations:
            continue  # already have the newest for this station
        ts = datetime.strptime(m.group("ts"), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        rest = " ".join(m.group("rest").split())
        stations[icao] = (ts, rest, decode_metar(m.group("rest")))
    return stations


# ── TAF (kept as raw text, not decoded into fields) ────────────────────────────

_TAF_REPORT_RE = re.compile(
    r"^(?P<ts>\d{12}) TAF(?: (?P<flag>AMD|COR))? (?P<icao>[A-Z]{4}) (?P<rest>[\s\S]*?)=",
    re.MULTILINE,
)


def parse_tafs(text):
    """Returns {icao: (issued_time, flag, raw_text)} — latest TAF per station."""
    stations = {}
    for m in _TAF_REPORT_RE.finditer(text):
        icao = m.group("icao")
        if icao in stations:
            continue
        ts = datetime.strptime(m.group("ts"), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        raw = " ".join(m.group("rest").split())
        stations[icao] = (ts, m.group("flag"), raw)
    return stations


# ── Build snapshot ─────────────────────────────────────────────────────────────

def build_features(metars, tafs, airports, now):
    features = []
    unmapped = set()
    for icao in sorted(set(metars) | set(tafs)):
        airport = airports.get(icao)
        if not airport:
            unmapped.add(icao)
            continue

        props = {"icao": icao, "name": airport["name"]}

        if icao in metars:
            obs_time, raw, fields = metars[icao]
            props["metar_raw"] = raw
            props["metar_obs_time"] = obs_time.strftime("%Y-%m-%dT%H:%M:00Z")
            props["stale"] = (now - obs_time).total_seconds() > FRESHNESS_MINUTES * 60
            for k, v in fields.items():
                props[k] = round(v, 1) if isinstance(v, float) else v
        else:
            props["stale"] = True  # no current METAR at all

        if icao in tafs:
            issued, flag, raw = tafs[icao]
            props["taf_raw"] = raw
            props["taf_issued"] = issued.strftime("%Y-%m-%dT%H:%M:00Z")
            if flag:
                props["taf_flag"] = flag

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [airport["lon"], airport["lat"]]},
            "properties": props,
        })

    if unmapped:
        print(f"  {len(unmapped)} ICAO code(s) with no entry in airports.json (skipped): {sorted(unmapped)}")
        print("  Run metar/build_airports.py to add them.")
    return features


def main():
    now = datetime.now(timezone.utc)
    airports = load_airports()

    print("Fetching latest METARs...")
    try:
        metar_text = fetch_ogimet_text(METAR_URL)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  METAR fetch failed: {e}")
        metar_text = ""
    metars = parse_metars(metar_text) if metar_text else {}
    print(f"  {len(metars)} station(s)")

    print("Fetching latest TAFs...")
    try:
        taf_text = fetch_ogimet_text(TAF_URL)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  TAF fetch failed: {e}")
        taf_text = ""
    tafs = parse_tafs(taf_text) if taf_text else {}
    print(f"  {len(tafs)} station(s)")

    if not metars and not tafs:
        print("No data from either feed — not writing (Ogimet may be unreachable, not necessarily an error).")
        return

    features = build_features(metars, tafs, airports, now)
    if not features:
        print("No stations mapped to known coordinates — nothing to write.")
        return

    geojson = {
        "type": "FeatureCollection",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "METAR/TAF via Ogimet (https://www.ogimet.com), sourced from the WMO Global "
                        "Telecommunication System.",
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
