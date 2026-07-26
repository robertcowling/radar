#!/usr/bin/env python3
"""
build_airports.py — (Re)builds metar/airports.json, the static ICAO ->
{name, lat, lon} reference used by fetch_metar_taf.py.

Run manually (not part of any scheduled workflow — matches the precedent of
surface_obs/build_station_altitude.py). Re-run this when fetch_metar_taf.py
logs "ICAO code(s) with no entry in airports.json" for a station that's
started reporting and isn't covered yet.

Sources, in priority order:
  1. OurAirports' public-domain airports.csv (davidmegginson.github.io
     mirror) — has name + coordinates for almost every civil aerodrome.
  2. Ogimet's SYNOP station list (ultimos synops carry an "ICAO index" field
     alongside lat/lon) — covers UK military airfields OurAirports doesn't
     always have under their ICAO code.
  3. MANUAL_OVERRIDES below — the handful of stations (mostly small RAF
     ranges) neither source has. Add new entries here if step 1+2 can't
     resolve a station either; a plain map/satellite view is enough to get
     a good-enough lat/lon for a station-plot marker.

Needs today's live METAR + TAF feeds to know which ICAO codes to resolve —
this script queries them itself rather than taking a fixed list, so it
naturally covers new stations as they start appearing.
"""
import csv
import io
import json
import os
import re
import urllib.request

OUT_PATH = os.path.join(os.path.dirname(__file__), "airports.json")

OURAIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
METAR_URL = "https://www.ogimet.com/ultimos_metars2.php?lang=en&estado=United+K&fmt=txt&iord=yes&Send=Send"
TAF_URL   = "https://www.ogimet.com/ultimos_tafs2.php?lang=en&estado=United+K&fmt=txt&iord=yes&Send=Send"
SYNOP_URL = ("https://www.ogimet.com/display_synopsc2.php?lang=en&estado=United+K&tipo=ALL&ord=REV"
             "&nil=SI&fmt=txt&ano={y}&mes={mo:02d}&day={d:02d}&hora=00&anof={y}&mesf={mo:02d}&dayf={d:02d}&horaf=23&send=send")
UA = {"User-Agent": "floodforecast-radar/1.0 (+https://floodforecast.co.uk; airport reference builder)"}

# Stations neither OurAirports nor the Ogimet SYNOP list resolve, as of the
# last time this was run — small RAF airfields with no listed ICAO/coords in
# either source. Coordinates here are approximate (public knowledge / map
# lookup), fine for a station-plot marker.
MANUAL_OVERRIDES = {
    "EGXS": {"name": "RAF Scampton", "lat": 53.3081, "lon": -0.5511},
    "EGXV": {"name": "RAF Barkston Heath", "lat": 52.9656, "lon": -0.5664},
}


def fetch_text(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_pre(url, timeout=30):
    html = fetch_text(url, timeout)
    m = re.search(r"<pre>([\s\S]*?)</pre>", html)
    return m.group(1) if m else html


def needed_icaos():
    metar_text = fetch_pre(METAR_URL)
    taf_text = fetch_pre(TAF_URL)
    metar_icaos = set(re.findall(r"METAR ([A-Z]{4}) \d{6}Z", metar_text))
    taf_icaos = set(re.findall(r"TAF (?:AMD |COR )?([A-Z]{4}) \d{6}Z", taf_text))
    return metar_icaos | taf_icaos


def from_ourairports(icaos):
    print("Fetching OurAirports reference data...")
    csv_text = fetch_text(OURAIRPORTS_CSV_URL, timeout=60)
    result = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        code = row.get("icao_code") or row.get("ident")
        if code in icaos:
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            result[code] = {"name": row["name"], "lat": round(lat, 4), "lon": round(lon, 4)}
    print(f"  Resolved {len(result)}/{len(icaos)} from OurAirports")
    return result


_SYNOP_STATION_RE = re.compile(
    r"^# (\d{5}), (.+?) \(United Kingdom\)\n"
    r"# WIGOS Id: [^\n]*\n"
    r"# ICAO index: (\S+)\n"
    r"# Latitude (\d+)-(\d+)-(\d+)([NS])\. Longitude (\d+)-(\d+)-(\d+)([EW])\. Altitude (-?\d+) m\.",
    re.MULTILINE,
)


def _dms(d, m, s, hemi):
    v = int(d) + int(m) / 60.0 + int(s) / 3600.0
    return -v if hemi in ("S", "W") else v


def from_synop(icaos, missing):
    if not missing:
        return {}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    print("Fetching Ogimet SYNOP station list for supplemental coordinates...")
    text = fetch_pre(SYNOP_URL.format(y=now.year, mo=now.month, d=now.day), timeout=45)
    result = {}
    for m in _SYNOP_STATION_RE.finditer(text):
        icao = m.group(3)
        if icao == "----" or icao not in missing:
            continue
        lat = _dms(*m.groups()[3:7])
        lon = _dms(*m.groups()[7:11])
        result[icao] = {"name": m.group(2).strip(), "lat": round(lat, 4), "lon": round(lon, 4)}
    print(f"  Resolved {len(result)}/{len(missing)} from SYNOP station metadata")
    return result


def main():
    icaos = needed_icaos()
    print(f"{len(icaos)} distinct ICAO code(s) across current METAR + TAF feeds")

    result = from_ourairports(icaos)
    missing = icaos - result.keys()
    result.update(from_synop(icaos, missing))

    still_missing = icaos - result.keys()
    for code in still_missing:
        if code in MANUAL_OVERRIDES:
            result[code] = MANUAL_OVERRIDES[code]
    still_missing -= MANUAL_OVERRIDES.keys()

    if still_missing:
        print(f"UNRESOLVED — add these to MANUAL_OVERRIDES: {sorted(still_missing)}")

    result = dict(sorted(result.items()))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    print(f"Wrote {OUT_PATH} ({len(result)} station(s))")


if __name__ == "__main__":
    main()
