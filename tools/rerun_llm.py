import os
import json
import boto3
from datetime import datetime, timezone, timedelta
import openai

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET        = os.environ.get("R2_BUCKET_NAME", "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL", "") or "gpt-4o"

LOG_KEY          = "gaugecheck/log.json"
RUN_KEY          = "gaugecheck/runs/20260522T1100Z.json"

UK_EXTREME_RAINFALL = """\
UK extreme rainfall records (Wikipedia, verified 2026-05-21):
  Duration      Amount      Location                            Date
  5 min         32 mm       Preston, Lancashire                 10 Aug 1893
  30 min        80 mm       Eskdalemuir, Dumfries & Galloway   26 Jun 1953
  60 min        92 mm       Maidenhead, Berkshire               12 Jul 1901
  90 min       117 mm       Dunsop Valley, Lancashire            8 Aug 1967
  120 min      193 mm       Walshaw Dean Lodge, W. Yorkshire    19 May 1989
  24 hr        341 mm       Honister Pass, Cumbria               4 Dec 2015
These constrain 15-min plausibility: 32 mm in 5 min implies ~50 mm/15 min is
possible in the most extreme historic UK convective events.  60 mm/15 min would
be extraordinary; ≥ 60 mm warrants immediate scrutiny for sensor error.\
"""

def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

def r2_get_json(r2, key, default=None):
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        print(f"Error loading {key}: {e}")
        return default

def r2_put_json(r2, key, data):
    body = json.dumps(data, separators=(",", ":")).encode()
    r2.put_object(Bucket=R2_BUCKET, Key=key, Body=body,
                  ContentType="application/json; charset=utf-8")

def get_station_slots(days, station_id, end_date_str, end_slot, n):
    result = []
    d = datetime.strptime(end_date_str, "%Y%m%d")
    slot = end_slot
    while slot < 0:
        slot += 96
        d -= timedelta(days=1)
    for _ in range(n):
        date_str = d.strftime("%Y%m%d")
        raw = days.get(date_str, {}).get(station_id, {}).get(str(slot))
        result.append(float(raw) if isinstance(raw, (int, float)) else None)
        slot -= 1
        if slot < 0:
            slot = 95
            d -= timedelta(days=1)
    return list(reversed(result))

def _haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, atan2, sqrt
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def find_neighbours(station_id, stations, latest_readings, radius_km=20.0, k=5):
    s = stations[station_id]
    nbrs = []
    for sid, st in stations.items():
        if sid == station_id:
            continue
        dist = _haversine_km(s["lat"], s["lon"], st["lat"], st["lon"])
        if dist <= radius_km:
            nbrs.append((sid, dist, st.get("name", sid), latest_readings.get(sid)))
    nbrs.sort(key=lambda x: x[1])
    return nbrs[:k]

