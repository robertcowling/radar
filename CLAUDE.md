# Radar / Flood Forecast — Claude notes

## Workflow

- **Always push committed changes to `main` on GitHub.** The user wants commits pushed, not left local — after committing, push to `origin main` (rebasing on top of the frequent automated data-update commits if the remote has moved on) without needing to be asked each time.

## UI conventions

- **No block caps / all-caps in the UI.** The user dislikes `text-transform: uppercase` and writing labels in ALL CAPS. Use sentence case or title case instead. This applies to table headers, legend labels, status pills, modal text, and any UI copy.
- Labels and pills use title case for proper nouns and single-word abbreviations (e.g. "Spike", "Spatial", "Genuine"), not "SPIKE", "SPATIAL", "GENUINE".

## Project structure

- Main viewer: `index.html` (Leaflet radar + satellite + MSLP)
- Rain gauges: `rain/index.html`
- Gauge QC: `gaugecheck.html` + `gauge_qc.py`
- Data served from Cloudflare R2 via `https://radar.floodforecast.co.uk`

## R2 Class A budget

The Cloudflare R2 free tier allows **1M Class A operations/month**
(PutObject, CopyObject, ListObjects — LIST costs one op *per page*).
Class B (GetObject, HeadObject) has a 10M allowance and is effectively
free at our volumes; DeleteObject is completely free. The fetch scripts
run every 5–15 minutes, so per-run waste multiplies fast. Rules for any
script that writes to R2:

- **Never unconditionally PUT content that may be unchanged.** R2's ETag
  for a single-part upload is the body MD5, so `head_object` + MD5 compare
  (Class B) tells you whether a PUT (Class A) can be skipped. See
  `r2_upload()` in `make_accum_hourly.py` or `r2_upload_file()` in
  `make_dashboard_composites.py` for the pattern.
- **Don't re-LIST a prefix you already listed.** Retention cleanups should
  reuse the key listing fetched at startup (deletes are free), not paginate
  the prefix again — see `cleanup_r2()` in `process_latest.py`.
- **Gate LIST-based prune scans to ~hourly** (`utcnow().minute < 10`)
  rather than every invocation; expired objects only cross a retention
  cutoff once an hour at most.
- The dominant remaining consumer is `fetch_ukv.py` (~25–30 PNG/JSON
  uploads per forecast step × ~55 steps × 8 runs/day ≈ 350k ops/month) —
  those are genuinely new objects each run, so reducing them means
  reducing schemes/thresholds/durations (a product decision, not plumbing).
  `fetch_ecmwf.py` adds a smaller ~76k ops/month on top of that (one
  scheme × up to 5 uploads/step × ~504 steps/day, across its 4 daily runs'
  hourly-to-T+90h cadence and up-to-15-day horizon) — combined R2 Class A
  usage is still well under 45% of the 1M/month free tier.

## Freshness timestamps are a public API (floodforecast consumer contract)

The sibling **floodforecast** Flask app reads the timestamp fields out of the
manifests this repo publishes and uses them to tell users, on the page,
whether what they are looking at is current. That makes these fields a
consumer contract rather than an internal detail. Nothing here needs a UI
change in this repo; it needs the fields to keep behaving.

| Object | Field floodforecast grades |
|---|---|
| `frames_parallel.json` | `time` on the last element (`Wed 12 Aug 2026 16:30 UTC`) |
| `rain/meta.json` | `latest_time` |
| `warnings/warnings_latest.json` | `generated_at` |
| `ea/floods/current.json`, `nrw/floods/current.json` | `generated_at` |
| `accum_multi_meta.json` | `generated_at` |
| `mslp_meta.json` | `generated_at` |
| `ukv_meta.json` | `runs[0].run_ts` |
| `ecmwf_meta.json` | `runs[0].run_ts` |

Three things to keep in mind when changing any of them:

- **Renaming or dropping one of these fields silently breaks a user-facing
  warning.** It does not fail loudly — floodforecast just stops being able to
  tell users their data is stale, which is the exact failure the feature
  exists to prevent. Treat them as public API.
- **A cadence change needs a matching threshold change in floodforecast**,
  because thresholds are 3× cadence for "late" and 6× for "very late". The
  live example is the Met Office warnings move from 30 to 10 minutes: that
  takes "late" from 90 minutes down to 30. Changing the schedule without
  telling the consumer leaves it either crying wolf or asleep.
