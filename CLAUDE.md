# Radar / Flood Forecast — Claude notes

## UI conventions

- **No block caps / all-caps in the UI.** The user dislikes `text-transform: uppercase` and writing labels in ALL CAPS. Use sentence case or title case instead. This applies to table headers, legend labels, status pills, modal text, and any UI copy.
- Labels and pills use title case for proper nouns and single-word abbreviations (e.g. "Spike", "Spatial", "Genuine"), not "SPIKE", "SPATIAL", "GENUINE".

## Project structure

- Main viewer: `index.html` (Leaflet radar + satellite + MSLP)
- Rain gauges: `rain/index.html`
- Gauge QC: `gaugecheck.html` + `gauge_qc.py`
- Data served from Cloudflare R2 via `https://radar.floodforecast.co.uk`

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

## Gauge QC (`gauge_qc.py` / `gaugecheck.html`)

- Results: `gaugecheck/results.json` (current run snapshot)
- Log: `gaugecheck/log.json` (rolling 48 h event log)
- Manifest + run archive: `gaugecheck/manifest.json` + `gaugecheck/runs/*.json`
- The log accumulates events keyed by `(station_id, first_flagged_at)` — one entry per station per "flagging episode".
