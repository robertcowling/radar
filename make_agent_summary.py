#!/usr/bin/env python3
"""
make_agent_summary.py

Generates an AI weather briefing by assembling multi-source observational
and forecast context, selecting radar/MSLP/forecast images, calling
ChatGPT (vision), and writing agent/summary.json.

Data sources (all from local git repo or public R2):
  warnings/warnings_latest.json   Met Office warnings
  rfg/rfg_latest.json             Rapid Flood Guidance
  FFC API /fgs                    5-day Flood Guidance Statement
  rain/meta.json + R2             Rain gauge accumulation stats
  rain/stations.json              Station metadata
  ukv_meta.json                   UKV NWP forecast
  cosmos/*.json                   Soil moisture (COSMOS network)
  frames_parallel.json            Radar image URLs
  frames_accum_daily.json         24hr accumulation image URL
  frames_mslp.json                MSLP chart URLs
"""

import json
import os
import re
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from glob import glob

# ── Config ──────────────────────────────────────────────────────────────────────

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = (os.environ.get("OPENAI_MODEL", "") or "gpt-5.4").strip()
def _ffc_key_fallback():
    """Read the FFC API key already committed in fetch_rfg.py as a fallback."""
    try:
        with open("fetch_rfg.py", encoding="utf-8") as _f:
            m = re.search(r'"([a-f0-9]{64})"', _f.read())
            return m.group(1) if m else ""
    except Exception:
        return ""

FFC_API_KEY = os.environ.get("FFC_API_KEY", "").strip() or _ffc_key_fallback()
FGS_API_BASE   = "https://api.ffc-environment-agency.fgs.metoffice.gov.uk/api/public/v3"
OUTPUT_PATH    = "agent/summary.json"

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET     = os.environ.get("R2_BUCKET_NAME", "").strip()


# ── Generic helpers ──────────────────────────────────────────────────────────────

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def http_get_json(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  HTTP {url[:80]}: {e}")
        return None


def r2_get_json(key):
    """Fetch a JSON object directly from R2 using authenticated boto3 access."""
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET]):
        return None
    try:
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )
        resp = client.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(resp["Body"].read())
    except Exception as e:
        print(f"  R2 {key}: {e}")
        return None


def now_utc():
    return datetime.now(timezone.utc)


# ── Region assignment (bounding-box lookup) ───────────────────────────────────────

def assign_region(lat, lon):
    if lat >= 55.0:
        return "Scotland"
    if lat >= 54.0:
        return "NW England" if lon <= -2.5 else "NE England"
    if lat >= 53.0:
        if lon <= -3.5:
            return "Wales"
        return "NW England" if lon <= -2.0 else "Yorkshire"
    if lat >= 52.0:
        if lon <= -3.5:
            return "Wales"
        return "Midlands" if lon <= -0.5 else "E England"
    if lat >= 51.0:
        return "SW England" if lon <= -1.5 else "SE England"
    return "SW England" if lon <= -1.0 else "SE England"


# ── Warnings ──────────────────────────────────────────────────────────────────────

def load_warnings():
    data = load_json("warnings/warnings_latest.json", {})
    now_str = now_utc().isoformat()
    active = []
    for w in data.get("warnings", []):
        end = w.get("end_date", "")
        if end and end < now_str:
            continue
        storm = None
        title = w.get("title", "")
        m = re.search(r"\bStorm\s+(\w+)\b", title, re.I)
        if m:
            storm = f"Storm {m.group(1)}"
        else:
            for tag in w.get("tag", []):
                if tag.lower().startswith("storm"):
                    storm = tag
                    break
        active.append({
            "severity": w.get("severity", "unknown"),
            "title":    title,
            "area":     w.get("area_desc", ""),
            "headline": w.get("headline", ""),
            "start":    w.get("start_date", ""),
            "end":      end,
            "storm":    storm,
        })
    return active, data.get("generated_at", "")


# ── RFG ───────────────────────────────────────────────────────────────────────────