- **`mslp_meta.json`, `ukv_meta.json` and `ecmwf_meta.json` frames run into
  the future** — they are forecast series, so the newest frame's
  `valid_time` is *ahead* of now (MSLP was +324 minutes ahead of its own
  `generated_at` when this was written). A consumer must never treat
  newest-frame time as data age; it would read as permanently fresh no
  matter how long the pipeline had been dead. `mslp_meta.json` also carries
  **no run timestamp at all** — only `generated_at` and per-frame
  `valid_time` — so if run-age grading is ever wanted for MSLP, this repo
  has to publish a run field first.

## UKV forecast fetch (`fetch_ukv.py`)

- Met Office publishes UKV runs **hourly**, but only the 3-hourly synoptic
  runs (00/03/06/09/12/15/18/21Z) go out to the full T+54h forecast horizon.
  The runs in between (e.g. 19Z, 20Z) only ever publish out to **T+11h** —
  this is expected Met Office behaviour, not a stall or an outage.
- `is_run_complete()` checks for the run's T+54h file on Met Office's public
  S3 bucket (`met-office-atmospheric-model-data`, region `eu-west-2`, prefix
  `uk-deterministic-2km/`) before accepting a run as processable. Since only
  the 3-hourly runs ever reach T+54h, a run showing "not yet complete" for
  a long time is only actually stuck if it's one of the 00/03/06/09/12/15/18/21Z
  runs — if it's an off-cycle hourly run, it will never complete and that's
  correct, `find_latest_run` will move on once a genuine 3-hourly run appears.
  Browse the bucket directly to check current state, e.g.:
  `https://s3.console.aws.amazon.com/s3/buckets/met-office-atmospheric-model-data?region=eu-west-2&prefix=uk-deterministic-2km/`
  (needs any AWS login to browse a public bucket via the console) or the
  plain unauthenticated listing:
  `https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/?list-type=2&delimiter=/&prefix=uk-deterministic-2km/`
- `FORCE_RERUN=1` (env var, also exposed as a `force_rerun` workflow_dispatch
  input on the "Fetch UKV Forecast" GitHub Action) bypasses the "already
  processed" guard so a boundary/aggregation fix can be reprocessed onto the
  latest run without waiting for the next one — but only once `is_run_complete`
  is also satisfied for that run.

## ECMWF HRES forecast fetch (`fetch_ecmwf.py`)

- Second, independent deterministic-rainfall page (`ecmwf.html`) modelled on
  UKV's pipeline, covering the same map domain/canvas for direct visual
  comparability. **Not sourced from ECMWF directly** — ECMWF's own free Open
  Data feed is capped at 0.25° (~28km), and a genuinely useful map needs
  something closer to the model's real resolution. Instead this pulls from
  **Open-Meteo's public AWS Open Data S3 bucket** (`s3://openmeteo`,
  `us-west-2`, unauthenticated), which redistributes ECMWF's own IFS HRES
  output at full **~9km native resolution** under the same CC BY 4.0 licence
  — ECMWF has said they'll offer this resolution directly "later in 2026",
  at which point revisit whether to switch source. Path:
  `data_spatial/ecmwf_ifs/{YYYY}/{MM}/{DD}/{HH}00Z/{valid_iso}.om`, read via
  the `omfiles` package (not GRIB/cfgrib — an earlier version of this script
  pulled ECMWF's own 0.25° GRIB2 files directly and smoothed them for
  display; that whole approach was replaced, not extended, once the 9km
  source was found and validated).
- The source grid is ECMWF's **O1280 octahedral reduced Gaussian grid** —
  unstructured (each latitude row has a different point count), with no
  coordinate array shipped per file since it's static across every timestep
  and run. `build_o1280_grid()` reconstructs it from first principles
  (Gaussian latitudes via Gauss-Legendre quadrature + the standard
  octahedral points-per-row formula `pl(i) = 4*i + 16`) — trust this only
  because it was verified two independent ways before being relied on: the
  computed point count matches Open-Meteo's array length exactly
  (6,599,680), and a rendered UK-domain subset lines up pixel-for-pixel with
  the UK coastline. If this ever needs re-deriving, re-run that same
  validation before trusting a change to it.
- Remapping onto the output canvas uses **inverse-distance-weighted
  interpolation over each pixel's 4 nearest source points**
  (`scipy.spatial.cKDTree`, built once per run in `build_mapping()` since the
  source grid never changes) — not a regular-grid method, because O1280 isn't
  a regular grid. This is what gives the page its smooth, ECMWF-chart-like
  rain boundaries instead of blocky grid cells.
