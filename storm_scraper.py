"""
storm_scraper.py — Met Office UK Storm Centre scraper.

There is no API for named storms, so this scrapes the Storm Centre pages:

  https://www.metoffice.gov.uk/weather/warnings-and-advice/uk-storm-centre/index
    → current season's name list
  .../uk-storm-centre/uk-storm-season-YYYY-YY
    → a past season's name list (same table markup, used for backfill)

Each page carries one table: Name | Date named | Date of impact. The rows are
the *whole season's* name list, so most of them are names that have not been
used yet and carry empty date cells.

Why this exists as its own module
---------------------------------
Storm information appears **gradually**. A storm is typically added to the
table with a "Date named" and an empty impact column, and the impact dates
land hours or days later once the system has actually crossed the UK. A
report PDF may appear weeks after that, or never. Anything reading this
table has to treat every field as "may arrive later", which the previous
insert-only implementation did not: it wrote a row the first time it saw a
name and never looked at it again, so a storm caught in its name-only state
kept blank impact dates permanently.

Parser hazards this handles, all seen in live data
--------------------------------------------------
* **Unused future names** carry `&nbsp;` in both date cells. They are not
  storms and must not be archived as ones.
* **Non-ASCII names** — `Éowyn` (2024-25). The page is UTF-8 but does not
  always say so in its headers, so the encoding is forced rather than
  guessed, and names are NFC-normalised so the same storm cannot arrive as
  two different byte sequences.
* **`(Named by Meteo France)` / `(Named by AEMET)`** suffixes sit inside the
  "Date named" cell. Stripped out of the date, kept as `named_by`.
* **Date formats vary**: `3 - 4 October 2025`, `22-23 October 2025`,
  `14 November 2025`, `8 - 10 December 2025`. Cross-month ranges
  (`30 December - 1 January 2026`) have not appeared yet but will, so they
  are handled too.
* **PDF links are not identifiers.** In 2024-25, Conall's link points at
  Bert's PDF. Nothing keys off the link, and any consumer reading the PDF
  must confirm the storm's own name appears in the text before trusting it.
* **Names repeat between seasons** — there is a Storm Hannah in both 2018-19
  and 2025-26. A storm is identified by name *and season*, never name alone.

A parse failure on one row is never fatal: the row keeps its raw strings and
leaves the derived ISO fields empty, so one odd cell cannot cost a whole run.

This module is a verbatim port from the sibling floodforecast project's
`storm_scraper.py` — see radar's CLAUDE.md ("Named storm archive") for why
this pipeline lives here rather than there, and keep any parsing fix ported
to both copies.
"""

import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

STORM_INDEX_URL = "https://www.metoffice.gov.uk/weather/warnings-and-advice/uk-storm-centre/index"
STORM_SEASON_URL = "https://www.metoffice.gov.uk/weather/warnings-and-advice/uk-storm-centre/uk-storm-season-{season}"

# The Met Office site returns 403 to a bare requests user-agent often enough
# to matter for something meant to run unattended every few hours.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 30
FETCH_RETRIES = 3
FETCH_BACKOFF_S = 4

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


# ── Fetch ────────────────────────────────────────────────────────────────────
def fetch_page(url, session=None):
    """Return page HTML as text, or None. Retries transient failures.

    The response encoding is forced to UTF-8: the Storm Centre pages are
    UTF-8 but do not always declare it, and requests then falls back to
    ISO-8859-1, which turns `Éowyn` into mojibake.
    """
    sess = session or requests.Session()
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            resp = sess.get(url, timeout=FETCH_TIMEOUT, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-GB,en;q=0.9",
            })
            resp.raise_for_status()
            resp.encoding = "utf-8"
            html = resp.text
            if "Date named" not in html:
                # A 200 that does not contain the table is a redirect to a
                # cookie wall or an error page dressed as success. Treat it
                # as a failure so the retry has a chance, and so the caller
                # never parses zero storms out of a valid-looking page.
                raise ValueError("page fetched but contains no storm table")
            return html
        except Exception as e:
            if attempt == FETCH_RETRIES:
                logger.error("Storm page fetch failed after %d attempts (%s): %s", FETCH_RETRIES, url, e)
                return None
            wait = FETCH_BACKOFF_S * attempt
            logger.warning("Storm page fetch attempt %d/%d failed (%s), retrying in %ds",
                           attempt, FETCH_RETRIES, e, wait)
            time.sleep(wait)
    return None


# ── Cell helpers ─────────────────────────────────────────────────────────────
def _strip_tags(html):
    return re.sub(r"<[^>]+>", " ", html or "")