def load_rfg():
    data = load_json("rfg/rfg_latest.json", {})
    status = data.get("status", "Status not available")
    latest = data.get("latest_rfg", {})
    issued_at = latest.get("issued_at") if latest else None
    if issued_at:
        try:
            dt = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
            if (now_utc() - dt).total_seconds() > 72 * 3600:
                issued_at = None
        except Exception:
            issued_at = None
    return {
        "status":       status,
        "issued_at":    issued_at,
        "generated_at": data.get("generated_at", ""),
    }


# ── FGS (5-day Flood Guidance Statement) ──────────────────────────────────────────

def fetch_fgs_history(n=4):
    """Return list of up to n recent FGS statements (newest first)."""
    data = http_get_json(
        f"{FGS_API_BASE}/fgs",
        headers={"x-api-key": FFC_API_KEY},
    )
    if isinstance(data, list):
        return data[:n]
    elif data:
        return [data]
    return []


def fmt_fgs(fgs_data):
    if not fgs_data:
        return "FGS data not available."
    lines = []
    issued = (fgs_data.get("issued_at") or fgs_data.get("issuedAt") or "")
    if issued:
        lines.append(f"Issued: {issued}")
    areas = (fgs_data.get("areas") or fgs_data.get("guidance_areas") or
             fgs_data.get("floodAreas") or [])
    if isinstance(areas, list):
        for area in areas[:12]:
            name = (area.get("area_name") or area.get("name") or area.get("areaName") or "Unknown")
            risks = []
            for day_n in range(1, 6):
                for key in (f"day{day_n}", f"day_{day_n}"):
                    day = area.get(key) or {}
                    if isinstance(day, dict):
                        risk = (day.get("risk") or day.get("riskLevel") or "").strip()
                        if risk:
                            risks.append(f"D{day_n}:{risk}")
                            break
            if risks:
                lines.append(f"  {name}: {', '.join(risks)}")
    elif isinstance(areas, dict):
        for name, detail in list(areas.items())[:12]:
            risk = detail.get("risk") or detail.get("level") or str(detail)[:60]
            lines.append(f"  {name}: {risk}")
    if not lines:
        for key in ("summary", "overall_risk", "headline", "text", "description"):
            val = fgs_data.get(key)
            if val and isinstance(val, str):
                lines.append(val[:400])
                break
    if not lines:
        lines.append(f"FGS received — structure: {list(fgs_data.keys())[:6]}")
    return "\n".join(lines)


def fmt_fgs_history(fgs_list):
    """Format multiple FGS statements to show 3-day trend."""
    if not fgs_list:
        return "FGS data not available."
    parts = []
    for i, stmt in enumerate(fgs_list):
        issued = stmt.get("issued_at") or stmt.get("issuedAt") or ""
        label = f"Statement {i+1}" + (f" (issued {issued})" if issued else "")
        parts.append(f"--- {label} ---")
        parts.append(fmt_fgs(stmt))
    return "\n".join(parts)


# ── Rain gauge observations ───────────────────────────────────────────────────────