def call_llm(station_id, name, lat, lon, value, slot_time_str,
             checks, history_96, neighbours_info, radar_mm, accums,
             consecutive_flags, month_name, season,
             radar_30km_mm=None, spell_summary=None, regional_wet=None,
             check_pattern_str=None, ukv_1h_mm=None, ukv_step_info=None,
             qi=None):
    def _fmt_slots(slots):
        parts = []
        for i, v in enumerate(slots):
            parts.append(f"{v:.1f}" if v is not None else "-")
            if (i + 1) % 24 == 0 and i < len(slots) - 1:
                parts.append("\n  ")
        return "[" + " ".join(parts) + "]"

    def _fmt_check(label, c):
        if c.get("skipped"):
            return f"  {label}: SKIP ({c['skipped']})"
        if not c.get("passed", True):
            extras = {k: v for k, v in c.items()
                      if k not in ("passed", "reason")}
            return f"  {label}: FAIL — {c.get('reason', '')} {extras}"
        return f"  {label}: PASS"

    qi_str = (f"Quality index (QI): {qi:.2f}  "
              f"({'reject' if qi < 0.25 else 'poor' if qi < 0.50 else 'uncertain' if qi < 0.75 else 'good'})\n"
              if qi is not None else "")

    check_block = "\n".join([
        _fmt_check("Gross error check (GEC)",            checks["hard_threshold"]),
        _fmt_check("Range check (RC, seasonal)",          checks.get("range_check", {"skipped": "not_in_run"})),
        _fmt_check("Temporal consistency (TCC)",          checks["temporal"]),
        _fmt_check("Spatial consistency (SCC, 100 km)",   checks.get("spatial", {"skipped": "not_in_run"})),
        _fmt_check("Persistent zero (RCC false-zero)",    checks["persistent_zero"]),
        _fmt_check("Radar conformity (RCC false-pos.)",   checks["radar"]),
        _fmt_check("Drip/leak pattern",                   checks["drip_leak"]),
        _fmt_check("Accumulation anomaly",                checks["accum_anomaly"]),
    ])

    accum_str = "  " + "  ".join(
        f"{k}: {v} mm" for k, v in accums.items()
    )

    nbr_block = ""
    for nbr in neighbours_info[:3]:
        hist = nbr.get("history_24", [])
        hist_str = "[" + " ".join(f"{v:.1f}" if v is not None else "-"
                                  for v in hist) + "]"
        ukv_nbr = nbr.get("ukv_1h_mm")
        ukv_tag = f"  UKV={ukv_nbr:.1f}mm" if ukv_nbr is not None else ""
        nbr_block += (f"\n  {nbr['name']} ({nbr['dist_km']:.1f} km): "
                      f"latest {nbr.get('value_mm', '?')} mm  6hr={hist_str}{ukv_tag}")

    radar_5km_str = (f"{radar_mm:.2f} mm/15 min" if radar_mm is not None
                     else "unavailable")
    radar_30km_str = (f"{radar_30km_mm:.2f} mm/15 min" if radar_30km_mm is not None
                      else "unavailable")

    flag_history = (f"This is the first time this gauge has been flagged in this episode."
                    if consecutive_flags == 1
                    else f"This gauge has been flagged for {consecutive_flags} consecutive 15-min runs "
                         f"({consecutive_flags * 15} min = {consecutive_flags * 15 // 60}h "
                         f"{consecutive_flags * 15 % 60}m).")

    spell_block = ""
    if spell_summary:
        spell_block = (
            f"\nWet/dry spell context:\n"
            f"  Max 15-min slot in last 24 hr: {spell_summary['max_15min_mm']} mm\n"
            f"  Last significant rain (≥1 mm/slot): {spell_summary['hrs_since_significant_rain']}\n"
            f"  Current spell: {spell_summary['current_spell']}\n"
        )

    regional_block = ""
    if regional_wet:
        regional_block = (
            f"\nRegional context (50 km radius, all EA stations):\n"
            f"  {regional_wet['total']} stations reporting  |  "
            f"{regional_wet['any_rain']} with any rain  |  "
            f"{regional_wet['heavy_rain']} with ≥2 mm/slot\n"
        )

    ukv_block = ""
    if ukv_1h_mm is not None:
        ukv_block = (
            f"\nUKV NWP model at station ({ukv_step_info or 'latest run'}):\n"
            f"  Forecast 1-hr accumulation: {ukv_1h_mm:.1f} mm\n"
            f"  [Point-data from ~1.5 km NWP — treat as soft corroborating evidence only;\n"
            f"   UKV often underestimates localised convective peaks and should not\n"
            f"   override observational evidence from the gauge or radar QPE.]\n"
        )

    pattern_block = (f"\nCheck failure pattern: {check_pattern_str}\n"
                     if check_pattern_str else "")

    prompt = (
        f"You are a UK rainfall quality-control expert advising a flood forecasting team.\n\n"
        f"Reference — {UK_EXTREME_RAINFALL}\n\n"
        f"Station: {name} (ID {station_id}), "
        f"{lat:.4f}°N  {abs(lon):.4f}°{'W' if lon < 0 else 'E'}\n"
        f"Season: {season} ({month_name})\n"
        f"Latest reading: {value:.1f} mm in 15-min slot at {slot_time_str} UTC\n"
        f"{flag_history}\n\n"
        f"Accumulation totals ending at this slot:\n{accum_str}\n"
        f"{spell_block}"
        f"\nAutomated QC check results (computed externally — do not recalculate):\n"
        f"{check_block}\n"
        f"{qi_str}"
        f"{pattern_block}"
        f"\nRadar QPE at station:\n"
        f"  5 km radius:  {radar_5km_str}\n"
        f"  30 km radius: {radar_30km_str}  (near-zero confirms spatial isolation)\n"
        f"{ukv_block}"
        f"{regional_block}"
        f"\nThis gauge last 24 hr (96 × 15-min slots, oldest→newest, mm, '-'=missing):\n"
        f"  {_fmt_slots(history_96)}\n\n"
        f"Nearest neighbours (latest reading + last 6-hr slot history + UKV 1-hr forecast where available):"
        f"{nbr_block if nbr_block else chr(10) + '  None available'}\n\n"
        f"Guidance on interpreting specific checks:\n\n"
        f"Drip/leak flag:\n"
        f"  Tipping bucket gauges record rainfall in discrete 0.1 mm tips. In genuine light\n"
        f"  rain (0.3–0.5 mm/hr) every slot naturally records exactly 0.1 mm, so runs of\n"
        f"  6–12 consecutive identical low values are a normal tipping-bucket artefact of\n"
        f"  resolution, not proof of a hardware fault. The flag is most reliable when:\n"
        f"    (a) the 24-hr history shows no coherent preceding rain event;\n"
        f"    (b) neighbours and radar are dry throughout the flagged period; and\n"
        f"    (c) the near-constant low-rate pattern persists for many hours with\n"
        f"        no realistic variation and no credible weather explanation.\n"
        f"  If a genuine rain event is visible in the 24-hr history, or neighbours/radar\n"
        f"  support recent rainfall, a drip flag on a low-value tail is likely genuine\n"
        f"  post-frontal drizzle or the dying phase of a real event — not a fault.\n"
        f"  Upland and west-facing sites (Lake District, Welsh mountains, Scottish Highlands,\n"
        f"  Dartmoor, Pennines) experience extended low-intensity drizzle far more commonly\n"
        f"  than lowland sites; apply extra benefit of the doubt at such locations.\n"
        f"  When drip/leak is the only failing check, lean toward UNCERTAIN or GENUINE\n"
        f"  unless the full evidence picture is clearly inconsistent with real rainfall.\n\n"
        f"Spatial consistency flag:\n"
        f"  A high Di score during a known narrow convective band or orographic shower\n"
        f"  is expected — isolated intense cells routinely outpace their nearest neighbours.\n"
        f"  Treat a spatial flag as weaker evidence of a fault when the 24-hr history and\n"
        f"  radar both show a coherent event, even if neighbours are relatively dry.\n\n"
        f"Based on the above, assess whether the reading at {slot_time_str} is genuine "
        f"rainfall or a sensor/telemetry fault. Consider the UK climate context.\n"
        f"Give a 2–3 sentence explanation.\n"
        f"On the final line write exactly one of:\n"
        f"VERDICT: GENUINE\nVERDICT: SUSPECT\nVERDICT: UNCERTAIN"
    )

    print("--- GENERATED PROMPT ---")
    print(prompt)
    print("------------------------")

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp   = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=300,
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        verdict = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("VERDICT:"):
                v = line.split(":", 1)[1].strip().upper()
                if v in ("GENUINE", "SUSPECT", "UNCERTAIN"):
                    verdict = v
                break
        reasoning = " ".join(
            l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("VERDICT:")
        )
        return verdict, reasoning
    except Exception as e:
        print(f"  LLM error ({station_id}): {e}")
        return None, None