def _unescape(text):
    """Resolve the handful of entities the Storm Centre actually emits.

    `&nbsp;` matters most: it is what an unused future name carries in its
    date cells, and it must end up as an empty string so those rows can be
    filtered out.
    """
    if not text:
        return ""
    out = text
    for ent, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#160;", " "),
                      ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        out = out.replace(ent, char)
    out = re.sub(r"&[#a-zA-Z0-9]+;", " ", out)
    return out


def clean_cell(html):
    """Tags and entities out, whitespace collapsed, NFC-normalised."""
    text = _unescape(_strip_tags(html))
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()


def first_link(html):
    m = re.search(r'<a[^>]+href="([^"]+)"', html or "", re.I)
    if not m:
        return ""
    href = _unescape(m.group(1)).strip()
    # The page has shipped `?prefix=assets&prefix=assets` (Dave, 2025-26).
    href = re.sub(r"(\?prefix=assets)(&prefix=assets)+", r"\1", href)
    if href.startswith("/"):
        href = "https://www.metoffice.gov.uk" + href
    return href


# ── Date parsing ─────────────────────────────────────────────────────────────
def parse_named_date(cell):
    """'22 October 2025 (Named by Meteo France)' → ('2025-10-22', 'Meteo France').

    Returns (iso_or_empty, named_by_or_empty). A cell that cannot be parsed
    yields ('', named_by) so the raw text is still preserved by the caller.
    """
    text = clean_cell(cell)
    if not text:
        return "", ""

    named_by = ""
    m = re.search(r"\(\s*Named\s+by\s*([^)]*)\)", text, re.I)
    if m:
        named_by = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
        text = text[:m.start()] + text[m.end():]

    iso = ""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        month = MONTHS.get(m.group(2).lower())
        if month:
            try:
                iso = date(int(m.group(3)), month, int(m.group(1))).isoformat()
            except ValueError:
                iso = ""
    return iso, named_by


def parse_impact_dates(cell):
    """Impact cell → (start_iso, end_iso), either possibly ''.

    Handles the formats the page actually uses:
      '14 November 2025'          single day
      '3 - 4 October 2025'        spaced range
      '22-23 October 2025'        unspaced range
      '8 - 10 December 2025'      two-digit end
      '30 December - 1 January 2026'   cross-month (not yet seen, will happen)
      '31 December 2025 - 1 January 2026'  cross-year
    """
    text = clean_cell(cell)
    if not text:
        return "", ""

    def mk(y, mo, d):
        try:
            return date(int(y), int(mo), int(d))
        except (ValueError, TypeError):
            return None

    # Both sides carry their own month (and possibly year): the only shape
    # that can span a month or year boundary unambiguously.
    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]+)\s*(\d{4})?\s*(?:-|–|to)\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
        text, re.I)
    if m:
        m1, m2 = MONTHS.get(m.group(2).lower()), MONTHS.get(m.group(5).lower())
        y2 = m.group(6)
        # An omitted first year means the range sits inside one year, unless
        # it wraps December→January, in which case the start is the year before.
        y1 = m.group(3) or (str(int(y2) - 1) if (m1 and m2 and m1 > m2) else y2)
        start, end = mk(y1, m1, m.group(1)), mk(y2, m2, m.group(4))
        if start and end and start <= end:
            return start.isoformat(), end.isoformat()

    # Day range sharing one month and year: '3 - 4 October 2025'.
    m = re.search(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text, re.I)
    if m:
        mo = MONTHS.get(m.group(3).lower())
        start, end = mk(m.group(4), mo, m.group(1)), mk(m.group(4), mo, m.group(2))
        if start and end and start <= end:
            return start.isoformat(), end.isoformat()

    # Single day.
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        mo = MONTHS.get(m.group(2).lower())
        d = mk(m.group(3), mo, m.group(1))
        if d:
            return d.isoformat(), d.isoformat()

    # Hyphen-separated, possibly two-digit year: '29-Nov-15', '27-Apr-19',
    # '12-13 Nov 15'. Not a format the Storm Centre uses, but it is what many
    # rows already in storms.csv carry, and this parser is also used to read
    # those back (to work out a row's season). Matching DataProcessor's own
    # tolerance here is what stops an existing row looking season-less and
    # being mistaken for an unmigrated placeholder.
    m = re.search(r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?[\s-]+([A-Za-z]+)[\s-]+(\d{2,4})", text)
    if m:
        mo = MONTHS.get(m.group(3).lower())
        year = int(m.group(4))
        if year < 100:
            year += 2000
        start = mk(year, mo, m.group(1))
        end = mk(year, mo, m.group(2)) if m.group(2) else start
        if start and end and start <= end:
            return start.isoformat(), end.isoformat()
        if start:
            return start.isoformat(), start.isoformat()

    logger.warning("Could not parse storm impact dates from %r", text)
    return "", ""


def season_for(iso_date):
    """ISO date → '2025/26'. Storm seasons run September to August."""
    if not iso_date:
        return ""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return ""
    start = d.year if d.month >= 9 else d.year - 1
    return f"{start}/{str(start + 1)[-2:]}"


def format_display_dates(start_iso, end_iso):
    """ISO pair → the human format the storms table already uses.

    Mirrors DataProcessor._format_storm_date_range so a row written here
    reads identically to the ones already in the file.
    """
    if not start_iso:
        return ""
    try:
        s = datetime.strptime(start_iso, "%Y-%m-%d").date()
        e = datetime.strptime(end_iso, "%Y-%m-%d").date() if end_iso else s
    except ValueError:
        return ""
    if s == e:
        return f"{s.day} {s.strftime('%b')} {s.year}"
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.day}-{e.day} {s.strftime('%b')} {s.year}"
    if s.year == e.year:
        return f"{s.day} {s.strftime('%b')}-{e.day} {e.strftime('%b')} {e.year}"
    return f"{s.day} {s.strftime('%b')} {s.year}-{e.day} {e.strftime('%b')} {e.year}"