def load_rain_gauge_stats():
    meta     = load_json("rain/meta.json", {})
    r2_base  = meta.get("r2_base_url", "").rstrip("/")
    stations = load_json("rain/stations.json", {}).get("stations", {})
    if not r2_base or not stations:
        print("  Gauge: missing meta or stations data")
        return None, None, None, meta.get("latest_time", "")

    n = now_utc()
    today_str      = n.strftime("%Y%m%d")
    yesterday_str  = (n - timedelta(days=1)).strftime("%Y%m%d")
    current_slot   = (n.hour * 60 + n.minute) // 15

    def fetch_day(date_str):
        key  = f"rain/readings/{date_str}.json"
        data = r2_get_json(key)
        if data is None:
            data = http_get_json(f"{r2_base}/rain/readings/{date_str}.json")
        return (data or {}).get("readings", {})

    print("  Gauge: fetching readings...")
    today_r     = fetch_day(today_str)
    yesterday_r = fetch_day(yesterday_str)
    if not today_r and not yesterday_r:
        return None, None, None, meta.get("latest_time", "")

    def slot_val(day_dict, sid, idx):
        if idx < 0:
            return 0.0
        return float((day_dict.get(sid) or {}).get(str(idx), 0) or 0)

    station_stats = []
    last = current_slot - 1  # last fully complete slot
    for sid, info in stations.items():
        lat, lon = info.get("lat", 0), info.get("lon", 0)
        name     = info.get("name", sid).strip()
        region   = assign_region(lat, lon)

        def acc(n_slots):
            total = 0.0
            for i in range(n_slots):
                idx = last - i
                total += (slot_val(today_r, sid, idx) if idx >= 0
                          else slot_val(yesterday_r, sid, 96 + idx))
            return total

        a24 = acc(96)
        if a24 < 0.1:
            continue
        station_stats.append({
            "name": name, "region": region,
            "acc_1hr":  round(acc(4),  1),
            "acc_6hr":  round(acc(24), 1),
            "acc_24hr": round(a24,     1),
        })

    if not station_stats:
        return None, None, None, meta.get("latest_time", "")

    by_region = defaultdict(list)
    for s in station_stats:
        by_region[s["region"]].append(s)

    regional = {}
    for reg, stns in sorted(by_region.items()):
        v24 = [s["acc_24hr"] for s in stns]
        v1  = [s["acc_1hr"]  for s in stns]
        regional[reg] = {
            "max_24hr":      round(max(v24), 1),
            "mean_24hr":     round(sum(v24) / len(v24), 1),
            "over_10mm_1hr": sum(1 for v in v1 if v > 10),
            "over_30mm_1hr": sum(1 for v in v1 if v > 30),
            "n_stations":    len(stns),
        }

    top10 = sorted(station_stats, key=lambda s: s["acc_24hr"], reverse=True)[:10]

    # Rainfall trend: compare last 1hr total vs expected from 6hr average
    total_1hr = sum(s["acc_1hr"] for s in station_stats)
    total_6hr = sum(s["acc_6hr"] for s in station_stats)
    if total_6hr < 1.0:
        trend = {"direction": "dry", "note": "very little rain in last 6hrs"}
    else:
        avg_1hr = total_6hr / 6.0
        ratio   = total_1hr / avg_1hr if avg_1hr > 0 else 1.0
        if ratio > 1.3:
            trend = {"direction": "intensifying", "pct_above_avg": round((ratio - 1) * 100)}
        elif ratio < 0.7:
            trend = {"direction": "easing", "pct_below_avg": round((1 - ratio) * 100)}
        else:
            trend = {"direction": "steady"}

    return regional, top10, trend, meta.get("latest_time", "")


# ── Soil moisture (COSMOS) ────────────────────────────────────────────────────────

def load_cosmos_moisture():
    files = sorted(glob("cosmos/*.json"))
    if not files:
        return None
    data = load_json(files[-1], {})
    date_str = data.get("date", "")
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if (now_utc() - dt.replace(tzinfo=timezone.utc)).total_seconds() > 48 * 3600:
                return None
        except Exception:
            pass
    readings = data.get("readings", {})
    vwcs = [v["vwc"] for v in readings.values() if isinstance(v, dict) and v.get("vwc") is not None]
    if not vwcs:
        return None
    mean_vwc = round(sum(vwcs) / len(vwcs), 1)
    if mean_vwc >= 55:
        condition = "very wet — high antecedent moisture, rapid surface runoff likely"
    elif mean_vwc >= 40:
        condition = "above average — moderately wet soils"
    elif mean_vwc >= 25:
        condition = "near average"
    else:
        condition = "dry — low antecedent moisture, reduced direct runoff"
    return {
        "date":        date_str[:10],
        "mean_vwc":    mean_vwc,
        "max_vwc":     round(max(vwcs), 1),
        "n_stations":  len(vwcs),
        "condition":   condition,
    }


# ── Image selection ───────────────────────────────────────────────────────────────

