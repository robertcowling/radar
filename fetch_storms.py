#!/usr/bin/env python3
"""
fetch_storms.py — merges the Met Office Storm Centre scrape into storms.csv
on GCS, and generates LLM summaries for storms whose impact window has closed.

Runs from radar (via .github/workflows/storms_update.yml, workflow_dispatch
only, triggered externally by a Google Apps Script roughly every 12 hours) even
though the data it writes belongs to the sibling floodforecast project's
/storms page — see CLAUDE.md ("Named storm archive") for why. storm_scraper.py
and storm_summaries.py are verbatim ports from that project; the four
functions below are a near-verbatim port of its fetch_and_update.py, with
gcs_utils swapped for this repo's gcs_storms (GCS auth only, no local-file/ADC
fallback — meaningless on a runner) and no dependency on that project's other
FGS-fetching code.

Usage:
  python fetch_storms.py storms                       scrape + merge
  python fetch_storms.py storms --dry-run             scrape + merge, skip the GCS write
  python fetch_storms.py storms --prune               drop reserved-name placeholder rows
  python fetch_storms.py storms --backfill 2024-25 2023-24
  python fetch_storms.py summaries                    generate due summaries
  python fetch_storms.py summaries --force Amy,Bert
  python fetch_storms.py summaries --limit 3           cap the LLM spend per run

Needs GOOGLE_JSON_KEY_FLOODFORECAST always; summaries also need OPENAI_API_KEY
(OPENAI_MODEL optional, defaults to gpt-5.4 — see storm_summaries.py).
"""

import json
import logging
import os
import sys
from datetime import timedelta

import pandas as pd
import requests

import storm_scraper
import storm_summaries
from gcs_storms import read_csv_from_gcs, write_csv_to_gcs, STORMS_FILE

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _storm_row_season(row):
    """Season for an existing CSV row, tolerating older rows without one."""
    season = str(row.get('Season') or '').strip()
    if season:
        return season
    for iso_field in ('ImpactStart', 'DateNamed'):
        season = storm_scraper.season_for(str(row.get(iso_field) or '').strip())
        if season:
            return season
    # Pre-existing rows carry only the human date string.
    objs = storm_scraper.parse_impact_dates(str(row.get('Dates') or ''))
    return storm_scraper.season_for(objs[0]) if objs and objs[0] else ''