- Open-Meteo's `precipitation` field is a genuine **backward sum** — the
  rainfall (mm) for the interval since the *previous* available valid time
  (1h/3h/6h depending on where in the run's cadence a step falls) — verified
  empirically (global sums scale with interval length, not cumulatively).
  Unlike raw ECMWF `tp` (cumulative since T+0), this needs **no
  de-accumulation step**: each step's value already is that interval's
  rainfall, fed straight into the accum-stack windowing logic UKV also uses.
- Same horizon split as ECMWF's own feed, just relabelled by run hour rather
  than a source-provided "stream" field: 00Z/12Z runs (`stream: "oper"`) go
  to the full **15-day** horizon (hourly to T+90h, 3-hourly to T+144h,
  6-hourly beyond); 06Z/18Z runs (`stream: "scda"`) only reach **6 days**
  (T+144h). `ecmwf.html` labels `scda` runs "(short-range)" in the run
  dropdown.
- Run completeness is read straight from each run's `meta.json`
  (`completed: true/false` + the exact `valid_times` list) — much simpler
  than the old GRIB2 approach's "probe for a specific final-step file"
  check, and avoids hardcoding a step schedule that could drift.
- v1 is deliberately lean vs UKV: one colour scheme (`_MET_COLORS`, the same
  "alternative" palette UKV itself offers, chosen so the two pages aren't
  visually confusable) and four accumulation windows (3h/6h/24h/48h, not
  UKV's six) — no splat masks, no polygon/gauge time series. R2 key layout
  mirrors UKV's: `ecmwf/{run_ts}/{offset}_{prefix}_met.png`.
- Higher time-resolution (hourly to T+90h) and a longer horizon (15 vs the
  old feed's 10 days) mean roughly 2.5x more steps/day than the original
  0.25°-based design — still only ~75k R2 ops/month (well within budget, see
  above), but worth knowing if step counts ever look surprising.
- Availability delay after nominal run time is roughly 6-9 hours (no
  additional delay vs ECMWF's own 0.25° feed, per Open-Meteo). The Google
  Apps Script trigger for the "Fetch ECMWF HRES Forecast" workflow should
  poll **hourly** — frequent enough to pick up each of the 4 daily runs
  within about an hour, while a "not ready yet" check is just a small
  `meta.json` fetch, so faster polling buys nothing.
- `ecmwf.html` shows catchment/region/county boundary outlines for
  geographic reference (same GeoJSON sources as `/ukv`), but — unlike
  UKV's boundary layer — with no per-run choropleth fill, since v1 has no
  polygon-average data pipeline.
- No GRIB/eccodes system dependency any more (`ecmwf.yml` no longer runs
  `apt-get install libeccodes-dev`) — reading `.om` files is pure
  Python+numpy+scipy, which simplified the workflow versus the first cut of
  this script.
- **Run time is latency-bound, not bandwidth-bound**: traced actual S3
  requests and found `omfiles` reads each file as ~40-50 small (~65KB)
  sequential range GETs rather than one bulk transfer — the real data
  needed per file is only ~1-2MB, so wall-clock is dominated by per-request
  network round-trip time, not data volume. That makes the per-step fetch
  step highly parallelizable: `STEP_FETCH_WORKERS = 16` in `fetch_ecmwf.py`
  is a measured value (roughly halved wall-clock vs 6 workers on a same-size
  batch), not a guess — 30 workers measured *worse* than 16, likely
  contention on fsspec's internal sync-over-async event loop, so don't just
  keep raising it if a future run still feels slow; re-measure first.

## FGS tracker (`fetch_fgs.py` / `fgscomparison/index.html`)

- FGS = the FFC 5-day Flood Guidance Statement (daily ~10:30 UK, occasional
  amendments). API: `https://api.ffc-environment-agency.fgs.metoffice.gov.uk/api/public/v3/statements`
  (header `X-Api-Key`, secret `FFC_API_KEY`/`FGS_API_KEY`; same host as RFG).
  The API keeps only the ~50 newest statements — our archive accumulates them
  permanently on R2.
- Each statement links a `detailed_csv_url` ("five day export"): one row per
  county per day (109 England & Wales counties × 5 days) with overall risk
  (VL/L/M/H) plus a two-digit cell per source (rivers/surface/coastal/ground):
  first digit impact 1–4, second likelihood 1–4, comma-separated when a county
  has multiple areas of concern (e.g. `32,33`). `14` (minimal impact, high
  likelihood) is the everywhere-default "nothing to see" value.
- R2 keys: `fgs/index.json` (rolling index with per-day above-VL and
  areas-of-concern county counts) and `fgs/archive/{id}.json` +
  `fgs/archive/{id}_aoc.jpg` (parsed statement + archived area-of-concern
  image). Statements are re-fetched when `last_modified_at` changes.
  Workflow: `fgs_update.yml`, hourly cron.
- `fgscomparison/index.html` compares successive statements **by valid date**
  (day 2 of yesterday's issue = day 1 of today's): change lists, county
  choropleths (matched to `geo/uk-counties.geojson` by expanding N/S/E/W/NE/Gtr
  abbreviations), diff map, per-source 4×4 risk matrices, county × date table.
  Overall county risk often stays VL while all the action is in source cells,
  so change detection always scans every source, not just the selected view.

## Named storm archive (`fetch_storms.py` / `storm_scraper.py` / `storm_summaries.py`)

- Migrated here from the sibling **floodforecast** Flask app's
  `/trigger-storm-update` route because this repo already
  had the `OPENAI_API_KEY`/`OPENAI_MODEL` secret and the
  workflow_dispatch-triggered-by-Google-Apps-Script convention the job needs —
  floodforecast had neither. Triggered externally roughly every 12 hours via
  the `storms_update.yml` workflow (`workflow_dispatch` only, no cron, same as
  every other job here). floodforecast's `/trigger-storm-update` route still
  exists and still works — it was *not* removed when the scheduled job moved
  here, and an earlier version of this note wrongly said it had been. Treat
  this repo as the scheduled runner and that route as a manual fallback; just
  don't point a scheduler at both, or two writers race on the same
  `storms.csv`.
- **This is the only script in the repo that writes to Google Cloud Storage**
  rather than R2. `storms.csv` lives in floodforecast's own GCS bucket
  (`grounded-chain-373213.appspot.com`) because its `/storms` page reads it
  from there directly — moving the *scraper* here doesn't move the *data*.
  `gcs_storms.py` is a minimal read/write helper for just this file, using env
  var `GOOGLE_JSON_KEY_FLOODFORECAST` — the same GCP service-account JSON key
  floodforecast's own `gcs_utils.py` already uses under that name, copied into
  a radar secret rather than a new key. Env-var-only, no local-file/ADC
  fallback (meaningless on a runner) — a missing secret fails loudly rather
  than trying some other auth path.
- `storm_scraper.py` (parses the Met Office Storm Centre — no API exists) and
  `storm_summaries.py` (LLM summaries from the storm's report PDF and/or the
  NSWWS warnings archive below) are verbatim ports from floodforecast's copies
  of the same files. floodforecast keeps its own copies as a manual
  dev-machine fallback — if you fix a parsing hazard here (the module
  docstrings list several real ones already hit: `&nbsp;` reserved names,
  non-ASCII names, cross-month date ranges, one PDF covering two storms), port
  the fix to both.
- `fetch_storms.py`'s `update_storms()` is a **merge, not an insert**: storm
  information arrives gradually (name first, impact dates a day or two later,
  a report PDF weeks later or never), so a field already in `storms.csv` is
  never overwritten with a blank. Storms are keyed by name **and** season —
  names repeat across seasons (Hannah in 2018-19 and 2025-26).
- `_load_warnings_for_window()` reads Met Office warnings back from **this
  repo's own public R2 CDN** (`warnings/archive/warnings_YYYYMMDD.json`) as
  evidence for the LLM summary — a same-repo HTTP round trip rather than
  reading local files, kept that way for parity with the floodforecast copy.
  Surface obs are deliberately not used as wind evidence: `surface_obs`
  archives 1-minute *mean* wind with no gust field, and quoting a mean as a
  storm's peak gust would read as plainly wrong.
- `python fetch_storms.py storms --dry-run` runs the full scrape/merge and
  prints what would change without writing to GCS — there's no test bucket,
  so this is the only safety net before a real write against the CSV
  floodforecast.co.uk reads live.

## Gauge QC (`gauge_qc.py` / `gaugecheck.html`)

- Results: `gaugecheck/results.json` (current run snapshot)
- Log: `gaugecheck/log.json` (rolling 48 h event log)
- Manifest + run archive: `gaugecheck/manifest.json` + `gaugecheck/runs/*.json`
- The log accumulates events keyed by `(station_id, first_flagged_at)` — one entry per station per "flagging episode".