def parse_frame_time(time_str):
    try:
        return datetime.strptime(time_str, "%a %d %b %Y %H:%M UTC").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── UKV poly numerical forecast ───────────────────────────────────────────────────

def load_ukv_poly_text():
    """Fetch UKV regional poly accumulations from R2 for multiple forecast steps."""
    meta = load_json("ukv_meta.json", {})
    runs = meta.get("runs", [])
    if not runs:
        return None, None
    run      = runs[0]
    run_ts   = run.get("run_ts", "")
    run_label = run.get("run_label", run_ts)
    # Build offset_hours → step lookup
    steps_by_h = {s.get("offset_hours"): s for s in run.get("steps", [])}

    lines = [f"UKV NWP run {run_label} — cumulative regional rainfall forecasts:"]
    # Periods: cumulative accum up to T+6, T+12, T+24, T+48
    target_offsets = [(6, "accum_6h"), (12, "accum_12h"), (24, "accum_24h"), (48, "accum_48h")]
    got_any = False
    for offset_h, accum_key in target_offsets:
        step = steps_by_h.get(offset_h)
        if not step:
            continue
        valid_label = step.get("valid_label", f"T+{offset_h}h")
        # Try zero-padded and plain integer key formats
        poly_data = None
        for fmt in (f"{offset_h:03d}", str(offset_h)):
            poly_data = r2_get_json(f"ukv_poly/{run_ts}/{fmt}.json")
            if poly_data:
                break
        if not poly_data:
            continue
        regional_mm = (poly_data.get("regions") or {}).get(accum_key, {})
        if not regional_mm:
            continue
        got_any = True
        lines.append(f"\n  T+{offset_h}h (valid {valid_label}) — {offset_h}hr cumul. by region (England & Wales focus):")
        # Sort by highest rainfall, skip Scotland for E&W focus but include if significant
        for region, mm in sorted(regional_mm.items(), key=lambda x: -x[1])[:12]:
            lines.append(f"    {region}: {float(mm):.1f} mm")

    if not got_any:
        return None, run_label
    return "\n".join(lines), run_label


def pick_nearest(frames, target_dt, time_key="time"):
    best, best_d = None, None
    for f in frames:
        t = parse_frame_time(f.get(time_key, ""))
        if t is None:
            continue
        d = abs((t - target_dt).total_seconds())
        if best_d is None or d < best_d:
            best, best_d = f, d
    return best


def select_images():
    images = []
    n = now_utc()

    # ── Radar composites ─────────────────────────────────────────────────────────
    radar = load_json("frames_parallel.json", [])
    if radar:
        offsets_min = [0, 60, 120, 360, 720]
        labels      = ["most recent", "~1hr ago", "~2hr ago", "~6hr ago", "~12hr ago"]
        seen = set()
        for offset, lbl in zip(offsets_min, labels):
            f = pick_nearest(radar, n - timedelta(minutes=offset))
            if not f:
                continue
            url = f.get("dashboard_radar_url") or f.get("url")
            if url and url not in seen:
                seen.add(url)
                images.append((f"Radar composite — {f['time']} ({lbl})", url))

    # ── 24hr accumulation ─────────────────────────────────────────────────────────
    accum_daily = load_json("frames_accum_daily.json", [])
    if accum_daily:
        today_frame = accum_daily[0]
        images.append((
            f"24hr accumulated rainfall today — ending {n.strftime('%H:%M UTC %d %b %Y')}",
            today_frame["url"],
        ))

    # ── MSLP analysis ─────────────────────────────────────────────────────────────
    mslp = load_json("frames_mslp.json", [])
    for f in mslp[-2:]:
        images.append((f"MSLP analysis — {f['time']}", f["mslp"]))

    return images


# ── System prompt ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert UK weather analyst writing a concise operational briefing for flood risk and emergency planning practitioners in England and Wales. You have been given structured observational and forecast data PLUS radar composite images, a 24hr rainfall accumulation map, and MSLP analysis charts.