# ── Table parsing ────────────────────────────────────────────────────────────
def parse_storm_table(html):
    """Storm Centre HTML → list of scraped storm dicts.

    Only rows that have been *named* are returned; a row whose date cells are
    both empty is a name the Met Office has reserved but not yet used, and is
    not a storm. Rows are returned in page order (which is naming order).
    """
    if not html:
        return []

    tbody = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S | re.I)
    region = tbody.group(1) if tbody else None
    if region is None:
        table = re.search(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
        if not table:
            logger.error("No storm table found in Storm Centre page")
            return []
        region = table.group(1)

    storms = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", region, re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
        if len(cells) < 2:
            continue

        name = clean_cell(cells[0])
        # Drop the header row and any footnote markers ("Amy [1]").
        if not name or name.lower() == "name":
            continue
        name = re.sub(r"\s*\[\s*\d+\s*\]\s*$", "", name).strip()
        if not name:
            continue

        named_raw = clean_cell(cells[1]) if len(cells) > 1 else ""
        impact_raw = clean_cell(cells[2]) if len(cells) > 2 else ""

        if not named_raw and not impact_raw:
            continue  # reserved name, not yet used

        named_iso, named_by = parse_named_date(cells[1] if len(cells) > 1 else "")
        start_iso, end_iso = parse_impact_dates(cells[2] if len(cells) > 2 else "")

        # Season is anchored on whichever date exists. Named-date first: it is
        # present from the moment a storm appears, whereas impact dates lag,
        # and it is what lets a just-named storm sort into the right season.
        season = season_for(named_iso) or season_for(start_iso)

        storms.append({
            "name": name,
            "season": season,
            "named_date": named_iso,
            "named_raw": named_raw,
            "named_by": named_by,
            "impact_start": start_iso,
            "impact_end": end_iso,
            "impact_raw": impact_raw,
            "display_dates": format_display_dates(start_iso, end_iso) or impact_raw,
            "link": first_link(cells[0]),
        })

    return storms


def scrape_current_season(session=None):
    """The current season's named storms, or [] if the page can't be read."""
    return parse_storm_table(fetch_page(STORM_INDEX_URL, session=session))


def scrape_season(season_slug, session=None):
    """A past season's named storms. `season_slug` looks like '2024-25'."""
    url = STORM_SEASON_URL.format(season=season_slug)
    return parse_storm_table(fetch_page(url, session=session))


def storm_key(name, season):
    """Identity for a storm row: folded name plus season.

    Name alone is not unique — Hannah appears in 2018-19 and again in
    2025-26 — and keying on name alone silently drops the second one.
    """
    base = unicodedata.normalize("NFC", str(name or "")).strip().casefold()
    return f"{base}|{str(season or '').strip()}"


def summary_due(impact_end_iso, delay_days=3, today=None):
    """True once a storm's impact window is far enough past to summarise.

    Impact dates get edited for a day or two after an event and the report
    PDF lags further, so summaries wait rather than describing a storm the
    Met Office is still updating.
    """
    if not impact_end_iso:
        return False
    try:
        end = datetime.strptime(impact_end_iso, "%Y-%m-%d").date()
    except ValueError:
        return False
    ref = today or date.today()
    return ref >= end + timedelta(days=delay_days)
