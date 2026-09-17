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

Forecast-table parsing is text extraction (pypdf) off the "Ключевая ставка,
в среднем за год" and "Инфляция..." rows. Two traps, both seen in every
real document, not guessed at:

  * The PDF text layer fuses the footnote-reference digit onto the number
    it annotates: "14,5–14,61", "17,51", "19,21". Checked against all 11
    forecasts published since 16.02.2024 (2026-09-17): the CBR prints the
    key-rate average to exactly one decimal, and every second decimal digit
    is that footnote marker. parse_rate_range() strips it; forecast_summary
    keeps the raw text for anyone who wants to re-check.
  * The table's columns are years, and which years depends on the vintage:
    3 forecast years in February, 4 in July, and the first column moves
    every January. Columns are mapped to the years printed in the table's
    own header row, never by position -- a positional "index 1 is 2026"
    would silently file the February 2027 forecast's 2027 range as 2026.

The meeting schedule comes from the CBR calendar (cbr.ru/dkp/cal_mp/),
stored in cbr_schedule by refresh_schedule(); CBR_MEETINGS_2026 below is
only the fallback for a store that has never fetched it.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .health import log_health, log_health_once_today
from .store import db

MOEX_STYLE_UA = {"User-Agent": "hybond/0.1 (research)"}
BASE = "https://cbr.ru"
MATERIALS_INDEX_URL = f"{BASE}/dkp/mp_dec/decision_key_rate/"
SEARCH_URL = f"{BASE}/search/"
RAW_DIR = Path("data/raw/cbr")

CALENDAR_URL = f"{BASE}/dkp/cal_mp/"

# Fallback 2026 schedule, used only until refresh_schedule() has stored the
# CBR calendar (get_schedule() merges the two, the calendar winning). Four
# résumé dates here were a day later than the calendar's own (01.04, 01.07,
# 05.08, 23.09) -- the fetch then ran a day late rather than missing.
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


_RANGE_SPLIT_RE = re.compile(r"\s*[–—-]\s*")
_ONE_NUMBER_RE = re.compile(r"^\d{1,3}(?:[.,]\d+)?$")


def _strip_footnote(num: str) -> str:
    """'14,61' -> '14,6'. The CBR prints key-rate averages to one decimal;
    a second decimal digit is the footnote marker the PDF text layer fused
    on (see module docstring). Integers and one-decimal values pass through."""
    m = re.fullmatch(r"(\d{1,3})[.,](\d)(\d+)", num)
    return f"{m.group(1)},{m.group(2)}" if m else num


def parse_rate_range(token: str | None) -> tuple[float, float] | None:
    """A CBR key-rate cell -> (low, high) in percent, footnote digits stripped.

    Accepts every spelling seen in real documents: '14,5–14,61', '13,5-15,51',
    '19,0 – 21,0', '14,0 –16,0', '17,51', '14.5-14.6'. A single value comes
    back as (v, v). None for anything that is not one or two numbers."""
    if not token:
        return None
    parts = [p for p in _RANGE_SPLIT_RE.split(token.strip()) if p]
    if not 1 <= len(parts) <= 2 or not all(_ONE_NUMBER_RE.match(p) for p in parts):
        return None
    vals = [float(_strip_footnote(p).replace(",", ".")) for p in parts]
    return (min(vals), max(vals))


def format_rate_range(rng: tuple[float, float] | None, dash: str = "–") -> str | None:
    """(14.5, 14.6) -> '14,5–14,6'; (17.5, 17.5) -> '17,5'. The spelling the
    cbr_meeting range columns have always stored, footnote-free; `dash` lets
    a caller keep the source document's own separator."""
    if rng is None:
        return None
    lo, hi = (f"{v:.1f}".replace(".", ",") for v in rng)
    return lo if lo == hi else f"{lo}{dash}{hi}"


def forecast_table_years(forecast_text: str) -> list[int]:
    """The years heading the forecast table, in column order -- the first is
    the prior-year actual ('2025 (факт)'). Read from the text between the
    table title and the key-rate row. [] when no header is present."""
    idx = forecast_text.find("Ключевая ставка, в среднем за год")
    if idx == -1:
        return []
    head_start = forecast_text.rfind("Основные параметры прогноза", 0, idx)
    head = forecast_text[head_start if head_start != -1 else 0:idx]
    first_row = head.find("Инфляция")
    if first_row != -1:
        head = head[:first_row]
    years: list[int] = []
    for y in re.findall(r"\b(20\d\d)\b", head):
        if int(y) not in years:
            years.append(int(y))
    return years


