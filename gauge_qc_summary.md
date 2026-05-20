# Gauge QC — how it works

## Seven deterministic checks

Run every 15 minutes across ~1,000 Environment Agency tipping-bucket stations.

| Check | What it detects | Triggers when |
|---|---|---|
| **Threshold** | Physically impossible reading | ≥25 mm (suspect) or ≥50 mm (impossible) in a single 15-min slot |
| **Spike** | Isolated burst vs. recent history | Reading ≥8× the median of the preceding 7 slots (1h 45m), AND ≥5 mm absolute |
| **Spatial** | High reading with no neighbours confirming | Reading ≥8 mm, but all stations within 20 km report <2 mm |
| **Zero** | Gauge stuck while others are wet | Zero for ≥80% of the last 6 hrs, while ≥3 neighbours are active AND radar confirms rainfall (≥2 mm) |
| **Radar** | Gauge far exceeds radar estimate | Reading ≥5 mm, radar QPE at that location <0.5 mm, and gauge/radar ratio >15× |
| **Drip** | Leaking or blocked tipping bucket | ≥8 non-zero slots in 3 hrs with mean <1.5 mm and near-zero variation (CV <0.25). Suppressed if neighbours are also consistently wet (≥0.15 mm/slot mean) — protects against genuine widespread drizzle. Stuck counter (CV=0) always flags regardless. |
| **Accum** | Systematic over-recording over 24 hrs | 24hr total ≥5× the mean of ≥3 nearby neighbours (each with ≥5 mm total, neighbour mean ≥3 mm) |

A station with **1 check failed** → **Elevated** (orange on map).  
A station with **2+ checks failed** → **Flagged** (red on map).

---

## LLM assessment (OpenAI)

### When it runs
- Any Elevated or Flagged station (≥1 check failed)
- Any reading ≥20 mm even if all checks pass (high-value override)
- Re-runs every 8 consecutive flags (~2 hrs) to refresh stale verdicts during long events
- Skipped on intermediate runs between refreshes to keep API costs low

### What it receives

```
Season and month
Latest reading and how long the gauge has been continuously flagged
Accumulation totals: 1hr / 3hr / 6hr / 12hr / 24hr
All 7 check results with full detail (ratios, slot counts, CV values, neighbour data)
Radar QPE at the station location
Full 24-hr slot history (96 × 15-min values, oldest→newest)
3 nearest neighbours: latest reading + last 6-hr slot history (24 × 15-min values)
```

### Output
One of **Genuine**, **Uncertain**, or **Suspect**, plus a 2–3 sentence explanation shown in the station popup on the QC map.

---

## Data flow

```
EA API (15-min telemetry)
    → rain/readings/YYYYMMDD.json  (R2)
    → gauge_qc.py  (triggered every 10 min via Google Apps Script → workflow_dispatch)
        → gaugecheck/results.json   current snapshot (maps + popups)
        → gaugecheck/runs/{ts}.json timestamped archive (timeline slider)
        → gaugecheck/manifest.json  ordered list of run timestamps
        → gaugecheck/log.json       rolling 48-hr event log (right panel)
```

## Key thresholds (at a glance)

| Parameter | Value |
|---|---|
| Hard suspect limit | 25 mm / 15 min |
| Hard impossible limit | 50 mm / 15 min |
| Temporal spike ratio | 8× preceding median |
| Temporal min absolute | 5 mm |
| NN radius | 20 km |
| NN trigger | 8 mm gauge, all neighbours <2 mm |
| Persistent zero window | 6 hrs (24 slots) |
| Radar ratio limit | 15× (only when radar <0.5 mm) |
| Drip window | 3 hrs (12 slots), mean <1.5 mm, CV <0.25 |
| Drip neighbour suppression | neighbours mean ≥0.15 mm/slot |
| Accum anomaly ratio | 5× neighbour mean over 24 hrs |
| LLM trigger (high value) | ≥20 mm |
| LLM refresh interval | every 8 runs (~2 hrs) |
| Telemetry lookback | 3 slots (45 min) for latency tolerance |
| Log retention | 48 hrs |
| Run archive | 7 days (672 runs) |
