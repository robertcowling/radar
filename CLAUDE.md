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
  `/trigger-storm-update` route (now removed there) because this repo already
  had the `OPENAI_API_KEY`/`OPENAI_MODEL` secret and the
  workflow_dispatch-triggered-by-Google-Apps-Script convention the job needs —
  floodforecast had neither. Triggered externally roughly every 12 hours via
  the `storms_update.yml` workflow (`workflow_dispatch` only, no cron, same as
  every other job here).
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
