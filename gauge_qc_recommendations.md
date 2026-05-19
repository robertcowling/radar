# Gauge Quality Control Process Analysis and Recommendations

## 1. Overview of the Current Process
The current gauge quality control (QC) process, implemented in `gauge_qc.py`, operates on 15-minute telemetry data from rain gauges. It flags anomalous readings using seven deterministic checks and an LLM-based secondary evaluation for high-value or multi-failed readings.

The seven checks are:
1. **Hard Threshold:** Flags readings ≥ 25 mm as suspect, and ≥ 50 mm as impossible within a 15-minute slot.
2. **Temporal Spike:** Flags isolated spikes (ratio ≥ 8.0) against the median of the preceding 7 slots (1h45m).
3. **Nearest Neighbour:** Flags readings > 8.0 mm if all neighbours within 20 km are reporting < 2.0 mm.
4. **Persistent Zero:** Flags gauges reporting < 0.1 mm for 80% of a 6-hour window while ≥ 3 neighbours are active (> 3.0 mm) and radar confirms rainfall (≥ 2.0 mm).
5. **Radar Conformity:** Flags gauges > 5.0 mm if the ratio to radar QPE exceeds 15.0 and radar estimates < 0.5 mm.
6. **Drip/Leak Pattern:** Detects suspiciously uniform small readings over a 3-hour period (low coefficient of variation or stuck counters).
7. **Accumulation Anomaly:** Flags 24-hour accumulations that are > 3.5x higher than the mean of at least 3 nearby gauges.

Readings failing ≥ 2 checks, or those ≥ 20 mm, trigger an LLM prompt containing the check results, nearest neighbour data, and recent gauge history. The LLM acts as an independent assessor, outputting GENUINE, SUSPECT, or UNCERTAIN.

---

## 2. Analysis and Recommendations for Deterministic Checks

### Check 1: Hard Threshold
- **Current:** Global thresholds of 25 mm (suspect) and 50 mm (impossible) per 15-minute slot.
- **Recommendations:**
  - **Climatological/Regional Thresholds:** Hard limits are too rigid for diverse topographies. A 25 mm/15min reading might be impossible in eastern England in winter but plausible in a summer thunderstorm or in high-elevation regions like Cumbria or Wales.
  - **Recommendation:** Implement station-specific or region-specific Return Period limits based on historical climatology (e.g., UK Centre for Ecology & Hydrology FEH statistics).

### Check 2: Temporal Spike
- **Current:** Compares the current reading to the median of the previous 7 slots. Flags if the ratio ≥ 8.0 and value ≥ 5.0 mm.
- **Recommendations:**
  - **Dynamic Context Window:** Short-duration convective storms can legitimately cause massive temporal spikes from zero. A 7-slot median is robust but might over-penalize sharp, genuine convective cells.
  - **Recommendation:** Integrate radar trend data. If radar shows a rapidly developing cell (high reflectivity gradient) over the station, increase the acceptable spike ratio.

### Check 3: Nearest Neighbour
- **Current:** 20 km search radius, needs 2 neighbours. Flags if gauge > 8 mm and all neighbours < 2 mm.
- **Recommendations:**
  - **Topographic Constraints:** A 20 km radius circle in mountainous terrain spans vastly different microclimates (e.g., windward vs. leeward slopes).
  - **Recommendation:** Replace fixed radius with an elevation/catchment-aware grouping or reduce radius in high-relief areas. Also, weight neighbours by inverse distance and elevation difference rather than a simple boolean (< 2 mm).

### Check 4: Persistent Zero (Blockage)
- **Current:** Looks back 6 hours. Needs 3+ active neighbours and radar > 2 mm.
- **Recommendations:**
  - **Sensitivity to Freezing Conditions:** During snow or freezing rain, unheated tipping buckets will legitimately read zero despite radar returns (snow isn't tipping the bucket).
  - **Recommendation:** Incorporate surface temperature data. Suppress the persistent zero flag if the local temperature is near or below freezing, as the gauge is likely snow-capped rather than blocked by debris.

### Check 5: Radar Conformity
- **Current:** Flags if gauge > 5 mm, ratio > 15.0, and radar < 0.5 mm.
- **Recommendations:**
  - **Radar Beam Overshoot/Undershoot:** Radar struggles at long ranges (overshooting clouds) or in complex terrain (beam blockage, bright band). A fixed ratio of 15.0 might inappropriately flag genuine orographic drizzle that the radar misses.
  - **Recommendation:** Add a quality index for the radar pixel (e.g., range from radar site, known beam blockage). Relax the ratio limit for stations where radar QPE is known to be historically poor.

### Check 6: Drip/Leak Pattern
- **Current:** Looks at 3 hours of data, flags low variance (CV < 0.25) or long identical runs (stuck counter).
- **Recommendations:**
  - **Excellent Concept, Needs Edge Case Protection:** Genuine light, persistent stratiform drizzle can sometimes present a very uniform tipping rate.
  - **Recommendation:** Correlate with wind data or synoptic context. Drips are often structural (wind vibrating a blocked funnel). If winds are calm and synoptic weather indicates frontal rain, loosen the CV threshold.

### Check 7: Accumulation Anomaly
- **Current:** 24hr gauge total / 24hr neighbour mean > 3.5.
- **Recommendations:**
  - **Orographic Enhancement Factor:** A station high on a fell might legitimately record 3.5x more rain over 24h than a valley neighbour during a prolonged westerly atmospheric river.
  - **Recommendation:** Normalize totals against average annual rainfall (SAAR) ratios. Evaluate `(Gauge_24h / Gauge_SAAR) vs (Neighbour_Mean_24h / Neighbour_Mean_SAAR)` rather than raw values.

---

## 3. System and Architecture Recommendations

### LLM Assessment Strategy
- **Current:** Triggers on 2+ check failures or high values (>20mm). Only runs on the first flag to save API costs.
- **Observation:** Skipping the LLM on subsequent runs means long-lasting but dynamic events (e.g., a massive 6-hour storm) might be classified based solely on the first anomalous 15-minute slot.
- **Recommendation:** Allow re-triggering the LLM assessment if the weather context changes drastically (e.g., radar intensity increases by an order of magnitude) or re-evaluate once every 2-3 hours during a persistent event. Furthermore, ensure the LLM receives altitude and time-of-year data to better assess plausibility.

### Data Latency and Synchronization
- **Current:** `LOOKBACK_SLOTS = 3` (45 min) to account for telemetry latency.
- **Recommendation:** Asynchronous telemetry can lead to "false temporal spikes" where a bulked reading (multiple tips transmitted at once) is recorded in a single slot. Implement an "Accumulation De-bulking" pre-processing step that identifies non-standard reporting intervals and distributes the accumulated volume over the silent period before running the QC checks.

## 4. Summary of Top Priorities
1. **Incorporate Topography:** Move away from pure circular/distance-based nearest neighbour checks. Use elevation and Annual Average Rainfall (SAAR) as weighting factors for both accumulation and spatial checks.
2. **Temperature Awareness:** Integrate a temperature feed to prevent false "Persistent Zero" flags during snow events.
3. **Radar Quality Index:** Modulate radar conformity limits based on the station's known distance and visibility from the nearest C-band/S-band radar site.
4. **Dynamic Thresholds:** Transition the hard limits (25mm/50mm) to station-specific thresholds based on historical extreme value analysis.