def update_storms(force_scrape_seasons=None, dry_run=False):
    """Scrape the Met Office Storm Centre and merge into storms.csv on GCS.

    Storm information appears **gradually**: a storm is usually listed with a
    "Date named" and empty impact dates, which fill in a day or two later, and
    a report PDF may follow weeks after that. So this is a merge, not an
    insert. Three rules make that safe:

    1. **Fields only ever fill in.** A value already in the file is never
       replaced by a blank one. If the Met Office reworks the page, or a fetch
       returns a stale cache, or the parser trips on one cell, a storm that
       had impact dates cannot lose them.
    2. **A changed value is logged, not hidden.** If a non-empty field changes
       to a different non-empty value that is a genuine Met Office correction,
       so it is applied and recorded in the log.
    3. **Identity is name + season.** Storm names repeat between seasons
       (Hannah in 2018-19 and again in 2025-26); keying on name alone silently
       drops the second one, which is what used to happen.

    Reserved names the Met Office has published but not yet used carry empty
    date cells and are not storms — they are skipped, so they never reach the
    file or inflate the storm count on the page.

    `dry_run=True` runs the full scrape and merge but skips the GCS write —
    there is no test bucket, and a bad write to the CSV floodforecast.co.uk's
    /storms page reads live is the highest-risk failure mode in this pipeline.

    Returns a dict describing what changed, so the caller can report it.
    """
    logger.info("Starting storms update%s...", " (dry run)" if dry_run else "")
    result = {'added': [], 'updated': [], 'unchanged': 0, 'error': None}

    try:
        session = requests.Session()

        scraped = storm_scraper.scrape_current_season(session=session)
        if not scraped:
            # An empty scrape means the fetch failed or the page changed
            # shape. Either way, writing nothing is correct: never let a bad
            # scrape rewrite the archive.
            result['error'] = 'no storms parsed from Storm Centre (page unreachable or changed)'
            logger.error(result['error'])
            return result

        # Optional backfill of past seasons, e.g. ['2024-25', '2023-24'].
        for season_slug in (force_scrape_seasons or []):
            past = storm_scraper.scrape_season(season_slug, session=session)
            logger.info("Backfill %s: %d storms", season_slug, len(past))
            scraped.extend(past)

        logger.info("Parsed %d named storms from Storm Centre", len(scraped))

        df_old = read_csv_from_gcs(STORMS_FILE)
        if df_old is None or df_old.empty:
            logger.warning("Existing %s is empty or unreadable — aborting rather than "
                           "overwriting it with a fresh scrape", STORMS_FILE)
            result['error'] = 'existing storms.csv empty or unreadable'
            return result

        rows = df_old.fillna('').to_dict('records')
        # Ensure every column this writer maintains exists, so older files
        # gain them without a migration step.
        for col in ('Name', 'Dates', 'Link', 'Summary', 'Hazard',
                    'Season', 'DateNamed', 'NamedBy', 'ImpactStart', 'ImpactEnd'):
            for r in rows:
                r.setdefault(col, '')

        index = {}
        # Legacy rows with no season at all: the placeholder rows the previous
        # scraper wrote for reserved names ('&nbsp;' dates, no season derivable).
        # Indexed separately by name so a scrape can adopt and repair one
        # instead of adding a second row beside it. A legacy row for a *real*
        # storm always yields a season from its date string, so it can never
        # be adopted by the wrong storm here.
        legacy_by_name = {}
        for r in rows:
            season = _storm_row_season(r)
            index[storm_scraper.storm_key(r.get('Name'), season)] = r
            if not season:
                legacy_by_name.setdefault(
                    storm_scraper.storm_key(r.get('Name'), ''), r)

        # Scraped field → CSV column. Summary and Hazard are deliberately
        # absent: those are generated (see storm_summaries.py) and must never
        # be touched by the scrape.
        FIELD_MAP = [
            ('display_dates', 'Dates'),
            ('link', 'Link'),
            ('season', 'Season'),
            ('named_date', 'DateNamed'),
            ('named_by', 'NamedBy'),
            ('impact_start', 'ImpactStart'),
            ('impact_end', 'ImpactEnd'),
        ]

        for storm in scraped:
            key = storm_scraper.storm_key(storm['name'], storm['season'])
            existing = index.get(key)

            if existing is None:
                # Adopt a season-less legacy row for this name rather than
                # writing a duplicate next to it. Only ever fires for the old
                # placeholder rows, which carry no data worth keeping anyway.
                legacy = legacy_by_name.pop(
                    storm_scraper.storm_key(storm['name'], ''), None)
                if legacy is not None:
                    logger.info("Adopting legacy placeholder row for %s into season %s",
                                storm['name'], storm['season'] or 'unknown')
                    # A placeholder's '&nbsp;' date must not survive the never-blank
                    # rule and block the real date, so clear it before merging.
                    if not storm_scraper.parse_impact_dates(str(legacy.get('Dates') or ''))[0]:
                        legacy['Dates'] = ''
                    existing = legacy
                    index[key] = legacy

            if existing is None:
                row = {
                    'Name': storm['name'],
                    'Dates': storm['display_dates'],
                    'Link': storm['link'],
                    'Summary': '',
                    'Hazard': '',
                    'Season': storm['season'],
                    'DateNamed': storm['named_date'],
                    'NamedBy': storm['named_by'],
                    'ImpactStart': storm['impact_start'],
                    'ImpactEnd': storm['impact_end'],
                }
                rows.append(row)
                index[key] = row
                result['added'].append(storm['name'])
                logger.info("New storm: %s (%s) named=%s impact=%s",
                            storm['name'], storm['season'] or 'season unknown',
                            storm['named_date'] or '?', storm['display_dates'] or 'pending')
                continue

            changes = []
            for src_field, col in FIELD_MAP:
                new = str(storm.get(src_field) or '').strip()
                old = str(existing.get(col) or '').strip()
                if not new:
                    continue                      # rule 1: never blank out
                if old == new:
                    continue
                if old:
                    # rule 2: a real correction, worth a log line
                    changes.append(f"{col}: {old!r} -> {new!r}")
                else:
                    changes.append(f"{col}: (blank) -> {new!r}")
                existing[col] = new

            if changes:
                result['updated'].append(storm['name'])
                logger.info("Updated storm %s (%s): %s",
                            storm['name'], storm['season'], "; ".join(changes))
            else:
                result['unchanged'] += 1

        if not result['added'] and not result['updated']:
            logger.info("No storm changes (%d already current)", result['unchanged'])
            return result

        if dry_run:
            logger.info("Dry run: would write %s (%d rows, %d added, %d updated) — skipping write",
                        STORMS_FILE, len(rows), len(result['added']), len(result['updated']))
            return result

        df_new = pd.DataFrame(rows)
        write_csv_to_gcs(df_new, STORMS_FILE)
        logger.info("Wrote %s: %d rows (%d added, %d updated)", STORMS_FILE,
                    len(df_new), len(result['added']), len(result['updated']))
        return result

    except Exception as e:
        logger.error("Error updating storms: %s", e, exc_info=True)
        result['error'] = str(e)
        return result