Respond with a JSON object inside <json> tags with exactly these fields:
- "headline": One punchy sentence (maximum 20 words) capturing the most significant current weather situation or outlook.
- "summary": Three paragraphs separated by double newline (\\n\\n):
    Para 1 — Observations: where heaviest rainfall has fallen over the past 24 hours across England and Wales; integrate gauge data AND describe what the radar images show about spatial distribution and evolution; note whether rainfall is widespread or convective and localised. Where appropriate, name specific regions (e.g. North West England, South Wales, Yorkshire) or smaller units (catchments, counties) when the data supports it.
    Para 2 — Hazards and Guidance: active Met Office warnings — name any named storms explicitly; Rapid Flood Guidance status; Flood Guidance Statement 5-day outlook by area — if multiple statements are provided, note any trend in risk levels day-to-day; threshold exceedances (10 mm/1hr heavy, 30 mm/1hr extreme flash flood risk); antecedent soil moisture and its implications for runoff.
    Para 3 — Forecast: use the UKV NWP regional accumulation tables (provided as structured data) to describe forecast rainfall totals for England and Wales by region over the next 6, 12, 24 and 48 hours; integrate the synoptic pattern from the MSLP charts; identify the areas of greatest concern in the outlook period and the timing of any peak rainfall.

Rainfall significance thresholds:
  10 mm/1hr: heavy  |  30 mm/1hr: extreme, flash flood risk
  25 mm/3hr: significant  |  40 mm/3hr: very significant
  50 mm/6hr or 100 mm/24hr: exceptional