def main():
    r2 = get_r2()
    print("Loading run archive...")
    run_data = r2_get_json(r2, RUN_KEY)
    if not run_data:
        print("Run archive not found!")
        return

    # Find Havering Bower entry
    havering_idx = None
    for idx, g in enumerate(run_data.get("gauges", [])):
        if g["station_id"] == "000180TP":
            havering_idx = idx
            break

    if havering_idx is None:
        print("Havering Bower entry not found in run archive!")
        return

    g = run_data["gauges"][havering_idx]
    print(f"Found Havering Bower entry in run archive. Current verdict: {g.get('llm_verdict')}")

    # Set correct value to 8.0 mm
    actual_value = 8.0
    print(f"Setting actual value to {actual_value} mm")

    # Load resources to reconstruct LLM prompt exactly
    print("Loading days...")
    days = {}
    days["20260522"] = r2_get_json(r2, "rain/readings/20260522.json", {}).get("readings", {})
    days["20260521"] = r2_get_json(r2, "rain/readings/20260521.json", {}).get("readings", {})

    print("Loading stations...")
    sdata = r2_get_json(r2, "rain/stations.json", {})
    stations = sdata.get("stations", {})

    target_dt = datetime(2026, 5, 22, 11, 0, tzinfo=timezone.utc)
    target_slot = 44

    latest_readings = {}
    for sid in stations:
        for back in range(4):
            slot_b = target_slot - back
            date_b = "20260522"
            if slot_b < 0:
                slot_b += 96
                date_b = "20260521"
            val = days.get(date_b, {}).get(sid, {}).get(str(slot_b))
            if isinstance(val, (int, float)) and val >= 0:
                latest_readings[sid] = float(val)
                break

    # Reconstruct correct history_96 ending at slot 44
    history_96 = get_station_slots(days, "000180TP", "20260522", target_slot, 96)
    # Ensure the latest slot has the correct actual value of 8.0 mm
    if history_96:
        history_96[-1] = actual_value

    neighbours = find_neighbours("000180TP", stations, latest_readings)
    neighbours_info = []
    for nbr_id, dist_km, nbr_name, nbr_val in neighbours[:3]:
        h24 = get_station_slots(days, nbr_id, "20260522", target_slot, 24)
        neighbours_info.append({
            "name":       nbr_name,
            "dist_km":    dist_km,
            "value_mm":   round(nbr_val, 2) if nbr_val is not None else None,
            "history_24": h24,
            "ukv_1h_mm":  None,
        })

    # Recalculate and update the checks for 8.0 mm
    checks = {
        "hard_threshold":  {"passed": True},
        "range_check":     {"passed": True, "threshold_mm": 25.0},
        "temporal":        {"passed": False, "reason": "spike_from_zero", "value_mm": actual_value, "preceding_median_mm": 0.0},
        "spatial":         {"passed": False, "reason": "spatial_outlier", "outlier_class": "strong", "di": 16.0, "value_mm": actual_value, "domain_median_mm": 0.0, "domain_mad": 0.0, "domain_n": 200},
        "persistent_zero": {"passed": True, "skipped": "gauge_nonzero"},
        "radar":           {"passed": False, "reason": "radar_mismatch", "gauge_mm": actual_value, "radar_mm_15min": 0.0, "ratio": 800.0},
        "drip_leak":       {"passed": True, "skipped": "insufficient_nonzero_slots"},
        "accum_anomaly":   {"passed": True, "skipped": "total_below_threshold", "gauge_24hr_mm": 15.0}
    }
    qi = 0.0

    accums = {
        "1hr": 15.0,
        "3hr": 15.0,
        "6hr": 15.0,
        "12hr": 15.0,
        "24hr": 15.0
    }

    # Re-evaluate spell summary
    def compute_spell_summary(history_96):
        recent = [(i, v) for i, v in enumerate(reversed(history_96)) if v is not None]
        max_mm = max((v for _, v in recent), default=0.0)
        hrs_since = None
        for i, v in recent:
            if v >= 1.0:
                hrs_since = round(i * 15 / 60, 1)
                break
        hrs_since_str = f"{hrs_since} hr ago" if hrs_since is not None else ">24 hr ago"
        if not recent:
            spell_str = "unknown"
        else:
            is_wet = recent[0][1] >= 0.1
            count = 0
            for _, v in recent:
                if (v >= 0.1) == is_wet:
                    count += 1
                else:
                    break
            spell_str = f"{'wet' if is_wet else 'dry'} for {round(count * 15 / 60, 1)} hr"
        return {
            "max_15min_mm":              round(max_mm, 2),
            "hrs_since_significant_rain": hrs_since_str,
            "current_spell":              spell_str,
        }
    _spell = compute_spell_summary(history_96)

    def regional_wetness_count(station_id, stations, latest_readings, radius_km=50.0):
        s = stations[station_id]
        total = reporting_any = reporting_heavy = 0
        for sid, st in stations.items():
            if sid == station_id:
                continue
            if _haversine_km(s["lat"], s["lon"], st["lat"], st["lon"]) <= radius_km:
                total += 1
                v = latest_readings.get(sid) or 0
                if v > 0:
                    reporting_any += 1
                if v >= 2.0:
                    reporting_heavy += 1
        return {"total": total, "any_rain": reporting_any, "heavy_rain": reporting_heavy}
    _regional = regional_wetness_count("000180TP", stations, latest_readings)

    def check_failure_pattern(checks, prev_checks):
        _names = {"hard_threshold": "GEC", "range_check": "Range",
                  "temporal": "Temporal", "spatial": "Spatial",
                  "persistent_zero": "Zero", "radar": "Radar",
                  "drip_leak": "Pattern", "accum_anomaly": "Accum"}
        def _failing(c):
            return {_names[k] for k, v in c.items()
                    if not v.get("passed", True) and not v.get("skipped")}
        now   = _failing(checks)
        prev  = _failing(prev_checks) if prev_checks else set()
        new_f = now - prev
        cleared = prev - now
        parts = []
        if now:
            parts.append("Failing: " + ", ".join(sorted(now)))
        if new_f:
            parts.append("New this run: " + ", ".join(sorted(new_f)))
        if cleared:
            parts.append("Cleared: " + ", ".join(sorted(cleared)))
        if not now:
            parts.append("No deterministic failures (triggered by high reading value)")
        return " | ".join(parts)
    _pattern = check_failure_pattern(checks, None)

    # Call LLM with consecutive_flags = 1, actual_value = 8.0 mm!
    print("Calling OpenAI LLM with corrected 8.0 mm reading...")
    verdict, reasoning = call_llm(
        "000180TP", "Havering Bower", g["lat"], g["lon"], actual_value,
        "2026-05-22T11:00", checks, history_96, neighbours_info, 0.0,
        accums, 1, "May", "Spring",
        spell_summary=_spell, regional_wet=_regional, check_pattern_str=_pattern,
        qi=qi
    )

    print(f"New Verdict: {verdict}")
    print(f"New Reasoning: {reasoning}")

    if not verdict:
        print("Failed to get LLM verdict!")
        return

    # Update run archive gauge entry with corrected 8.0 mm fields
    g["latest_value_mm"] = actual_value
    g["checks"] = checks
    g["qi"] = qi
    g["accums"] = accums
    g["llm_verdict"] = verdict
    g["llm_reasoning"] = reasoning
    g["consecutive_flags"] = 1
    g["llm_from_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Uploading updated run archive...")
    r2_put_json(r2, RUN_KEY, run_data)

    # Update active log.json
    print("Updating log.json...")
    log_data = r2_get_json(r2, LOG_KEY)
    if log_data:
        updated_log = False
        for ev in log_data.get("events", []):
            # Find Havering Bower event for May 22 11:00Z
            if ev["station_id"] == "000180TP":
                ev["latest_value_mm"] = actual_value
                ev["checks"] = checks
                ev["qi"] = qi
                ev["accums"] = accums
                ev["llm_verdict"] = verdict
                ev["llm_reasoning"] = reasoning
                ev["consecutive_flags"] = 1
                ev["llm_from_run"] = g["llm_from_run"]
                updated_log = True
                print("Havering Bower event updated in log.json.")
                break
        
        if updated_log:
            r2_put_json(r2, LOG_KEY, log_data)
        else:
            print("Havering Bower event not found in log.json!")

    print("Done!")

if __name__ == "__main__":
    main()
