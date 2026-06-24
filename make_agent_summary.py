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


def _r2_client():
    """Return a boto3 S3 client pointed at R2, or None if credentials missing."""
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET]):
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )
    except Exception as e:
        print(f"  R2 client init: {e}")
        return None


def r2_get_json(key):
    """Fetch a JSON object directly from R2 using authenticated boto3 access."""
    client = _r2_client()
    if client is None:
        return None
    try:
        resp = client.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(resp["Body"].read())
    except Exception as e:
        print(f"  R2 {key}: {e}")
        return None


def r2_put_json(key, obj):
    """Upload a JSON object to R2. Returns True on success."""
    client = _r2_client()
    if client is None:
        return False
    try:
        body = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        client.put_object(Bucket=R2_BUCKET, Key=key, Body=body, ContentType="application/json")
        return True
    except Exception as e:
        print(f"  R2 upload {key}: {e}")
        return False


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
    """Return list of up to n recent FGS statements (newest first).

    Tries /statements then /fgs. 404 means no active FGS (normal in summer).
    """
    for path in ("statements", "fgs"):
        req = urllib.request.Request(
            f"{FGS_API_BASE}/{path}",
            headers={"x-api-key": FFC_API_KEY},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            print(f"  FGS HTTP {e.code} on /{path}: {e}")
            return []
        except Exception as e:
            print(f"  FGS /{path}: {e}")
            return []
    else:
        print("  FGS: no active statement (404 — normal in low-risk periods)")
        return []
    # Unwrap if wrapped in an object e.g. {"statements": [...]}
    if isinstance(data, dict):
        for key in ("statements", "fgs", "results", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if isinstance(data, list):
        return data[:n]
    return []


def fmt_fgs(fgs_data):
    if not fgs_data:
        return "FGS data not available."
    lines = []

    # Published date
    pf = fgs_data.get("public_forecast") or {}
    published = pf.get("published_at") or fgs_data.get("issued_at") or fgs_data.get("issuedAt") or ""
    if published:
        lines.append(f"Published: {published[:19].replace('T', ' ')} UTC")

    # England / Wales outlook text
    eng = pf.get("england_forecast") or pf.get("english_forecast") or ""
    wal = pf.get("wales_forecast_english") or ""
    if eng:
        lines.append(f"England: {eng.strip()}")
    if wal:
        lines.append(f"Wales: {wal.strip()}")

    # 5-day flood risk trend
    trend = fgs_data.get("flood_risk_trend") or {}
    if trend:
        trend_str = "  ".join(
            f"D{i+1}:{trend.get(f'day{i+1}', '?')}"
            for i in range(5)
        )
        lines.append(f"5-day trend: {trend_str}")

    # Fallback to per-area format if present
    if not lines:
        areas = (fgs_data.get("areas") or fgs_data.get("guidance_areas") or
                 fgs_data.get("floodAreas") or [])
        if isinstance(areas, list):
            for area in areas[:12]:
                name = (area.get("area_name") or area.get("name") or "Unknown")
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
    current_slot   = (n.hour * 60 + n.minute) // 15

    def fetch_day(date_str):
        key  = f"rain/readings/{date_str}.json"
        data = r2_get_json(key)
        if data is None:
            data = http_get_json(f"{r2_base}/rain/readings/{date_str}.json")
        return (data or {}).get("readings", {})

    print("  Gauge: fetching readings...")
    # days[0] = today, [1] = yesterday, [2] = day before — covers a 48h window.
    days = [fetch_day((n - timedelta(days=d)).strftime("%Y%m%d")) for d in range(3)]
    if not days[0] and not days[1]:
        return None, None, None, meta.get("latest_time", "")

    def slot_val(day_dict, sid, idx):
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
                idx, d = last - i, 0
                while idx < 0:        # walk back into earlier days, 96 slots each
                    idx += 96
                    d += 1
                if d < len(days):
                    total += slot_val(days[d], sid, idx)
            return total

        a24 = acc(96)
        a48 = acc(192)
        if a48 < 0.1:
            continue
        station_stats.append({
            "name": name, "region": region,
            "acc_1hr":  round(acc(4),  1),
            "acc_6hr":  round(acc(24), 1),
            "acc_24hr": round(a24,     1),
            "acc_48hr": round(a48,     1),
        })

    if not station_stats:
        return None, None, None, meta.get("latest_time", "")

    by_region = defaultdict(list)
    for s in station_stats:
        by_region[s["region"]].append(s)

    regional = {}
    for reg, stns in sorted(by_region.items()):
        v24 = [s["acc_24hr"] for s in stns]
        v48 = [s["acc_48hr"] for s in stns]
        v1  = [s["acc_1hr"]  for s in stns]
        regional[reg] = {
            "max_24hr":      round(max(v24), 1),
            "mean_24hr":     round(sum(v24) / len(v24), 1),
            "max_48hr":      round(max(v48), 1),
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

# Government regions outside England & Wales — excluded from the E&W-focused tables.
_NON_EW_REGIONS = {"scotland", "northern ireland", "ni"}


def _fmt_ddhhmm(label):
    """Convert a UKV time label to compact 'DD/HHMM GMT' form.

    Accepts human labels ('24 Jun 2026 03:00 GMT') or run_ts ('20260624T0300Z').
    Falls back to the original string if it cannot be parsed.
    """
    if not label:
        return label
    s = label.strip()
    for fmt in ("%d %b %Y %H:%M GMT", "%d %b %Y %H:%M UTC", "%Y%m%dT%H%MZ"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%H%M GMT")
        except ValueError:
            continue
    return label


def _run_window_regions(run, target_valid_label):
    """Return the accum_24h region dict for the step in `run` valid at
    `target_valid_label`, or None. Used for run-to-run trend comparison —
    the rolling-24h window ending at a fixed valid time is identical across
    runs, so only the forecast lead differs.
    """
    run_ts = run.get("run_ts", "")
    for s in run.get("steps", []):
        if s.get("valid_label") == target_valid_label:
            offset_str = s.get("offset", "")
            poly = r2_get_json(f"ukv_poly/{run_ts}/{offset_str}.json") if offset_str else None
            if poly:
                return (poly.get("regions") or {}).get("accum_24h", {})
            return None
    return None


def _top_polys(layer_dict, accum_key, limit, ew_only=False):
    """Return sorted [(name, mm), ...] for a poly layer/period, dropping None and zeros."""
    period = (layer_dict or {}).get(accum_key, {})
    rows = []
    for name, mm in period.items():
        if mm is None:
            continue
        if ew_only and any(x in name.lower() for x in _NON_EW_REGIONS):
            continue
        try:
            v = float(mm)
        except (TypeError, ValueError):
            continue
        if v < 0.05:
            continue
        rows.append((name, v))
    rows.sort(key=lambda x: -x[1])
    return rows[:limit]


def load_ukv_poly_text():
    """Fetch UKV poly accumulations from R2 for several forecast windows.

    Reports the wettest England & Wales regions (plus catchments and counties)
    for the next 6h / 12h / day-1 (T+0→24) / day-2 (T+24→48) windows, then a
    short run-to-run trend: how the latest few UKV runs' forecast for a fixed
    24h-ending-valid-time window has changed (wetter / drier / steady).
    """
    meta = load_json("ukv_meta.json", {})
    runs = meta.get("runs", [])
    if not runs:
        return None, None
    run       = runs[0]
    run_ts    = run.get("run_ts", "")
    run_label = run.get("run_label", run_ts)
    run_ddhhmm = _fmt_ddhhmm(run_label)
    steps_by_h = {s.get("offset_hours"): s for s in run.get("steps", [])}

    lines = [
        f"UKV model — run {run_ddhhmm} (issued {run_label}). Values are forecast "
        f"rainfall accumulated over the stated window, mm, area-mean per polygon. "
        f"In prose, attribute forecasts as 'UKV (run {run_ddhhmm})' and give valid "
        f"times as DD/HHMM GMT."
    ]
    # (label, accum_key, step_offset_h, window_desc). Day 2 uses the rolling
    # accum_24h at T+48 — i.e. the T+24→T+48 window — since steps carry no
    # accum_48h field.
    windows = [
        ("Next 6h",         "accum_6h",  6,  "T+0 → T+6"),
        ("Next 12h",        "accum_12h", 12, "T+0 → T+12"),
        ("Day 1 (next 24h)", "accum_24h", 24, "T+0 → T+24"),
        ("Day 2",           "accum_24h", 48, "T+24 → T+48"),
    ]
    got_any = False
    day1_top = None          # wettest E&W regions for T+0→24 (focus for trend)
    day1_valid_label = None
    for label, accum_key, offset_h, window_desc in windows:
        step = steps_by_h.get(offset_h)
        if not step:
            continue
        valid_label = step.get("valid_label", f"T+{offset_h}h")
        offset_str = step.get("offset", "")
        poly_data = r2_get_json(f"ukv_poly/{run_ts}/{offset_str}.json") if offset_str else None
        if not poly_data:
            continue

        regions = _top_polys(poly_data.get("regions"), accum_key, 12, ew_only=True)
        if not regions:
            continue
        got_any = True
        if offset_h == 24:
            day1_top = regions
            day1_valid_label = valid_label
        lines.append(f"\n  {label} ({window_desc}, valid by {_fmt_ddhhmm(valid_label)}) — wettest regions, England & Wales:")
        for name, mm in regions:
            lines.append(f"    {name}: {mm:.1f} mm")

        # Catchments at every window (flood-relevant unit for decision-making)
        catch_limit = 10 if offset_h == 24 else 8
        catch = _top_polys(poly_data.get("catchments"), accum_key, catch_limit)
        if catch:
            lines.append(f"\n  {label} — wettest river catchments:")
            for name, mm in catch:
                lines.append(f"    {name}: {mm:.1f} mm")

        # County / unitary detail on the two daily windows
        if offset_h in (24, 48):
            counties = _top_polys(poly_data.get("counties"), accum_key, 8)
            if counties:
                lines.append(f"\n  {label} — wettest counties / unitary areas:")
                for name, mm in counties:
                    lines.append(f"    {name}: {mm:.1f} mm")

    # Run-to-run trend for the wettest day-1 region: same 24h-ending-valid-time
    # window across the latest few runs shows whether the model is firming up.
    if day1_top and day1_valid_label:
        focus_region, focus_val = day1_top[0]
        series = [(run_ddhhmm, focus_val)]
        for prev in runs[1:4]:
            regs = _run_window_regions(prev, day1_valid_label)
            if not regs:
                continue
            pv = regs.get(focus_region)
            if pv is None:
                continue
            try:
                series.append((_fmt_ddhhmm(prev.get("run_label", "")), float(pv)))
            except (TypeError, ValueError):
                continue
        if len(series) >= 2 and focus_val >= 1.0:
            oldest = series[-1][1]
            if focus_val > oldest * 1.15:
                direction = "trending wetter"
            elif focus_val < oldest * 0.85:
                direction = "trending drier"
            else:
                direction = "steady run-to-run"
            chain = " → ".join(f"{lbl}: {v:.1f}mm" for lbl, v in series)
            lines.append(
                f"\n  Run-to-run consistency — {focus_region}, 24h to "
                f"{_fmt_ddhhmm(day1_valid_label)} (newest run first): {chain} ({direction})."
            )

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
    """Select images for the model. EVERY image must carry the CartoDB basemap +
    region boundaries so the model can geolocate features. Raw overlay-only PNGs
    (no basemap) are never used.
    """
    images = []
    n = now_utc()

    # ── Radar rate composites — recent movement (last 6h, all basemap) ───────────
    # Only the ~24 most recent radar frames have a dashboard composite (basemap);
    # older frames have only the raw overlay, so we stay inside that window.
    radar = load_json("frames_parallel.json", [])
    if radar:
        offsets_min = [0, 120, 240, 360]
        labels      = ["most recent", "~2hr ago", "~4hr ago", "~6hr ago"]
        seen = set()
        for offset, lbl in zip(offsets_min, labels):
            f = pick_nearest(radar, n - timedelta(minutes=offset))
            if not f:
                continue
            url = f.get("dashboard_radar_url")  # basemap composite only
            if url and url not in seen:
                seen.add(url)
                images.append((f"Radar rainfall-rate composite — {f['time']} ({lbl})", url))

    # ── Rolling 24h accumulation — build-up & trend (latest + ~12h earlier) ──────
    accum_hourly = load_json("frames_accum_hourly.json", [])
    if len(accum_hourly) >= 2:
        latest = accum_hourly[-1]
        earlier = accum_hourly[max(0, len(accum_hourly) - 13)]  # ~12h before latest
        images.append((
            f"Rolling 24h rainfall accumulation ending {latest['time']} (current footprint)",
            latest["url"],
        ))
        if earlier["url"] != latest["url"]:
            images.append((
                f"Rolling 24h rainfall accumulation ending {earlier['time']} (~12h earlier, for trend)",
                earlier["url"],
            ))
    else:
        accum_daily = load_json("frames_accum_daily.json", [])
        if accum_daily:
            images.append((
                f"24h accumulated rainfall today — ending {n.strftime('%H:%M UTC %d %b %Y')}",
                accum_daily[0]["url"],
            ))

    # ── MSLP analysis — synoptic pattern & evolution (latest + ~12h earlier) ─────
    mslp = load_json("frames_mslp.json", [])
    if len(mslp) >= 2:
        latest_m = mslp[-1]
        earlier_m = mslp[max(0, len(mslp) - 13)]
        images.append((f"MSLP analysis — {latest_m['time']} (latest)", latest_m["mslp"]))
        if earlier_m["mslp"] != latest_m["mslp"]:
            images.append((f"MSLP analysis — {earlier_m['time']} (~12h earlier, for evolution)", earlier_m["mslp"]))
    elif mslp:
        images.append((f"MSLP analysis — {mslp[-1]['time']}", mslp[-1]["mslp"]))

    return images


# ── System prompt ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert UK operational meteorologist and hydrologist writing a concise flood-risk briefing for emergency planning and Flood Forecasting Centre practitioners. Your focus is England and Wales at regional scale, dropping to catchment or county scale only where the data clearly justifies singling out a smaller area.

You are given (a) structured observational and forecast data and (b) a set of map images. EVERY image is rendered on the SAME geographic basemap with national/regional boundary lines and coastlines drawn on top, so you can geolocate every feature precisely — name the actual regions, coasts and uplands the rainfall sits over rather than describing abstract positions. The image set, in order, is:
  • Four radar rainfall-RATE composites (most recent, ~2h, ~4h, ~6h ago) — read these as a short loop to judge where rain is now and which way systems are tracking.
  • Two ROLLING 24-HOUR ACCUMULATION maps (current footprint and one ~12h earlier) — compare them to see where totals are building up and whether the wet footprint is expanding, shifting or decaying.
  • Two MSLP analysis charts (latest and ~12h earlier) — use isobar spacing and frontal positions to explain the synoptic driver and its evolution.

Method — reason across sources before writing:
  1. Cross-check the radar/accumulation imagery against the rain-gauge table; trust gauge mm values for magnitude and imagery for spatial pattern and motion.
  2. Weigh forecast rainfall (UKV tables + MSLP) against antecedent soil moisture and any rainfall already banked in the last 24h — the flood risk is highest where fresh heavy rain falls on already-wet ground or saturated catchments.
  3. Reconcile the official guidance (Met Office warnings, RFG, FGS trend) with what the data shows; flag clearly if observations look more or less severe than the official line.

Respond with a JSON object inside <json> tags with exactly these fields:
- "headline": One sharp sentence (max 20 words) capturing the single most decision-relevant point now or in the outlook.
- "summary": Exactly three paragraphs separated by a double newline (\\n\\n). Each paragraph is 3–4 sentences. The three-paragraph structure is MANDATORY regardless of how quiet or active conditions are — do not merge or omit paragraphs:
    Para 1 — Observations (last 24h): where the heaviest rain fell and peak gauge point-totals with region names; what the radar loop and accumulation maps show about spatial pattern and recent trend; any notable intensification, easing, or spatial shift.
    Para 2 — Hazards & guidance: active warnings (naming any storm) and RFG/FGS status with the 5-day flood-risk trend; key gauge threshold exceedances; antecedent state from the 48h gauge totals and COSMOS soil moisture, and what it means for runoff and catchment response.
    Para 3 — Forecast (next 6–48h): expected day-1 and day-2 totals by region and key catchments from UKV (these are spatial area-means, not point totals), attributed to the model run; synoptic context from the MSLP charts and whether the latest UKV run is firming up or easing versus prior runs; timing and location of peak risk linked to current catchment wetness.

Rainfall significance thresholds:
  10 mm/1h heavy | 30 mm/1h extreme (flash-flood risk)
  25 mm/3h significant | 40 mm/3h very significant
  50 mm/6h or 100 mm/24h exceptional

Style: professional, factual, third-person prose; no bullet points inside the summary; specific place names and mm values, not vague adjectives. Keep gauge values clearly identified as point totals (single-site measurements); keep UKV values clearly identified as spatial area-means over the named polygon — never conflate the two. When citing forecast rainfall, attribute it to the model and its run as "UKV (run DD/HHMM GMT)", and give all forecast valid times in the same DD/HHMM GMT form (e.g. "by 25/0300 GMT"); use UTC for observation times. Do not invent data — if a source is missing or quiet, say so briefly and move on. Be calm and proportionate: in benign conditions say so plainly rather than manufacturing alarm."""


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
    L.append("=== OBSERVED RAINFALL — RAIN GAUGE NETWORK (point totals: single-site spot measurements, mm) ===")
    if rain_latest_time:
        L.append(f"Latest data: {rain_latest_time}")
    if gauge_regional:
        L.append("")
        L.append(f"{'Region':<22} {'Max 24hr':>9} {'Mean 24hr':>10} {'Max 48hr':>9} {'>10mm/1hr':>10} {'>30mm/1hr':>10}")
        L.append("-" * 75)
        for reg, s in sorted(gauge_regional.items(), key=lambda x: -x[1]["max_24hr"]):
            L.append(
                f"{reg:<22} {s['max_24hr']:>8.1f}mm {s['mean_24hr']:>9.1f}mm {s.get('max_48hr', 0):>8.1f}mm"
                f" {s['over_10mm_1hr']:>10} {s['over_30mm_1hr']:>10}"
            )
        L.append("(48hr = antecedent window: rain already banked on the ground)")
        L.append("")
        if gauge_top10:
            L.append("Top 10 stations by 24hr accumulation:")
            for st in gauge_top10:
                L.append(f"  {st['name']} ({st['region']}): "
                         f"1hr={st['acc_1hr']}mm  6hr={st['acc_6hr']}mm  "
                         f"24hr={st['acc_24hr']}mm  48hr={st.get('acc_48hr', '?')}mm")
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
    L.append("=== UKV NWP FORECAST — POLYGON ACCUMULATIONS (spatial area-means, NOT point totals; England & Wales focus) ===")
    L.append(ukv_poly_text or "UKV numerical data not available.")
    L.append("")

    # Image context
    L.append("=== IMAGERY (attached below — all share one basemap with boundaries) ===")
    L.append("  4 radar rainfall-rate composites — most recent, ~2h, ~4h, ~6h ago (motion/loop)")
    L.append("  2 rolling 24h accumulation maps — current footprint and ~12h earlier (build-up/trend)")
    L.append("  2 MSLP analysis charts — latest and ~12h earlier (synoptic driver/evolution)")

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

    B.append(f"{len(images)} basemap images analysed: radar loop + 24h accumulation (×2) + MSLP (×2)")

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

    # Full prompt shown on the front end: system instructions + data + image manifest.
    image_manifest = "\n".join(f"  {i+1}. {lbl}" for i, (lbl, _u) in enumerate(images))
    full_prompt = (
        "######## SYSTEM PROMPT ########\n"
        f"{SYSTEM_PROMPT}\n\n"
        "######## DATA (user message) ########\n"
        f"{data_block}\n\n"
        "######## IMAGES ATTACHED (in order) ########\n"
        f"{image_manifest}"
    )

    output = {
        "generated_at":    now_utc().strftime("%Y-%m-%dT%H:%M:00Z"),
        "headline":        (parsed or {}).get("headline", ""),
        "summary":         (parsed or {}).get("summary",
                            "Briefing not yet generated — trigger the agent_summary workflow."),
        "context_bullets": bullets,
        "prompt_used":     full_prompt,
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

    # Upload slim copy (without large prompt_used field) to R2.
    # Save both a rolling latest and a dated archive for 2-week history.
    # Set a lifecycle rule on the agent/summaries/ prefix in the R2 console
    # (expire after 14 days) to keep storage bounded.
    slim = {k: v for k, v in output.items() if k != "prompt_used"}
    ts_tag = now_utc().strftime("%Y%m%d_%H%M")
    if r2_put_json("agent/summary.json", slim):
        print("  Uploaded agent/summary.json to R2 (rolling latest)")
    if r2_put_json(f"agent/summaries/{ts_tag}.json", slim):
        print(f"  Uploaded agent/summaries/{ts_tag}.json to R2 (archive)")


if __name__ == "__main__":
    main()