def forecast_rate_by_year(forecast_text: str) -> dict[int, tuple[float, float]]:
    """{year: (low, high)} for the key-rate row, every column including the
    prior-year actual, mapped through forecast_table_years(). {} when the
    header years and the row's cells do not line up -- better no numbers
    than numbers filed under the wrong year."""
    years = forecast_table_years(forecast_text)
    cells = _extract_row(forecast_text, "Ключевая ставка, в среднем за год")
    if not years or len(years) != len(cells):
        return {}
    out = {}
    for y, cell in zip(years, cells):
        rng = parse_rate_range(cell)
        if rng is not None:
            out[y] = rng
    return out


_MONTHS_GEN = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
               "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_REST_OF_YEAR_RE = re.compile(
    r"с\s+1\s+января\s+по\s+(\d{1,2})\s+([а-я]+)\s+(\d{4})\s+года\s+средняя\s+ключевая\s+ставка\s+"
    r"равна\s+(\d{1,2}[.,]\d)\s*%\s*,?\s*с\s+(\d{1,2})\s+([а-я]+)\s+до\s+конца\s+(\d{4})\s+года\s+"
    r"средняя\s+ключевая\s+ставка\s+прогнозируется\s+в\s+диапазоне\s+"
    r"(\d{1,2}[.,]\d)\s*[–—-]\s*(\d{1,2}[.,]\d)\s*%")


def forecast_rest_of_year(forecast_text: str) -> dict | None:
    """The forecast's footnote on the remainder of the current year:
    «С учетом того, что с 1 января по 26 июля 2026 года средняя ключевая
    ставка равна 15,0%, с 27 июля до конца 2026 года средняя ключевая ставка
    прогнозируется в диапазоне 13,7–14,0%». Present in every forecast since
    16.02.2024; written with a hyphen until February 2025 and an en dash
    after. Returns {year, lo, hi, ytd_avg, rest_from} or None."""
    flat = re.sub(r"\s+", " ", forecast_text)
    m = _REST_OF_YEAR_RE.search(flat)
    if not m:
        return None
    month = _MONTHS_GEN.get(m.group(6))
    if not month:
        return None
    year = int(m.group(7))
    return {
        "year": year,
        "lo": float(m.group(8).replace(",", ".")),
        "hi": float(m.group(9).replace(",", ".")),
        "ytd_avg": float(m.group(4).replace(",", ".")),
        "rest_from": f"{year:04d}-{month:02d}-{int(m.group(5)):02d}",
    }


