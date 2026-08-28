"""CBR monetary-policy meeting tracker: pulls the press release, Nabiullina's
speech, and (anchor meetings only) the mid-term forecast + commentary for
each Key Rate decision, and — once published, ~2 weeks later — the discussion
summary (резюме обсуждения). No LLM here; pure fetch/parse. The LLM analysis
step lives in skills/cbr-rate-analysis and reads what this module stores.

Discovery mechanism (verified empirically against live cbr.ru, not assumed
from the URL patterns alone):

  cbr.ru/dkp/mp_dec/decision_key_rate/ is a SINGLE page listing every meeting
  back to ~2024 (21 blocks as of 2026-07-28), each block already containing
  direct links to the press release, the speech (press/event/?id=...), and
  -- for anchor meetings -- the forecast PDF and the comment (format varies:
  sometimes a PDF, sometimes an HTML page at
  .../decision_key_rate/comment_DDMMYYYY/). One request replaces per-document
  guesswork entirely, INCLUDING the "event ID is not predictable" problem the
  original spec flagged for the speech: the ID is just sitting on this page
  next to its meeting date.

  Резюме обсуждения (summary_key_rate_DDMMYYYY page) shows up on this SAME
  index page once CBR publishes it (~2 weeks after the meeting) -- so
  re-fetching the index page later picks it up automatically. No separate
  discovery path needed.

  Fallback: cbr.ru/search/?text=... (CBR's own site search) for a meeting
  whose block isn't on the index page yet (e.g. a same-day fetch before
  today's row has been added) -- confirmed working via a live query for
  "Заявление Председателя 24 апреля 2026".

Rate figures are read straight from the press release's own headline
sentence ("...снизить/повысить ключевую ставку на N б.п., до X% годовых" or
"...сохранить ключевую ставку на уровне X% годовых") -- this gives
rate_after AND rate_change_bp directly from the primary source, so
rate_before never needs a separate lookup against the previous meeting.

Forecast-table parsing is best-effort text extraction (pypdf) off the
"Ключевая ставка, в среднем за год" and "Инфляция..." rows -- the PDF text
layer occasionally fuses a footnote-reference digit onto the last number in
a range (seen once: "14,5-14,61" for what the document's own footnote shows
is "14,5-14,6" + footnote marker "1"). This is NOT auto-corrected: guessing
which trailing digit is a footnote vs. real precision is exactly the kind of
silent number-mangling this project avoids elsewhere. The raw extracted
string is stored as-is; a human (or the analysis skill) should sanity-check
it against the source PDF before trusting the last digit.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .health import log_health
from .store import db

MOEX_STYLE_UA = {"User-Agent": "hybond/0.1 (research)"}
BASE = "https://cbr.ru"
MATERIALS_INDEX_URL = f"{BASE}/dkp/mp_dec/decision_key_rate/"
SEARCH_URL = f"{BASE}/search/"
RAW_DIR = Path("data/raw/cbr")

# 2026 meeting schedule (confirmed from the CBR calendar, cbr.ru/dkp/cal_mp/).
# resume_date is CBR's own ~2-weeks-later target; actual publication can
# drift a day or two (observed: scheduled 2026-07-02 for the 19 June
# meeting, actually published 2026-07-01).
CBR_MEETINGS_2026 = [
    {"date": "2026-02-13", "anchor": True, "resume_date": "2026-02-26"},
    {"date": "2026-03-20", "anchor": False, "resume_date": "2026-04-02"},
    {"date": "2026-04-24", "anchor": True, "resume_date": "2026-05-07"},
    {"date": "2026-06-19", "anchor": False, "resume_date": "2026-07-02"},
    {"date": "2026-07-24", "anchor": True, "resume_date": "2026-08-06"},
    {"date": "2026-09-11", "anchor": False, "resume_date": "2026-09-24"},
    {"date": "2026-10-23", "anchor": True, "resume_date": "2026-11-05"},
    {"date": "2026-12-18", "anchor": False, "resume_date": "2026-12-30"},
]


def _get(url: str, params: dict | None = None, timeout: int = 30) -> requests.Response:
    r = requests.get(url, params=params or {}, timeout=timeout, headers=MOEX_STYLE_UA)
    r.raise_for_status()
    return r


def _snapshot(meeting_date: str, name: str, content: bytes | str) -> Path:
    d = RAW_DIR / meeting_date
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(p, mode, encoding=None if mode == "wb" else "utf-8") as fh:
        fh.write(content)
    return p


def _ddmmyyyy_to_iso(s: str) -> str:
    d, m, y = s.split(".")
    return f"{y}-{m}-{d}"


# --- materials index parsing -------------------------------------------------

_BLOCK_HEADER_RE = re.compile(
    r'Заседание Совета директоров от (\d{2}\.\d{2}\.\d{4})')
_PRESS_LINK_RE = re.compile(r'href="(/press/pr/\?file=[^"]+)"')
_SPEECH_LINK_RE = re.compile(r'href="(/press/event/\?id=\d+)"[^>]*>\s*<span[^>]*>\s*Заявление Председателя')
_FORECAST_LINK_RE = re.compile(r'href="(/[Cc]ontent/[Dd]ocument/[Ff]ile/\d+/forecast_\d+\.pdf)"')
_COMMENT_LINK_RE = re.compile(
    r'href="([^"]*comment_\d+[^"]*)"[^>]*>\s*(?:<span[^>]*>)?\s*(?:<br>)?\s*Комментарий')
_RESUME_LINK_RE = re.compile(r'href="([^"]*summary_key_rate_\d+[^"]*)"')


def _parse_materials_index(html: str) -> dict[str, dict[str, str | None]]:
    """DD.MM.YYYY-keyed meeting-date -> discovered document URLs. Splits the
    page on its 'Заседание Совета директоров от DD.MM.YYYY' block headers,
    then regexes each block for the doc-type links it contains (a block
    for a non-anchor meeting simply won't match forecast/comment)."""
    headers = list(_BLOCK_HEADER_RE.finditer(html))
    out: dict[str, dict[str, str | None]] = {}
    for i, m in enumerate(headers):
        date_ddmmyyyy = m.group(1)
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(html)
        block = html[start:end]

        press_m = _PRESS_LINK_RE.search(block)
        speech_m = _SPEECH_LINK_RE.search(block)
        forecast_m = _FORECAST_LINK_RE.search(block)
        comment_m = _COMMENT_LINK_RE.search(block)
        resume_m = _RESUME_LINK_RE.search(block)

        out[date_ddmmyyyy] = {
            "press_release_url": (BASE + press_m.group(1)) if press_m else None,
            "speech_url": (BASE + speech_m.group(1)) if speech_m else None,
            "forecast_pdf_url": (BASE + forecast_m.group(1)) if forecast_m else None,
            "comment_url": (BASE + comment_m.group(1)) if comment_m else None,
            "resume_url": (BASE + resume_m.group(1)) if resume_m else None,
        }
    return out


def _site_search_event_url(query: str) -> str | None:
    """Fallback when a meeting's block isn't on the materials index yet.
    Returns the first /press/event/?id=... hit whose title contains
    'Заявление Председателя'."""
    r = _get(SEARCH_URL, params={"text": query})
    for m in re.finditer(
            r'<div class="title"><a href="(/press/event/\?id=\d+)"[^>]*>([^<]*)</a></div>',
            r.text):
        if "Заявление Председателя" in m.group(2):
            return BASE + m.group(1)
    return None


# --- text extraction ---------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _html_to_text(html: str) -> str:
    """Concatenate every div.landing-text block's visible text (that's where
    both press releases and резюме/comment HTML pages keep their body),
    tags stripped, entities unescaped, whitespace collapsed."""
    import html as html_mod
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    blocks = re.findall(r'<div class="landing-text">(.*?)</div>\s*(?=<div class="landing-text">|<div class="banner|$)',
                         html, re.DOTALL)
    if not blocks:
        # single-block pages (e.g. speech) may not have a clean trailing
        # boundary the regex above expects -- fall back to the first
        # landing-text div through the end of its containing section.
        m = re.search(r'<div class="landing-text">(.*)', html, re.DOTALL)
        blocks = [m.group(1)] if m else []
    text = "\n".join(_TAG_RE.sub(" ", b) for b in blocks)
    text = html_mod.unescape(text)
    text = text.replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _pdf_to_text(content: bytes) -> str:
    from pypdf import PdfReader
    import io
    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


# --- rate extraction from the press release ---------------------------------

_RATE_DECREASE_RE = re.compile(
    r"снизить\s+ключевую\s+ставку\s+на\s*(\d+)\s*б\.?\s*п\.?,?\s*до\s*(\d+[.,]\d+)\s*%")
_RATE_INCREASE_RE = re.compile(
    r"повысить\s+ключевую\s+ставку\s+на\s*(\d+)\s*б\.?\s*п\.?,?\s*до\s*(\d+[.,]\d+)\s*%")
_RATE_HOLD_RE = re.compile(
    r"сохранить\s+ключевую\s+ставку\s+на\s+уровне\s*(\d+[.,]\d+)\s*%")


def extract_rate_decision(press_text: str) -> tuple[float | None, float | None, int | None]:
    """(rate_before, rate_after, rate_change_bp) parsed straight from the
    press release's own headline sentence -- no lookup against a prior
    meeting needed, the release states both the new rate and the delta."""
    m = _RATE_DECREASE_RE.search(press_text)
    if m:
        bp, after = int(m.group(1)), float(m.group(2).replace(",", "."))
        return round(after + bp / 100, 2), after, -bp
    m = _RATE_INCREASE_RE.search(press_text)
    if m:
        bp, after = int(m.group(1)), float(m.group(2).replace(",", "."))
        return round(after - bp / 100, 2), after, bp
    m = _RATE_HOLD_RE.search(press_text)
    if m:
        after = float(m.group(1).replace(",", "."))
        return after, after, 0
    return None, None, None


# --- forecast-table extraction ------------------------------------------------

_NUM_TOKEN_RE = r"[\d,.–\-—]+"


def _extract_row(text: str, label: str) -> list[str]:
    """Find `label` in the forecast/comment text and grab the numeric-ish
    tokens (plain values or A-B ranges) up to the end of that table row
    (next newline) -- NOT a fixed count. The CBR forecast table's column
    count varies by vintage (4 forward years in the April 2026 release, 5
    by July 2026 as the rolling 3-years-ahead horizon advances), so a fixed
    "grab N tokens" bled into the next row's numbers for the shorter table.
    """
    idx = text.find(label)
    if idx == -1:
        return []
    tail = text[idx + len(label):]
    line_end = tail.find("\n")
    row = tail if line_end == -1 else tail[:line_end]
    tokens = re.findall(_NUM_TOKEN_RE, row)
    return [t for t in tokens if re.search(r"\d", t)]


def extract_forecast_ranges(forecast_text: str) -> dict[str, str | None]:
    """Row layout is (year-minus-1 факт, 2026, 2027, ..., longest horizon) --
    index 0 is always the prior-year actual, index 1 is always 2026 (the
    forecast's near year never moves), index 2 is 2027, and the LAST column
    is whichever year is currently the far end of CBR's rolling horizon --
    that's what 'rate_neutral' means here, not a fixed index."""
    rate_row = _extract_row(forecast_text, "Ключевая ставка, в среднем за год")
    infl_row = _extract_row(forecast_text, "Инфляция, в %, декабрь к декабрю")
    return {
        "rate_2026_mid": rate_row[1] if len(rate_row) > 1 else None,
        "rate_2027_mid": rate_row[2] if len(rate_row) > 2 else None,
        "rate_neutral": rate_row[-1] if len(rate_row) > 3 else None,
        "inflation_forecast_2026": infl_row[1] if len(infl_row) > 1 else None,
    }


# --- top-level fetch ----------------------------------------------------------

def fetch_meeting(meeting_date: str, is_anchor: bool) -> dict[str, Any]:
    """Fetch documents for one CBR meeting in priority order, STOPPING at the
    first empty/failed fetch for the three documents that genuinely form a
    dependency chain: press release --> Nabiullina speech --> резюме
    обсуждения. Rationale: if the press release itself isn't up yet, nothing
    downstream of it can be either -- no point burning requests probing for
    documents that can't exist yet.

    Forecast PDF is NOT gated behind résюме despite being 4th in the spec's
    numbered list -- verified empirically that the forecast link is on the
    PRESS RELEASE page itself, published same-day (confirmed: fetched the
    July 24 forecast immediately after the meeting, weeks before résюме was
    due). Gating it behind résюме's ~2-week-later availability would silently
    lose same-day forecast data for the current anchor meeting for two
    weeks -- a real regression caught by testing this against live data, not
    a hypothetical. comment_text genuinely IS ~2-week-later timing (checked:
    absent from both a fresh anchor meeting's press page and an old one's,
    only appears via the materials index once actually published), so it
    stays gated on being found there, independent of résюме's own gate.

    Idempotent/incremental: a later call re-fetching the same date only adds
    fields for documents that have since been published -- db.upsert_cbr_meeting
    merges onto the existing row rather than overwriting with the new (partial)
    dict, so this never erases something an earlier call already found.
    """
    ddmmyyyy = f"{meeting_date[8:10]}.{meeting_date[5:7]}.{meeting_date[0:4]}"
    index_html = _get(MATERIALS_INDEX_URL).text
    _snapshot(meeting_date, "materials_index.html", index_html)
    urls = _parse_materials_index(index_html).get(ddmmyyyy, {})

    out: dict[str, Any] = {
        "meeting_date": meeting_date,
        "is_anchor": 1 if is_anchor else 0,
        "fetched_on": date.today().isoformat(),
    }

    # 1. Press release -- stop here if it's not up yet.
    if not urls.get("press_release_url"):
        return out
    try:
        html = _get(urls["press_release_url"]).text
    except requests.RequestException:
        return out
    _snapshot(meeting_date, "press_release.html", html)
    text = _html_to_text(html)
    if not text:
        return out
    out["press_release_text"] = text
    before, after, change = extract_rate_decision(text)
    out["rate_before"], out["rate_after"], out["rate_change_bp"] = before, after, change

    # Forecast PDF (anchor only): discovered from THIS press release page,
    # not the materials index -- it's there same-day. Falls back to the
    # materials index link if the press page's own regex somehow misses it.
    if is_anchor:
        forecast_m = _FORECAST_LINK_RE.search(html)
        forecast_url = (BASE + forecast_m.group(1)) if forecast_m else urls.get("forecast_pdf_url")
        if forecast_url:
            try:
                content = _get(forecast_url).content
                _snapshot(meeting_date, "forecast.pdf", content)
                forecast_text = _pdf_to_text(content)
                if forecast_text:
                    out["forecast_summary"] = forecast_text[:4000]
                    out.update(extract_forecast_ranges(forecast_text))
            except requests.RequestException:
                pass

    # 2. Nabiullina speech -- stop here if not found.
    speech_url = urls.get("speech_url")
    if not speech_url:
        dd, mm, yyyy = ddmmyyyy.split(".")
        month_names = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                        "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        query = f"Заявление Председателя {int(dd)} {month_names[int(mm)]} {yyyy}"
        try:
            speech_url = _site_search_event_url(query)
        except requests.RequestException:
            speech_url = None
    if not speech_url:
        return out
    try:
        html = _get(speech_url).text
    except requests.RequestException:
        return out
    _snapshot(meeting_date, "speech.html", html)
    speech_text = _html_to_text(html)
    if not speech_text:
        return out
    out["nabiullina_speech_text"] = speech_text

    # 3. Резюме обсуждения -- stop here if not published yet (~2 weeks lag).
    if not urls.get("resume_url"):
        return out
    try:
        html = _get(urls["resume_url"]).text
    except requests.RequestException:
        return out
    _snapshot(meeting_date, "resume.html", html)
    resume_text = _html_to_text(html)
    if not resume_text:
        return out
    out["resume_text"] = resume_text

    # Комментарий к прогнозу -- anchor-only, same ~2-week timing as резюме,
    # not part of the spec's numbered priority chain so fetched independent
    # of whether the forecast step above succeeded.
    if is_anchor and urls.get("comment_url"):
        try:
            comment_url = urls["comment_url"]
            if comment_url.endswith(".pdf"):
                content = _get(comment_url).content
                _snapshot(meeting_date, "comment.pdf", content)
                comment_text = _pdf_to_text(content)[:8000]
            else:
                chtml = _get(comment_url).text
                _snapshot(meeting_date, "comment.html", chtml)
                comment_text = _html_to_text(chtml)[:8000]
            if comment_text:
                out["comment_text"] = comment_text
        except requests.RequestException:
            pass

    return out


# --- release comparison (meeting-over-meeting diff) --------------------------

def _midpoint(s: str | None) -> float | None:
    """"14,5-14,6" / "14.5-14.6" -> 14.55. None on anything unparseable --
    callers treat a None midpoint as "no shift computable", not zero.

    Normalises en-dash/em-dash to ASCII hyphen before splitting -- the
    stored range strings come straight from _extract_row()'s PDF-text
    capture (_NUM_TOKEN_RE includes both dash types as part of the token,
    matching the source PDF's own typography), so a range like "14,5–14,6"
    uses U+2013, not "-". Splitting on ASCII "-" alone silently returned a
    single-element list for every real range and threw ValueError inside
    the try/except -- forecast_2026_shift_bp/forecast_2027_shift_bp came
    back None for every meeting until this was caught by checking the
    actual stored diff rows, not just that the function didn't crash.
    """
    if not s:
        return None
    normalised = s.replace(",", ".").replace("–", "-").replace("—", "-")
    parts = normalised.split("-")
    try:
        return (float(parts[0]) + float(parts[-1])) / 2
    except (ValueError, IndexError):
        return None


def compute_diff(con, meeting_date: str) -> dict[str, Any]:
    """Compare this meeting to a prior one. Returns {} for a meeting with no
    qualifying prior (the dataset's first meeting).

    Which "prior" depends on whether THIS meeting is itself an anchor:
    for an anchor meeting, both the rate-pace comparison AND the forecast
    comparison use the prior ANCHOR -- anchor-to-anchor is the meaningful
    cadence for a rate-path narrative (this is what the worked spec example
    computes: 24 July's pace is measured against 24 April's 50bp step, not
    19 June's -- even though June's own step also happened to be 25bp,
    comparing to June would read as "same" pace, not "slowed", which
    contradicts the example's own numbers). For a non-anchor meeting, the
    immediately-previous meeting is used (there's no forecast to shift
    against anyway, and CBR's schedule alternates anchor/non-anchor so the
    immediately-previous meeting for a non-anchor IS the last anchor in
    practice -- the distinction only bites for anchor meetings).

    Does not attempt new_risk_factors/removed_risk_factors extraction --
    that needs comparing free-text risk-paragraph language between two
    releases, which isn't a deterministic parse the way pace/forecast-shift
    are. Left as NULL rather than faked; a future LLM-assisted pass (or the
    cbr-rate-analysis skill) is the right place for that, not this function.
    """
    this = db.get_cbr_meeting(con, meeting_date)
    if not this:
        return {}
    this = dict(this)

    immediate_prior = con.execute(
        "SELECT * FROM cbr_meeting WHERE meeting_date < ? "
        "AND rate_after IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1",
        (meeting_date,)).fetchone()
    if not immediate_prior:
        return {}
    immediate_prior = dict(immediate_prior)

    prior_anchor_row = con.execute(
        "SELECT * FROM cbr_meeting WHERE meeting_date < ? "
        "AND is_anchor=1 AND rate_after IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1",
        (meeting_date,)).fetchone()
    prior_anchor_row = dict(prior_anchor_row) if prior_anchor_row else None

    prior = prior_anchor_row if (this.get("is_anchor") and prior_anchor_row) else immediate_prior

    diff: dict[str, Any] = {
        "meeting_date": meeting_date,
        "prior_meeting_date": prior["meeting_date"],
        "rate_change_bp": this.get("rate_change_bp"),
    }

    this_change = this.get("rate_change_bp") or 0
    prior_change = prior.get("rate_change_bp") or 0
    if abs(this_change) > abs(prior_change):
        diff["pace_change"] = "accelerated"
    elif abs(this_change) < abs(prior_change):
        diff["pace_change"] = "slowed"
    elif this_change * prior_change < 0:
        diff["pace_change"] = "reversed"
    else:
        diff["pace_change"] = "same"

    prior_anchor = con.execute(
        "SELECT * FROM cbr_meeting WHERE meeting_date < ? "
        "AND is_anchor=1 AND rate_2026_mid IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1",
        (meeting_date,)).fetchone()
    if prior_anchor and this.get("rate_2026_mid"):
        prior_anchor = dict(prior_anchor)
        this_2026, prior_2026 = _midpoint(this.get("rate_2026_mid")), _midpoint(prior_anchor.get("rate_2026_mid"))
        this_2027, prior_2027 = _midpoint(this.get("rate_2027_mid")), _midpoint(prior_anchor.get("rate_2027_mid"))
        if this_2026 is not None and prior_2026 is not None:
            diff["forecast_2026_shift_bp"] = round((this_2026 - prior_2026) * 100)
        if this_2027 is not None and prior_2027 is not None:
            diff["forecast_2027_shift_bp"] = round((this_2027 - prior_2027) * 100)

    forecast_shift = diff.get("forecast_2026_shift_bp") or 0
    if diff["pace_change"] == "slowed" or forecast_shift > 25:
        diff["portfolio_signal"] = "shorten_duration"
        diff["signal_magnitude"] = "strong" if forecast_shift > 75 else "moderate"
        diff["tone_shift"] = "more_hawkish"
    elif diff["pace_change"] == "accelerated" or forecast_shift < -25:
        diff["portfolio_signal"] = "extend_duration"
        diff["signal_magnitude"] = "strong" if forecast_shift < -75 else "moderate"
        diff["tone_shift"] = "more_dovish"
    else:
        diff["portfolio_signal"] = "neutral"
        diff["signal_magnitude"] = "weak"
        diff["tone_shift"] = "neutral"

    diff["diff_note_ru"] = generate_diff_note(diff, this, prior)
    diff["computed_on"] = date.today().isoformat()
    return diff


def generate_diff_note(diff: dict, this: dict, prior: dict) -> str:
    """Template-generated Russian note -- deliberately no LLM call here, the
    diff dict is structured enough for a fixed template. `prior` is the
    immediately-previous meeting (for the pace sentence); the forecast-shift
    sentences use `diff`'s already-computed bp deltas (vs the prior ANCHOR),
    not `prior` directly."""
    parts = []

    pace_map = {
        "slowed": f'Шаг снижения замедлился: {abs(prior.get("rate_change_bp") or 0)}бп → {abs(this.get("rate_change_bp") or 0)}бп.',
        "accelerated": f'Шаг снижения ускорился: {abs(prior.get("rate_change_bp") or 0)}бп → {abs(this.get("rate_change_bp") or 0)}бп.',
        "same": f'Шаг снижения не изменился: {abs(this.get("rate_change_bp") or 0)}бп.',
        "reversed": "Направление изменения ставки изменилось.",
    }
    parts.append(pace_map.get(diff.get("pace_change", ""), ""))

    shift_26 = diff.get("forecast_2026_shift_bp")
    shift_27 = diff.get("forecast_2027_shift_bp")
    if shift_26 is not None:
        direction = "повышен" if shift_26 > 0 else "снижен"
        parts.append(
            f'Прогноз средней ставки на 2026 г. {direction} на {abs(shift_26)}бп '
            f'(теперь {this.get("rate_2026_mid", "?")}%).')
    if shift_27 is not None:
        direction = "повышен" if shift_27 > 0 else "снижен"
        parts.append(
            f'Прогноз на 2027 г. {direction} на {abs(shift_27)}бп '
            f'(теперь {this.get("rate_2027_mid", "?")}%).')

    signal_map = {
        "shorten_duration": "⚡ Сигнал портфелю: сокращать дюрацию, рассмотреть флоатеры.",
        "extend_duration": "✅ Сигнал портфелю: удлинять дюрацию, фиксировать длинный купон.",
        "neutral": "→ Сигнал портфелю: без изменений.",
    }
    parts.append(signal_map.get(diff.get("portfolio_signal", "neutral"), ""))

    return " ".join(p for p in parts if p)


def store_meeting(con, result: dict[str, Any]) -> dict[str, Any]:
    """Single choke point for writing a fetch result: upsert the meeting row,
    then compute and store its diff against the prior meeting/anchor. Every
    fetch entry point (CLI --fetch/--fetch-all, the cron functions below)
    goes through this instead of calling db.upsert_cbr_meeting directly, so
    the diff is always current without a separate manual step."""
    db.init_cbr(con)
    db.init_cbr_diff(con)
    db.upsert_cbr_meeting(con, **result)
    diff = compute_diff(con, result["meeting_date"])
    if diff:
        db.upsert_cbr_meeting_diff(con, **diff)
    return diff


# --- cron entry points --------------------------------------------------------

def _schedule_lookup() -> dict[str, dict]:
    return {m["date"]: m for m in CBR_MEETINGS_2026}


def check_and_fetch_today() -> dict | None:
    """Fast no-op unless today is a meeting date, a résumé date, or +14 days
    past an anchor meeting. Meant for the daily_run.sh cron slot."""
    from .store import db

    today = date.today().isoformat()
    schedule = _schedule_lookup()

    meeting = schedule.get(today)
    action = None
    target_date = today
    if meeting:
        action = "meeting"
    else:
        for m in CBR_MEETINGS_2026:
            if m["resume_date"] == today:
                action, target_date = "resume", m["date"]
                meeting = m
                break
            if m["anchor"]:
                comment_due = (date.fromisoformat(m["date"]) + timedelta(days=14)).isoformat()
                if comment_due == today:
                    action, target_date = "comment", m["date"]
                    meeting = m
                    break
    if not action:
        return None

    result = fetch_meeting(target_date, meeting["anchor"])

    con = db.connect()
    db.init_db(con)
    store_meeting(con, result)
    # health logging: today is a scheduled meeting/résюме/comment date (that's
    # the only way `action` gets set above), so a missing document at this
    # point means the fetch chain stalled, not that it's merely too early.
    if action == "meeting" and "press_release_text" not in result:
        log_health(con, 'cbr', 'ingest', 'warn', 'CBR_FETCH_FAIL',
                   f'press release not found for scheduled meeting {target_date}')
    elif action == "resume" and "resume_text" not in result:
        log_health(con, 'cbr', 'ingest', 'warn', 'CBR_STALE',
                   f'резюме обсуждения not yet published for meeting {target_date}')
    elif action == "comment" and "comment_text" not in result:
        log_health(con, 'cbr', 'ingest', 'warn', 'CBR_STALE',
                   f'комментарий к прогнозу not yet published for meeting {target_date}')
    con.close()
    return {"action": action, "meeting_date": target_date, "result": result}


def fetch_today_meeting() -> dict | None:
    """Dedicated meeting-day cron slot (14:00 MSK, after the 13:30 press
    release). Only acts if today is actually a scheduled meeting date.
    Fetches press release + speech (+ forecast if anchor) via fetch_meeting's
    normal priority chain -- résюме/comment naturally come back empty this
    same day (published ~2 weeks later) and are picked up by the separate
    résюме-day cron slot instead."""
    today = date.today().isoformat()
    meeting = _schedule_lookup().get(today)
    if not meeting:
        return None
    result = fetch_meeting(today, meeting["anchor"])
    con = db.connect()
    db.init_db(con)
    store_meeting(con, result)
    con.close()
    return result


def fetch_today_resume() -> dict | None:
    """Dedicated résюме-day cron slot (10:00 MSK). Only acts if today matches
    a scheduled resume_date. Re-runs the same fetch_meeting priority chain
    for that meeting's date -- press/speech are already stored from the
    meeting-day fetch (upsert merges, doesn't overwrite), so this call's
    real job is picking up résюме (and, for anchor meetings, the comment)."""
    today = date.today().isoformat()
    meeting = next((m for m in CBR_MEETINGS_2026 if m["resume_date"] == today), None)
    if not meeting:
        return None
    result = fetch_meeting(meeting["date"], meeting["anchor"])
    con = db.connect()
    db.init_db(con)
    store_meeting(con, result)
    con.close()
    return result


def fetch_all_meetings() -> list[dict]:
    """Backfill every past meeting in CBR_MEETINGS_2026 (meeting_date <=
    today). Used by `hybond cbr --fetch-all`."""
    today = date.today()
    con = db.connect()
    db.init_db(con)
    results = []
    for m in CBR_MEETINGS_2026:
        if date.fromisoformat(m["date"]) > today:
            continue
        result = fetch_meeting(m["date"], m["anchor"])
        store_meeting(con, result)
        results.append(result)
    con.close()
    return results


def compute_all_diffs() -> list[dict]:
    """Recompute diffs for every meeting that currently has one computable
    (i.e. has a prior meeting with rate_after set) -- used by `hybond cbr
    --diff-all` to backfill/refresh diffs without re-fetching documents."""
    con = db.connect()
    db.init_db(con)
    db.init_cbr(con)
    db.init_cbr_diff(con)
    diffs = []
    for m in CBR_MEETINGS_2026:
        row = db.get_cbr_meeting(con, m["date"])
        if not row or not row["rate_after"]:
            continue
        diff = compute_diff(con, m["date"])
        if diff:
            db.upsert_cbr_meeting_diff(con, **diff)
            diffs.append(diff)
    con.close()
    return diffs


def generate_cron_entries(project_root: str) -> str:
    """Two crontab lines per 2026 meeting date, generated from
    CBR_MEETINGS_2026 rather than hand-written so adding a year's schedule
    is a one-line data change:

      - meeting_date, 14:00 MSK (11:00 UTC) -- after the 13:30 press
        release; fetches press release + speech (+ forecast if anchor)
        via fetch_today_meeting().
      - resume_date, 10:00 MSK (07:00 UTC) -- fetches резюме (+ комментарий
        if anchor) via fetch_today_resume().

    Only dates still in the future (relative to today) are emitted -- past
    ones belong to `hybond cbr --fetch-all`, not a cron entry that will
    never fire again this year.
    """
    today = date.today()
    lines = []
    for m in CBR_MEETINGS_2026:
        meeting_d = date.fromisoformat(m["date"])
        if meeting_d >= today:
            y, mo, d = m["date"].split("-")
            log = f'data/logs/cbr_{y}{mo}{d}_meeting.log'
            lines.append(
                f'0 11 {int(d)} {int(mo)} *   cd "{project_root}" && '
                f'PYTHONPATH=src python3 -c "from hybond.cbr_tracker import fetch_today_meeting; '
                f'fetch_today_meeting()" >> {log} 2>&1'
            )
        resume_d = date.fromisoformat(m["resume_date"])
        if resume_d >= today:
            y, mo, d = m["resume_date"].split("-")
            log = f'data/logs/cbr_{y}{mo}{d}_resume.log'
            lines.append(
                f'0 7 {int(d)} {int(mo)} *   cd "{project_root}" && '
                f'PYTHONPATH=src python3 -c "from hybond.cbr_tracker import fetch_today_resume; '
                f'fetch_today_resume()" >> {log} 2>&1'
            )
    return "\n".join(lines)