Write in professional, factual third-person prose. No bullet points within the summary. Use specific place names and mm values from the data. Do not speculate beyond what the data shows. Use UTC for all times. If a data source shows no significant activity, state that briefly and move on."""


# ── Prompt data block ─────────────────────────────────────────────────────────────

def build_data_block(warnings, rfg, fgs_history_text, gauge_regional, gauge_top10, gauge_trend,
                     cosmos, ukv_poly_text, rain_latest_time):
    L = []
    L.append(f"Generated: {now_utc().strftime('%Y-%m-%dT%H:%M UTC')}\n")

    # Warnings
    L.append("=== ACTIVE MET OFFICE WARNINGS ===")
    if warnings:
        for w in warnings:
            L.append(f"[{w['severity'].upper()}] {w['title']}")
            if w.get("storm"):
                L.append(f"  Named storm: {w['storm']}")
            if w.get("area"):
                L.append(f"  Areas: {w['area']}")
            s = (w.get("start") or "")[:16].replace("T", " ")
            e = (w.get("end") or "")[:16].replace("T", " ")
            L.append(f"  Valid: {s} → {e} UTC")
            if w.get("headline"):
                L.append(f"  Headline: {w['headline']}")
    else:
        L.append("No active Met Office warnings.")
    L.append("")

    # RFG
    L.append("=== RAPID FLOOD GUIDANCE (RFG) ===")
    L.append(f"Status: {rfg['status']}")
    if rfg.get("issued_at"):
        L.append(f"Last issued: {rfg['issued_at']}")
    L.append("")

    # FGS
    L.append("=== FLOOD GUIDANCE STATEMENT (FGS) — 5-day flood risk outlook (last 3 days shown) ===")
    L.append(fgs_history_text or "FGS data not available.")
    L.append("")

    # Gauges
    L.append("=== OBSERVED RAINFALL — RAIN GAUGE NETWORK ===")
    if rain_latest_time:
        L.append(f"Latest data: {rain_latest_time}")
    if gauge_regional:
        L.append("")
        L.append(f"{'Region':<22} {'Max 24hr':>9} {'Mean 24hr':>10} {'>10mm/1hr':>10} {'>30mm/1hr':>10}")
        L.append("-" * 65)
        for reg, s in sorted(gauge_regional.items(), key=lambda x: -x[1]["max_24hr"]):
            L.append(
                f"{reg:<22} {s['max_24hr']:>8.1f}mm {s['mean_24hr']:>9.1f}mm"
                f" {s['over_10mm_1hr']:>10} {s['over_30mm_1hr']:>10}"
            )
        L.append("")
        if gauge_top10:
            L.append("Top 10 stations by 24hr accumulation:")
            for st in gauge_top10:
                L.append(f"  {st['name']} ({st['region']}): "
                         f"1hr={st['acc_1hr']}mm  6hr={st['acc_6hr']}mm  24hr={st['acc_24hr']}mm")
        L.append("")
        if gauge_trend:
            d = gauge_trend["direction"]
            note = ""
            if "pct_above_avg" in gauge_trend:
                note = f" (+{gauge_trend['pct_above_avg']}% above 6hr mean rate)"
            elif "pct_below_avg" in gauge_trend:
                note = f" (-{gauge_trend['pct_below_avg']}% below 6hr mean rate)"
            elif gauge_trend.get("note"):
                note = f" ({gauge_trend['note']})"
            L.append(f"Trend (last 1hr vs 6hr mean): {d}{note}")
    else:
        L.append("Gauge readings unavailable for this run.")
    L.append("")

    # Soil moisture
    if cosmos:
        L.append("=== ANTECEDENT SOIL CONDITIONS (COSMOS network) ===")
        L.append(f"Date: {cosmos['date']}  |  Stations: {cosmos['n_stations']}")
        L.append(f"Mean VWC: {cosmos['mean_vwc']}%  (max: {cosmos['max_vwc']}%)")
        L.append(f"Assessment: {cosmos['condition']}")
        L.append("")

    # UKV
    L.append("=== UKV NWP FORECAST — REGIONAL ACCUMULATIONS ===")
    L.append(ukv_poly_text or "UKV numerical data not available.")
    L.append("")

    # Image context
    L.append("=== IMAGERY (attached below) ===")
    L.append("  5 radar rate composites — most recent + 1hr, 2hr, 6hr, 12hr ago")
    L.append("  1 radar 24hr accumulation map — total rainfall today")
    L.append("  2 MSLP analysis charts — surface pressure and frontal systems")

    return "\n".join(L)


# ── OpenAI call ───────────────────────────────────────────────────────────────────

def call_openai(data_block, images):
    try:
        import openai
    except ImportError:
        print("openai package not installed")
        return None
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY not set — skipping")
        return None

    content = [{"type": "text", "text": data_block}]
    for label, url in images:
        content.append({"type": "text",      "text":      f"\n{label}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": content},
            ],
            max_completion_tokens=800,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"OpenAI error: {e}")
        return None


def parse_openai_response(text):
    if not text:
        return None
    m = re.search(r"<json>(.*?)</json>", text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    return {"headline": "", "summary": text}


# ── Context bullets ───────────────────────────────────────────────────────────────

def build_bullets(warnings, rfg, fgs_text, gauge_regional, gauge_top10,
                  gauge_trend, cosmos, images, parsed):
    B = []

    if warnings:
        counts = {}
        for w in warnings:
            s = w["severity"].lower()
            counts[s] = counts.get(s, 0) + 1
        parts = []
        for sev, label in [("extreme","Red"),("severe","Amber"),("moderate","Yellow"),("minor","Green")]:
            if counts.get(sev):
                parts.append(f"{counts[sev]} {label}")
        storms = list({w["storm"] for w in warnings if w["storm"]})
        storm_str = f"; {', '.join(storms)}" if storms else ""
        B.append(f"{len(warnings)} active warning{'s' if len(warnings)>1 else ''} ({', '.join(parts) or 'active'}){storm_str}")
    else:
        B.append("No active Met Office warnings")

    B.append(f"RFG: {rfg['status'][:80]}")

    if fgs_text and "not available" not in fgs_text.lower():
        B.append(f"FGS: {fgs_text.split(chr(10))[0][:80]}")

    if gauge_top10:
        top = gauge_top10[0]
        B.append(f"Peak 24hr gauge: {top['acc_24hr']}mm at {top['name']} ({top['region']})")
    if gauge_regional:
        over10 = sum(v.get("over_10mm_1hr", 0) for v in gauge_regional.values())
        over30 = sum(v.get("over_30mm_1hr", 0) for v in gauge_regional.values())
        if over10 or over30:
            B.append(f"Threshold exceedances: {over10} stns >10mm/1hr; {over30} stns >30mm/1hr")
    if gauge_trend:
        d = gauge_trend["direction"]
        if d == "intensifying":
            B.append(f"Rainfall trend: intensifying (+{gauge_trend.get('pct_above_avg',0)}% vs 6hr mean rate)")
        elif d == "easing":
            B.append(f"Rainfall trend: easing (-{gauge_trend.get('pct_below_avg',0)}% vs 6hr mean rate)")
        elif d == "dry":
            B.append("Rainfall trend: very little rain in recent hours")
        else:
            B.append("Rainfall trend: steady")

    if cosmos:
        B.append(f"Soil moisture: {cosmos['condition'].split('—')[0].strip()} (mean VWC {cosmos['mean_vwc']}%)")

    B.append(f"{len(images)} images analysed: radar composites + 24hr accum + MSLP")

    return B


# ── Main ──────────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("agent", exist_ok=True)

    print("Loading warnings...")
    warnings, warnings_gen = load_warnings()
    print(f"  {len(warnings)} active warning(s)")

    print("Loading RFG...")
    rfg = load_rfg()
    print(f"  {rfg['status'][:60]}")

    print("Fetching FGS...")
    fgs_list = fetch_fgs_history(n=4)
    fgs_history_text = fmt_fgs_history(fgs_list)
    fgs_issued = (fgs_list[0].get("issued_at") or fgs_list[0].get("issuedAt", "")) if fgs_list else ""
    print(f"  {len(fgs_list)} FGS statement(s) received" if fgs_list else "  not available")

    print("Loading rain gauge stats...")
    gauge_regional, gauge_top10, gauge_trend, rain_latest = load_rain_gauge_stats()
    print(f"  {'loaded' if gauge_regional else 'not available'}")

    print("Loading soil moisture...")
    cosmos = load_cosmos_moisture()
    print(f"  {'loaded' if cosmos else 'not available'}")

    print("Loading UKV poly forecast data...")
    ukv_poly_text, ukv_run_label = load_ukv_poly_text()
    print(f"  {'loaded' if ukv_poly_text else 'not available'}")

    print("Selecting images...")
    images = select_images()
    print(f"  {len(images)} image(s) selected")

    ukv_meta = load_json("ukv_meta.json", {})

    print("Building prompt...")
    data_block = build_data_block(
        warnings, rfg, fgs_history_text, gauge_regional, gauge_top10, gauge_trend,
        cosmos, ukv_poly_text, rain_latest,
    )

    parsed = None
    if OPENAI_API_KEY:
        print(f"Calling OpenAI ({OPENAI_MODEL})...")
        raw = call_openai(data_block, images)
        if raw:
            parsed = parse_openai_response(raw)
            if parsed:
                print(f"  headline={parsed.get('headline', '')[:60]}")
    else:
        print("OpenAI not configured — writing skeleton output")

    bullets = build_bullets(warnings, rfg, fgs_history_text, gauge_regional, gauge_top10,
                             gauge_trend, cosmos, images, parsed)

    output = {
        "generated_at":    now_utc().strftime("%Y-%m-%dT%H:%M:00Z"),
        "headline":        (parsed or {}).get("headline", ""),
        "summary":         (parsed or {}).get("summary",
                            "Briefing not yet generated — trigger the agent_summary workflow."),
        "context_bullets": bullets,
        "prompt_used":     data_block,
        "images_used":     [{"label": lbl, "url": url} for lbl, url in images],
        "data_age": {
            "warnings_generated_at": warnings_gen,
            "rfg_generated_at":      rfg.get("generated_at", ""),
            "fgs_issued_at":         fgs_issued,
            "rain_latest_time":      rain_latest,
            "ukv_run_label":         ukv_run_label or ukv_meta.get("generated_at", ""),
        },
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Written {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