def extract_forecast_ranges(forecast_text: str) -> dict[str, str | None]:
    """The cbr_meeting range columns for one forecast PDF's text.

    With the table header present (every real forecast), each column is
    filed under the year printed above it: rate_2026_mid .. rate_2029_mid
    get their own year's range (footnote-free, '14,5–14,6'), and a year the
    forecast does not cover stays None. rate_neutral is the last column,
    whichever year that is. inflation_forecast_2026 is the 2026 column of the
    inflation row.

    Header-less text (a hand-trimmed row, as older tests feed it) falls back
    to the historical positional reading -- index 1 = 2026, index 2 = 2027 --
    because nothing else is knowable from it."""
    rate_row = _extract_row(forecast_text, "Ключевая ставка, в среднем за год")
    infl_row = _extract_row(forecast_text, "Инфляция, в %, декабрь к декабрю")
    years = forecast_table_years(forecast_text)

    def cell(row: list[str], i: int) -> str | None:
        if len(row) <= i:
            return None
        dash = "-" if "-" in row[i] and "–" not in row[i] else "–"
        return format_rate_range(parse_rate_range(row[i]), dash)

    if years and len(years) == len(rate_row):
        col = {y: i for i, y in enumerate(years)}
        out = {f"rate_{y}_mid": (cell(rate_row, col[y]) if y in col and col[y] > 0 else None)
               for y in (2026, 2027, 2028, 2029)}
        out["rate_neutral"] = cell(rate_row, len(rate_row) - 1) if len(rate_row) > 3 else None
        infl_col = col.get(2026)
        out["inflation_forecast_2026"] = (cell(infl_row, infl_col)
                                          if infl_col and len(infl_row) == len(years) else None)
        return out
    return {
        "rate_2026_mid": cell(rate_row, 1),
        "rate_2027_mid": cell(rate_row, 2),
        "rate_neutral": cell(rate_row, len(rate_row) - 1) if len(rate_row) > 3 else None,
        "inflation_forecast_2026": cell(infl_row, 1),
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
        # doc_type -> URL of every document found; not cbr_meeting columns,
        # store_meeting() files them into cbr_document.
        "document_urls": {},
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
    out["document_urls"]["press_release"] = urls["press_release_url"]
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
                    out["forecast_text"] = forecast_text  # full text: the «остаток года» footnote
                    out["document_urls"]["forecast"] = forecast_url
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
    out["document_urls"]["statement"] = speech_url

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
    out["document_urls"]["summary"] = urls["resume_url"]

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
                out["document_urls"]["comment"] = comment_url
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
    _store_documents(con, result)
    diff = compute_diff(con, result["meeting_date"])
    if diff:
        db.upsert_cbr_meeting_diff(con, **diff)
    return diff


def _store_documents(con, result: dict[str, Any]) -> None:
    """File the documents a fetch found into cbr_document, and a forecast's
    key-rate ranges into rate_expectation -- so an anchor meeting's forecast
    joins the expectations history the same day, not at the next
    `hybond expectations` run. Summary bodies are left to that run:
    _html_to_text() stops before the options it needs."""
    urls = result.get("document_urls") or {}
    if not urls:
        return
    from . import expectations  # lazy: expectations imports this module
    db.init_rates(con)
    meeting = result["meeting_date"]
    fetched = result.get("fetched_on")
    texts = {"press_release": result.get("press_release_text"),
             "statement": result.get("nabiullina_speech_text")}
    for kind, url in urls.items():
        if url:
            db.upsert_cbr_document(con, meeting_date=meeting, doc_type=kind, url=url,
                                   published=meeting if kind in ("press_release", "statement", "forecast") else None,
                                   text=texts.get(kind), fetched_on=fetched)
    if urls.get("forecast") and result.get("forecast_text"):
        expectations.store_forecast_text(con, meeting, meeting, urls["forecast"], result["forecast_text"])
    con.commit()


# --- schedule: the CBR calendar ----------------------------------------------

_CAL_DAY_SPLIT = '<div class="main-events_day">'
_CAL_DATE_RE = re.compile(
    r'<div class="date[^"]*">\s*(\d{1,2})(?:&nbsp;|\s)+([а-я]+)(?:&nbsp;|\s)+(\d{4})(?:&nbsp;|\s)+года')
_HREF_RE = re.compile(r'href="([^"]+)"[^>]*>([^<]*)<')


def parse_calendar(html: str) -> list[dict]:
    """Every dated event on cbr.ru/dkp/cal_mp/ (all year tabs at once):
    [{event_date, kind, is_anchor, url}], kind in meeting/summary/comment.

    A day block that names «Заседание Совета директоров» is a meeting; it is
    an anchor (опорное) meeting when the same block lists «Среднесрочный
    прогноз» -- the calendar prints that line, linked or not, only for
    anchor meetings. «Резюме обсуждения» and «Комментарий к среднесрочному
    прогнозу» are separate later days. URLs are present once published."""
    import html as html_mod
    events: list[dict] = []
    for block in html.split(_CAL_DAY_SPLIT)[1:]:
        m = _CAL_DATE_RE.search(block)
        month = _MONTHS_GEN.get(m.group(2)) if m else None
        if not m or not month:
            continue
        day = f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
        text = html_mod.unescape(_TAG_RE.sub(" ", block)).replace("\xa0", " ")
        links = {html_mod.unescape(t).replace("\xa0", " ").strip(): u for u, t in _HREF_RE.findall(block)}

        def link(prefix: str, links: dict[str, str] = links) -> str | None:
            for label, u in links.items():
                if label.startswith(prefix):
                    return u if u.startswith("http") else BASE + u
            return None

        if "Заседание Совета директоров" in text:
            events.append({"event_date": day, "kind": "meeting",
                           "is_anchor": "Среднесрочный прогноз" in text, "url": link("Пресс-релиз")})
        if "Резюме обсуждения" in text:
            events.append({"event_date": day, "kind": "summary", "is_anchor": False,
                           "url": link("Резюме обсуждения")})
        if "Комментарий к" in text:
            events.append({"event_date": day, "kind": "comment", "is_anchor": False,
                           "url": link("Комментарий к")})
    return events


def schedule_from_events(events: list[dict]) -> list[dict]:
    """Calendar events -> [{date, anchor, resume_date}] meetings, oldest
    first. A meeting's résumé date is the first summary event after it and
    before the next meeting; None when the calendar has not listed it."""
    meetings = sorted((e for e in events if e["kind"] == "meeting"), key=lambda e: e["event_date"])
    summaries = sorted(e["event_date"] for e in events if e["kind"] == "summary")
    out = []
    for i, m in enumerate(meetings):
        nxt = meetings[i + 1]["event_date"] if i + 1 < len(meetings) else "9999-12-31"
        resume = next((d for d in summaries if m["event_date"] < d < nxt), None)
        out.append({"date": m["event_date"], "anchor": bool(m["is_anchor"]), "resume_date": resume})
    return out


def get_schedule(con=None) -> list[dict]:
    """The meeting schedule every entry point uses: the stored CBR calendar
    (cbr_schedule), with CBR_MEETINGS_2026 filling any date the store does
    not have yet -- so a fresh install, or a store that has never fetched
    the calendar, still has the 2026 dates. Oldest first."""
    by_date = {m["date"]: dict(m) for m in CBR_MEETINGS_2026}
    if con is not None and db.has_table(con, "cbr_schedule"):
        for m in schedule_from_events(db.cbr_schedule_events(con)):
            fallback = by_date.get(m["date"], {})
            by_date[m["date"]] = {"date": m["date"], "anchor": m["anchor"],
                                  "resume_date": m["resume_date"] or fallback.get("resume_date")}
    return sorted(by_date.values(), key=lambda m: m["date"])


def refresh_schedule(con) -> int:
    """Fetch the CBR calendar and store every event it lists. Returns the
    number of events stored; raises on a network error or an empty parse
    (a layout change must be loud, not an empty schedule)."""
    html = _get(CALENDAR_URL).text
    events = parse_calendar(html)
    if not any(e["kind"] == "meeting" for e in events):
        raise ValueError("CBR calendar parse found no meetings; layout changed?")
    db.init_rates(con)
    today = date.today().isoformat()
    for e in events:
        db.upsert_cbr_schedule_event(con, e["event_date"], e["kind"], e["is_anchor"], e["url"], today)
    con.commit()
    return len(events)


# --- cron entry points --------------------------------------------------------

def _schedule_lookup(con=None) -> dict[str, dict]:
    return {m["date"]: m for m in get_schedule(con)}


def check_and_fetch_today() -> dict | None:
    """Fast no-op unless today is a meeting date, a résumé date, or +14 days
    past an anchor meeting. Meant for the daily_run.sh cron slot."""
    from .store import db

    today = date.today().isoformat()
    con = db.connect()
    db.init_db(con)
    schedule = get_schedule(con)
    con.close()
    by_date = {m["date"]: m for m in schedule}

    meeting = by_date.get(today)
    action = None
    target_date = today
    if meeting:
        action = "meeting"
    else:
        for m in schedule:
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


# How far back catch_up_missed_meetings() looks. A decision missed for longer
# than this is a manual `hybond cbr --fetch` job, not something to retry on
# every cron slot forever.
CATCH_UP_MEETING_DAYS = 60
CATCH_UP_DOCUMENT_DAYS = 30
# The CBR calendar: «Предполагаемое время публикации пресс-релиза — 13:30 по
# московскому времени». After this (machine-local, MSK) a meeting held today
# counts as due, so the afternoon cron slot stores the decision before the
# universe run recomputes AEY -- not a slot later.
RELEASE_TIME_MSK = (13, 45)


def missed_meetings(con, today: date, now: datetime | None = None) -> list[dict]:
    """Scheduled meetings the store is missing something for, as
    [{date, anchor, missing}] with missing in decision/resume/comment:

      decision -- the meeting is past (within CATCH_UP_MEETING_DAYS), or is
                  today and RELEASE_TIME_MSK has passed (`now`, default the
                  machine clock), and cbr_meeting has no rate_after for it;
      resume   -- its résumé date is past (within CATCH_UP_DOCUMENT_DAYS)
                  and resume_text is empty;
      comment  -- an anchor meeting 14+ days past (within the same window)
                  with no comment_text.

    check_and_fetch_today() only acts on the exact calendar day, so a day
    the machine was off (5-13 Sep 2026 lost the 11.09 decision) is never
    revisited without this."""
    db.init_cbr(con)
    now = now or datetime.now()
    released_today = (now.hour, now.minute) >= RELEASE_TIME_MSK
    out = []
    for m in get_schedule(con):
        md = date.fromisoformat(m["date"])
        if md > today:
            continue
        row = db.get_cbr_meeting(con, m["date"])
        row = dict(row) if row else {}
        missing = None
        if (row.get("rate_after") is None and (today - md).days <= CATCH_UP_MEETING_DAYS
                and (md < today or released_today)):
            missing = "decision"
        elif (m["resume_date"] and not row.get("resume_text")
              and 0 <= (today - date.fromisoformat(m["resume_date"])).days <= CATCH_UP_DOCUMENT_DAYS):
            missing = "resume"
        elif (m["anchor"] and not row.get("comment_text")
              and 14 <= (today - md).days <= 14 + CATCH_UP_DOCUMENT_DAYS):
            missing = "comment"
        if missing:
            out.append({"date": m["date"], "anchor": m["anchor"], "missing": missing})
    return out


def catch_up_missed_meetings(today: date | None = None, now: datetime | None = None) -> list[dict]:
    """Re-fetch every meeting missed_meetings() reports and store what the
    CBR has published by now. Logs CBR_STALE when a past decision's press
    release still cannot be found. Returns [{date, missing, found}]."""
    today = today or date.today()
    con = db.connect()
    db.init_db(con)
    try:
        todo = missed_meetings(con, today, now)
        report = []
        for m in todo:
            result = fetch_meeting(m["date"], m["anchor"])
            store_meeting(con, result)
            field = {"decision": "press_release_text", "resume": "resume_text",
                     "comment": "comment_text"}[m["missing"]]
            found = field in result
            if m["missing"] == "decision" and not found:
                # once a day, not once per cron slot, for as long as it stays missing
                log_health_once_today(con, 'cbr', 'ingest', 'warn', 'CBR_STALE',
                                      f'decision for past meeting {m["date"]} still not found on cbr.ru')
            report.append({"date": m["date"], "missing": m["missing"], "found": found})
        return report
    finally:
        con.close()


def fetch_today_meeting() -> dict | None:
    """Dedicated meeting-day cron slot (14:00 MSK, after the 13:30 press
    release). Only acts if today is actually a scheduled meeting date.
    Fetches press release + speech (+ forecast if anchor) via fetch_meeting's
    normal priority chain -- résюме/comment naturally come back empty this
    same day (published ~2 weeks later) and are picked up by the separate
    résюме-day cron slot instead."""
    today = date.today().isoformat()
    con = db.connect()
    db.init_db(con)
    meeting = _schedule_lookup(con).get(today)
    if not meeting:
        con.close()
        return None
    result = fetch_meeting(today, meeting["anchor"])
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
    con = db.connect()
    db.init_db(con)
    meeting = next((m for m in get_schedule(con) if m["resume_date"] == today), None)
    if not meeting:
        con.close()
        return None
    result = fetch_meeting(meeting["date"], meeting["anchor"])
    store_meeting(con, result)
    con.close()
    return result


def fetch_all_meetings() -> list[dict]:
    """Backfill every past scheduled meeting (meeting_date <= today). Used by
    `hybond cbr --fetch-all`."""
    today = date.today()
    con = db.connect()
    db.init_db(con)
    results = []
    for m in get_schedule(con):
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
    for m in get_schedule(con):
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
    """Two crontab lines per meeting date, generated from get_schedule() (the
    stored CBR calendar, CBR_MEETINGS_2026 as fallback) rather than
    hand-written:

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
    con = db.connect()
    db.init_db(con)
    schedule = get_schedule(con)
    con.close()
    for m in schedule:
        meeting_d = date.fromisoformat(m["date"])
        if meeting_d >= today:
            y, mo, d = m["date"].split("-")
            log = f'data/logs/cbr_{y}{mo}{d}_meeting.log'
            lines.append(
                f'0 11 {int(d)} {int(mo)} *   cd "{project_root}" && '
                f'PYTHONPATH=src python3 -c "from hybond.cbr_tracker import fetch_today_meeting; '
                f'fetch_today_meeting()" >> {log} 2>&1'
            )
        resume_d = date.fromisoformat(m["resume_date"]) if m["resume_date"] else None
        if resume_d and resume_d >= today:
            y, mo, d = m["resume_date"].split("-")
            log = f'data/logs/cbr_{y}{mo}{d}_resume.log'
            lines.append(
                f'0 7 {int(d)} {int(mo)} *   cd "{project_root}" && '
                f'PYTHONPATH=src python3 -c "from hybond.cbr_tracker import fetch_today_resume; '
                f'fetch_today_resume()" >> {log} 2>&1'
            )
    return "\n".join(lines)