def prune_reserved_storm_names(dry_run=False):
    """One-off cleanup: drop rows that are reserved names, not storms.

    The previous scraper tried to filter unused names by testing whether the
    date cell started with '&#', but the page actually ships '&nbsp;', so the
    guard never fired and placeholder rows were written to storms.csv with a
    literal '&nbsp;' in the Dates column. They inflate the storm count on the
    page and show as season "Unknown".

    A row is only removed when it has no usable date **and** no summary, so a
    genuine storm can never be deleted by this.
    """
    df = read_csv_from_gcs(STORMS_FILE)
    if df is None or df.empty:
        logger.warning("storms.csv empty or unreadable — nothing to prune")
        return []

    rows = df.fillna('').to_dict('records')
    keep, dropped = [], []
    for r in rows:
        dates = str(r.get('Dates') or '').strip()
        has_date = bool(dates) and dates.lower() not in ('&nbsp;', 'nbsp;', 'nan')
        if not has_date:
            has_date = bool(storm_scraper.parse_impact_dates(dates)[0])
        has_iso = bool(str(r.get('ImpactStart') or '').strip()
                       or str(r.get('DateNamed') or '').strip())
        has_summary = bool(str(r.get('Summary') or '').strip())
        if has_date or has_iso or has_summary:
            keep.append(r)
        else:
            dropped.append(str(r.get('Name') or '').strip())

    if not dropped:
        logger.info("No reserved-name rows to prune")
        return []

    if dry_run:
        logger.info("Dry run: would prune %d reserved-name rows: %s — skipping write",
                    len(dropped), ", ".join(dropped))
        return dropped

    write_csv_to_gcs(pd.DataFrame(keep), STORMS_FILE)
    logger.info("Pruned %d reserved-name rows from storms.csv: %s",
                len(dropped), ", ".join(dropped))
    return dropped


def update_storm_summaries(force_names=None, delay_days=3, limit=None):
    """Generate Summary and Hazard for storms whose impact window has closed.

    Runs three days after a storm's last impact date (see
    storm_scraper.summary_due): impact dates get edited for a day or two after
    an event and the report PDF lags, so summarising sooner risks describing a
    storm the Met Office is still revising.

    A storm is summarised once. It is only redone when explicitly forced, or
    when a report PDF has appeared since the summary was written — that PDF
    carries the Met Office's own verified figures and is worth a rewrite.
    """
    logger.info("Starting storm summary generation...")
    result = {'generated': [], 'skipped': 0, 'failed': [], 'error': None}

    try:
        df = read_csv_from_gcs(STORMS_FILE)
        if df is None or df.empty:
            result['error'] = 'storms.csv empty or unreadable'
            logger.error(result['error'])
            return result

        rows = df.fillna('').to_dict('records')
        for col in ('Summary', 'Hazard', 'Season', 'DateNamed', 'NamedBy',
                    'ImpactStart', 'ImpactEnd', 'SummarySource'):
            for r in rows:
                r.setdefault(col, '')

        forced = {str(n).strip().casefold() for n in (force_names or [])}
        session = requests.Session()
        changed = False

        for r in rows:
            name = str(r.get('Name') or '').strip()
            if not name:
                continue
            is_forced = name.casefold() in forced

            impact_end = str(r.get('ImpactEnd') or '').strip()
            if not impact_end:
                objs = storm_scraper.parse_impact_dates(str(r.get('Dates') or ''))
                impact_end = objs[1] or ''

            has_summary = bool(str(r.get('Summary') or '').strip())
            source = str(r.get('SummarySource') or '').strip()

            if not is_forced:
                if not storm_scraper.summary_due(impact_end, delay_days=delay_days):
                    result['skipped'] += 1
                    continue
                # Already summarised, and either it used the report PDF or
                # there is still no PDF to improve on.
                if has_summary and (source == 'pdf' or not str(r.get('Link') or '').strip()):
                    result['skipped'] += 1
                    continue
                if has_summary and source == 'pdf':
                    result['skipped'] += 1
                    continue

            impact_start = str(r.get('ImpactStart') or '').strip()
            if not impact_start:
                objs = storm_scraper.parse_impact_dates(str(r.get('Dates') or ''))
                impact_start = objs[0] or ''

            storm = {
                'name': name,
                'season': str(r.get('Season') or '').strip() or _storm_row_season(r),
                'named_date': str(r.get('DateNamed') or '').strip(),
                'named_raw': str(r.get('DateNamed') or '').strip(),
                'named_by': str(r.get('NamedBy') or '').strip(),
                'impact_start': impact_start,
                'impact_end': impact_end,
                'impact_raw': str(r.get('Dates') or '').strip(),
                'display_dates': str(r.get('Dates') or '').strip(),
            }

            pdf_text = storm_summaries.extract_pdf_text(
                str(r.get('Link') or '').strip(), name, session=session)
            warnings = storm_summaries.warnings_during(
                impact_start, impact_end, _load_warnings_for_window)

            # Nothing new to work with: an existing summary stays as it is
            # rather than being replaced by a weaker one.
            if has_summary and not pdf_text and not is_forced:
                result['skipped'] += 1
                continue

            summary, hazard = storm_summaries.generate_summary(
                storm, pdf_text=pdf_text, warnings=warnings)
            if not summary:
                result['failed'].append(name)
                continue

            r['Summary'] = summary
            if hazard:
                r['Hazard'] = hazard
            r['SummarySource'] = 'pdf' if pdf_text else 'warnings'
            result['generated'].append(name)
            changed = True
            logger.info("Generated summary for %s (%s evidence, hazard=%s)",
                        name, r['SummarySource'], hazard or 'unset')

            if limit and len(result['generated']) >= limit:
                logger.info("Hit summary limit of %d, stopping", limit)
                break

        if changed:
            write_csv_to_gcs(pd.DataFrame(rows), STORMS_FILE)
            logger.info("Wrote %s with %d new summaries", STORMS_FILE, len(result['generated']))
        else:
            logger.info("No storm summaries generated (%d skipped)", result['skipped'])
        return result

    except Exception as e:
        logger.error("Error generating storm summaries: %s", e, exc_info=True)
        result['error'] = str(e)
        return result


