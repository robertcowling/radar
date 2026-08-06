"""
storm_summaries.py — LLM storm summary and hazard classification.

Fills the `Summary` and `Hazard` columns of storms.csv for storms whose
impact window has closed. The storms page presents Summary to users as an
"AI Generated Summary", which is what this produces.

Evidence, in order of preference
-------------------------------
1. **The Met Office storm report PDF**, when one exists. This is the
   authoritative account: verified peak gusts, rainfall totals, named
   locations, recorded impacts. Text is extracted and only used if the storm's
   own name appears in it, so a genuinely wrong link cannot feed one storm's
   verification figures into another's summary.

   Note that **one report can legitimately cover several storms**. Bert and
   Conall (2024-25) share a single PDF titled "Storm Bert, 22 to 25 November
   2024 and storm Conall, 26 to 27 November", and both Storm Centre rows link
   to it. That is correct, not a mislink, so the name check must not reject
   it — and the prompt below has to tell the model to attribute figures to the
   right storm, since Bert's 113 mph Cairngorm gust must not end up in
   Conall's entry.
2. **The Met Office NSWWS warnings** that were live during the impact window,
   from the project's own FGS/warnings history. Warning descriptions quote
   expected gusts and rainfall in mm, and the warning colours record how
   severe the Met Office judged the event at the time.

Why not surface observations
---------------------------
radar's own surface_obs archive holds 1-minute *mean* wind, with no gust
field. For a wind storm, mean wind badly understates the event — a storm
whose gusts hit 80 kt may show means in the 40s — and a summary quoting those
as "peak winds" would read as plainly wrong to anyone who knows the event.
Warning text and the report PDF both quote gusts properly, so those are used
instead.

Timing
------
A summary is generated once, three days after the storm's last impact date
(see storm_scraper.summary_due). Impact dates keep getting edited for a day
or two after an event, so summarising immediately risks describing a storm
the Met Office is still revising. A summary is only regenerated if forced, or
if a report PDF turns up later than the summary — the PDF adds real
verification data worth a rewrite.

This module is a verbatim port from the sibling floodforecast project's
`storm_summaries.py` — see radar's CLAUDE.md ("Named storm archive") for why
this pipeline lives here rather than there, and keep any fix ported to both
copies.
"""

import io
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL", "") or "gpt-5.4").strip()
MAX_COMPLETION_TOKENS = 1200

# Vocabulary already in storms.csv. The storms page maps these to icons by
# substring match on wind / rain / snow / ice, so staying inside this set
# keeps every row's icon correct.
HAZARD_VALUES = [
    "Wind", "Rain", "Rain/Flood", "Snow", "Snow/Ice",
    "Wind & Rain", "Wind/Snow", "Wind/Rain/Snow",
]

PDF_TIMEOUT = 60
PDF_MAX_BYTES = 12 * 1024 * 1024
PDF_MAX_CHARS = 18000

SYSTEM_PROMPT = """\
You are a UK operational meteorologist writing the archive entry for a Met \
Office named storm, for an audience of flood-risk and weather professionals.

Write 2-3 short paragraphs of plain prose. Cover, in this order:
1. What the storm was and when it affected the UK: the synoptic setup in one \
clause (e.g. a deep Atlantic low tracking northeast), and which parts of the \
UK bore the brunt.
2. What was actually recorded: peak gusts in mph with the station or area, \
rainfall totals in mm where relevant, and the Met Office warnings issued \
(name the colours and hazards).
3. The impacts that followed: power cuts, transport disruption, flooding, \
structural damage, fatalities if reported.

Rules:
- Use ONLY the evidence provided below. Never invent a gust figure, a rainfall \
total, a place name, or an impact.
- If the evidence does not support a figure, describe the event \
qualitatively instead of guessing a number.
- CRITICAL: a Met Office report often covers more than one storm, and the \
warnings listed may span a period that includes other storms. Write about \
{storm_name} ONLY. If a figure, location or impact in the evidence belongs to \
a different named storm, or to a date outside {storm_name}'s impact window, \
leave it out entirely — do not attribute it to {storm_name}. Where the \
evidence discusses the storms together and a figure cannot be confidently \
attributed to {storm_name}, omit it.
- Quote gusts as gusts and mean winds as mean winds. Never present a mean \
wind speed as a gust.
- Past tense. No preamble, no headings, no bullet points, no markdown.
- Do not begin with "Storm X was a storm". Lead with what it did.

Then, on a final separate line, output exactly:
HAZARD: <value>
choosing the single best value from this list: {hazards}
Base it on the dominant hazard(s) in the evidence, not on speculation.\
"""


# ── PDF ──────────────────────────────────────────────────────────────────────
def extract_pdf_text(url, storm_name, session=None):
    """Text of a Met Office storm report PDF, or '' if unusable.

    Returns '' when the PDF is missing, unreadable, or belongs to a different
    storm. The name check is the important part: the Storm Centre table has
    linked the wrong report before, so a PDF that never mentions this storm
    is treated as absent rather than trusted.
    """
    if not url or ".pdf" not in url.lower() and "pdf" not in url.lower():
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed — storm report PDFs will be skipped")
        return ""

    sess = session or requests.Session()
    try:
        resp = sess.get(url, timeout=PDF_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (compatible; FloodForecast/1.0)"})
        resp.raise_for_status()
        blob = resp.content
        if not blob.startswith(b"%PDF"):
            logger.warning("Storm report URL did not return a PDF: %s", url)
            return ""
        if len(blob) > PDF_MAX_BYTES:
            logger.warning("Storm report PDF unexpectedly large (%d bytes), skipping", len(blob))
            return ""
        reader = PdfReader(io.BytesIO(blob))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        logger.warning("Could not read storm report PDF %s: %s", url, e)
        return ""

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""

    # Fold accents both sides so 'Éowyn' matches 'Eowyn' in the PDF's text
    # layer, which is not always accented.
    def fold(s):
        return "".join(c for c in unicodedata.normalize("NFKD", s)
                       if not unicodedata.combining(c)).casefold()

    if fold(storm_name) not in fold(text):
        logger.warning("Storm report PDF for %s does not mention it (likely a "
                       "mislinked report) — not using it as evidence", storm_name)
        return ""

    return text[:PDF_MAX_CHARS]


# ── Warnings evidence ────────────────────────────────────────────────────────
def warnings_during(impact_start, impact_end, load_warnings):
    """Met Office warnings overlapping the impact window (plus a day either side).

    `load_warnings` is injected so this stays testable and so the caller owns
    where warnings come from. It takes (start_date, end_date) and returns an
    iterable of dicts with at least title/description keys.
    """
    if not impact_start:
        return []
    try:
        start = datetime.strptime(impact_start, "%Y-%m-%d").date() - timedelta(days=1)
        end = datetime.strptime(impact_end or impact_start, "%Y-%m-%d").date() + timedelta(days=1)
    except ValueError:
        return []
    try:
        return list(load_warnings(start, end) or [])
    except Exception as e:
        logger.warning("Could not load warnings for storm window %s..%s: %s",
                       impact_start, impact_end, e)
        return []


def build_evidence(storm, pdf_text="", warnings=None):
    """Assemble the evidence block handed to the model."""
    lines = [
        f"STORM: {storm.get('name', '')}",
        f"SEASON: {storm.get('season', '')}",
        f"DATE NAMED: {storm.get('named_raw') or storm.get('named_date') or 'unknown'}",
    ]
    if storm.get("named_by"):
        lines.append(f"NAMED BY: {storm['named_by']} "
                     "(a partner service, not the Met Office)")
    lines.append(f"IMPACT DATES: {storm.get('display_dates') or storm.get('impact_raw') or 'unknown'}")

    if warnings:
        lines.append("")
        lines.append(f"MET OFFICE WARNINGS ACTIVE DURING THE EVENT ({len(warnings)}):")
        for w in warnings[:40]:
            title = (w.get("title") or "").strip()
            desc = re.sub(r"\s+", " ", (w.get("description") or "")).strip()
            if len(desc) > 700:
                desc = desc[:700] + "..."
            regions = w.get("regions") or w.get("areas") or ""
            if isinstance(regions, (list, tuple)):
                regions = ", ".join(str(r) for r in regions)
            lines.append(f"- {title}" + (f" [{regions}]" if regions else ""))
            if desc:
                lines.append(f"  {desc}")
    else:
        lines.append("")
        lines.append("MET OFFICE WARNINGS: none available for this window.")

    if pdf_text:
        lines.append("")
        lines.append("MET OFFICE STORM REPORT (authoritative — prefer these figures):")
        # Reports are sometimes written up jointly for consecutive storms, so
        # say so explicitly rather than relying on the model to notice.
        others = _other_storms_mentioned(pdf_text, storm.get("name", ""))
        if others:
            lines.append(f"WARNING: this report also covers {', '.join(others)}. "
                         f"Only use figures and impacts attributable to "
                         f"Storm {storm.get('name', '')}.")
        lines.append(pdf_text)

    return "\n".join(lines)


def _other_storms_mentioned(text, this_name):
    """Other named storms referred to in a report, for the attribution warning."""
    found = []
    for m in re.finditer(r"[Ss]torm\s+([A-Z][a-zà-ÿÀ-Ÿ']+)", text or ""):
        other = m.group(1).strip()
        if other.casefold() == str(this_name or "").casefold():
            continue
        if other.casefold() in {"centre", "names", "naming", "season"}:
            continue
        if other not in found:
            found.append(other)
    return found[:6]


# ── LLM ──────────────────────────────────────────────────────────────────────
def _split_hazard(text):
    """Pull the trailing 'HAZARD: x' line off the prose."""
    if not text:
        return "", ""
    hazard = ""
    m = re.search(r"^\s*HAZARD\s*:\s*(.+?)\s*$", text, re.I | re.M)
    if m:
        hazard = m.group(1).strip().strip(".")
        text = (text[:m.start()] + text[m.end():]).strip()

    if hazard:
        # Snap to the existing vocabulary so the page's icon mapping holds.
        exact = {h.casefold(): h for h in HAZARD_VALUES}
        if hazard.casefold() in exact:
            hazard = exact[hazard.casefold()]
        else:
            low = hazard.casefold()
            parts = []
            if "wind" in low or "gale" in low:
                parts.append("Wind")
            if "rain" in low or "flood" in low:
                parts.append("Rain")
            if "snow" in low or "ice" in low:
                parts.append("Snow")
            hazard = "/".join(parts) if parts else "Wind"
    return text.strip(), hazard


def generate_summary(storm, pdf_text="", warnings=None):
    """(summary_prose, hazard) for one storm, or (None, None) on failure."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — cannot generate storm summaries")
        return None, None
    try:
        import openai
    except ImportError:
        logger.error("openai package not installed — cannot generate storm summaries")
        return None, None

    evidence = build_evidence(storm, pdf_text=pdf_text, warnings=warnings)

    # Refuse to invent an entry out of nothing. The dates alone are not
    # grounds for prose about gusts and impacts, and a fabricated summary is
    # worse than an empty cell the page can hide.
    if not pdf_text and not warnings:
        logger.warning("No evidence for storm %s (no report PDF, no warnings) — "
                       "skipping summary rather than inventing one", storm.get("name"))
        return None, None

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(
                    hazards=", ".join(HAZARD_VALUES),
                    storm_name=f"Storm {storm.get('name', '')}".strip())},
                {"role": "user", "content": evidence},
            ],
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            temperature=0.3,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("OpenAI error generating summary for %s: %s", storm.get("name"), e)
        return None, None

    summary, hazard = _split_hazard(raw)
    if not summary:
        return None, None
    return summary, hazard