def _load_warnings_for_window(start_date, end_date):
    """Met Office NSWWS warnings overlapping a date window.

    Reads radar's own warnings archive over its public R2 CDN, one file per
    day (warnings/archive/warnings_YYYYMMDD.json) — a same-repo round trip over
    HTTP rather than reading local files directly, kept that way for parity
    with the floodforecast copy of this same function. Every fetch is
    best-effort: a missing day is a day without an archive, not a failure, and
    a storm with no retrievable warnings simply gets no warning evidence.
    """
    base = os.environ.get('RADAR_CDN_BASE', 'https://radar.floodforecast.co.uk').rstrip('/')
    out = []
    day = start_date
    while day <= end_date:
        url = f"{base}/warnings/archive/warnings_{day.strftime('%Y%m%d')}.json"
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    out.extend(data)
        except Exception as e:
            logger.debug("No warnings archive for %s (%s)", day, e)
        day += timedelta(days=1)

    # One entry per warning: the archive holds a daily snapshot, so a warning
    # spanning several days appears in each of them.
    seen, deduped = set(), []
    for w in out:
        if not isinstance(w, dict):
            continue
        wid = w.get('alert_id') or (w.get('title'), w.get('description'))
        key = str(wid)
        if key in seen:
            continue
        seen.add(key)
        # Met Office warnings only — the archive also carries EA flood warnings,
        # which are a different product and would muddle a storm summary.
        src = str(w.get('source') or '').lower()
        if src and 'metoffice' not in src and 'ukmet' not in src:
            continue
        deduped.append(w)
    logger.info("Loaded %d Met Office warnings for %s..%s", len(deduped), start_date, end_date)
    return deduped


if __name__ == "__main__":
    #   python fetch_storms.py storms
    #   python fetch_storms.py storms --dry-run
    #   python fetch_storms.py storms --prune
    #   python fetch_storms.py storms --backfill 2024-25 2023-24
    #   python fetch_storms.py summaries
    #   python fetch_storms.py summaries --force Amy,Bert
    #   python fetch_storms.py summaries --limit 3
    args = sys.argv[1:]
    cmd = args[0] if args else ''
    dry_run = '--dry-run' in args

    def _opt(flag, default=None):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    if cmd == 'storms':
        if '--prune' in args:
            print(json.dumps({'pruned': prune_reserved_storm_names(dry_run=dry_run)}, indent=1))
        seasons = []
        if '--backfill' in args:
            seasons = [a for a in args[args.index('--backfill') + 1:] if not a.startswith('-')]
        print(json.dumps(update_storms(force_scrape_seasons=seasons or None, dry_run=dry_run),
                         indent=1, ensure_ascii=False))
    elif cmd == 'summaries':
        force = [n.strip() for n in (_opt('--force', '') or '').split(',') if n.strip()]
        limit = _opt('--limit')
        print(json.dumps(update_storm_summaries(
            force_names=force or None,
            limit=int(limit) if limit else None), indent=1, ensure_ascii=False))
    else:
        print(__doc__)
        sys.exit(1)
