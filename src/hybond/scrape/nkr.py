"""ratings.ru (НКР -- Национальные Кредитные Рейтинги) -- rating-database
lookup. Plain server-rendered HTML, no JS gate, no anti-bot: unlike
raexpert.ru (site search is JS/AJAX-driven, returns nothing via a bare POST)
and acra-ratings.ru (has a real AJAX endpoint, /local/ajax/get_rate_person.php,
but it silently ignores every filter param tried -- text, page, count all
return the same single cached-looking row regardless of input), НКР's
`/ratings/issuers/` page is a single static HTML response listing every
currently-rated issuer (255 rows as of 2026-07-31): name, rating, outlook,
ESG rating, sector, and rated-on date with a press-release link. No pagination
needed -- confirmed all 255 rows are present in one response, not client-side
paginated. НРА (ra-national.ru) was not investigated for a bulk listing this
pass since НКР alone already proved out end-to-end.

This is a bulk-fetch-then-cache source, same shape as
ingest/sources.fetch_emitent_map(): one request gets the whole table, matching
happens locally against it rather than a per-company network call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

BASE = "https://ratings.ru"
ISSUERS_URL = f"{BASE}/ratings/issuers/"
RAW_DIR = Path("data/raw/nkr")


@dataclass(frozen=True)
class NkrRating:
    slug: str
    name: str          # raw display name, e.g. 'ООО «Л-Старт»'
    name_core: str      # normalised core name for matching, e.g. 'Л-СТАРТ'
    rating: str | None    # converted to our ruXX convention, e.g. 'ruB' --
                           # None when is_withdrawn (see below)
    outlook: str
    sector: str
    rated_on: str | None      # ISO date
    press_release_url: str | None
    is_withdrawn: bool = False


class Backend(Protocol):
    def get(self, url: str) -> str: ...


class HttpBackend:
    def __init__(self, timeout: int = 30):
        import requests
        self._requests = requests
        self.timeout = timeout

    def get(self, url: str) -> str:
        r = self._requests.get(
            url, timeout=self.timeout,
            headers={"User-Agent": "hybond/0.1 (research)"})
        r.raise_for_status()
        return r.text


def _snapshot(name: str, text: str) -> Path:
    from datetime import date
    d = RAW_DIR / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def fetch_issuers_page(backend: Backend | None = None, snapshot: bool = True) -> str:
    backend = backend or HttpBackend()
    html = backend.get(ISSUERS_URL)
    if snapshot:
        _snapshot("issuers.html", html)
    return html


# --- parsing ----------------------------------------------------------------

_ROW_RE = re.compile(r'<tr id="bx_[^"]*"[^>]*>(.*?)</tr>', re.DOTALL)
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
_LINK_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]*)</a>')
_TAG_RE = re.compile(r'<[^>]+>')
_QUOTED_RE = re.compile(r'[«"]([^»"]+)[»"]')

# Legal-form tokens stripped for the no-quotes fallback (e.g. 'ПАО Сбербанк',
# 'Банк ВТБ ПАО') -- best-effort only, not needed for the vast majority of
# rows (248/255 already carry a quoted core name).
_LEGAL_FORM_RE = re.compile(
    r'\b(ООО|АО|ПАО|ЗАО|НАО|ПКО|ОАО|Банк|им\.)\b', re.IGNORECASE)


def _strip_tags(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


def normalize_name(raw: str) -> str:
    """Extract and normalise a company's core name for cross-source matching
    -- e.g. 'ООО «Л-Старт» ' and 'Общество с ограниченной ответственностью
    "Л-Старт"' (MOEX's emitent_title form) both normalise to 'Л-СТАРТ'."""
    m = _QUOTED_RE.search(raw)
    core = m.group(1) if m else _LEGAL_FORM_RE.sub("", raw)
    return " ".join(core.split()).upper()


def _to_ru_rating(nkr_rating: str) -> str:
    """NKR's own scale ('B.ru', 'BB-.ru', 'CC.ru') -> this codebase's
    'ruXX' convention (matches default_probs.DEFAULT_PROBS keys)."""
    return "ru" + nkr_rating.replace(".ru", "").strip()


def _is_withdrawn(rating_cell_text: str) -> bool:
    """NKR shows the literal text 'рейтинг отозван' in the rating column
    (instead of a letter grade) for issuers whose rating has been withdrawn
    -- e.g. contract lapsed, no longer rated. Confirmed live 2026-07-31
    (ООО «Баррель», ООО «Простая еда», ПАО «Кокс», АО «Почта России»).
    Treated the same as "genuinely unrated", not a real grade -- prefixing
    that string with 'ru' would otherwise produce nonsense like
    'ruрейтинг отозван'."""
    return "отозван" in rating_cell_text.lower()


def _to_iso_date(s: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def parse_issuers(html: str) -> list[NkrRating]:
    out = []
    for row_m in _ROW_RE.finditer(html):
        tds = _TD_RE.findall(row_m.group(1))
        if len(tds) < 6:
            continue
        name_link = _LINK_RE.search(tds[0])
        if not name_link:
            continue
        slug_url, name = name_link.groups()
        slug = slug_url.strip("/").split("/")[-1]
        name = name.strip()
        rating_cell = _strip_tags(tds[1])
        if not rating_cell:
            continue
        withdrawn = _is_withdrawn(rating_cell)
        outlook = _strip_tags(tds[2])
        sector = _strip_tags(tds[4])
        date_link = _LINK_RE.search(tds[5])
        if date_link:
            pr_url, date_str = date_link.groups()
            rated_on = _to_iso_date(date_str)
        else:
            pr_url, rated_on = None, None
        out.append(NkrRating(
            slug=slug, name=name, name_core=normalize_name(name),
            rating=None if withdrawn else _to_ru_rating(rating_cell),
            outlook=None if withdrawn else outlook, sector=sector,
            rated_on=rated_on,
            press_release_url=f"{BASE}{pr_url}" if pr_url else None,
            is_withdrawn=withdrawn))
    return out


def match_by_name(target_name: str, rows: list[NkrRating]) -> NkrRating | None:
    """Exact match on normalised core name -- deliberately not fuzzy/substring:
    unlike smartlab_news.py's short brand aliases (where a generic word would
    false-match constantly), full company core names are already distinctive,
    so exact-after-normalisation keeps the false-positive rate near zero at
    the cost of missing a few legal-form-only outliers (see normalize_name's
    no-quotes fallback)."""
    target_core = normalize_name(target_name)
    for row in rows:
        if row.name_core == target_core:
            return row
    return None


# =============================================================================
# Rating History Backfill task (2026-08-25): the bulk listing above only
# gives each issuer's CURRENT rating (255/258 issuers, one snapshot in
# time) -- this section adds the per-issuer PAST-actions trail (assigned/
# upgraded/downgraded/confirmed/withdrawn, one per historical press
# release), which the bulk page doesn't carry at all.
#
# Issuer discovery reuses fetch_issuers_page()/parse_issuers()/
# match_by_name() above rather than ratings.ru's own /search/ endpoint: a
# live search was tried first and dropped -- it returned zero "Эмитенты"
# results for a short/common name like 'АПРИ' (confirmed live), where the
# bulk listing + the already-proven normalize_name() core-name match found
# it immediately. One extra alias-stripping step is needed on top of
# normalize_name() for issuers whose `issuer.name` carries a trailing
# brand alias this project adds but ratings.ru's own name never has (e.g.
# 'Каршеринг Руссия (Делимобиль)' -> match against 'Каршеринг Руссия').
#
# Confirmed live against ПЛАЗА-Телеком (a tier=2 issuer already carrying 2
# rating rows in this project's DB from unrelated manual entry): the
# per-issuer page returned 4 actions whose 2 most recent dates/values
# reconciled exactly with those 2 existing rows.
# =============================================================================

SOURCE = "nkr_historical"
AGENCY = "НКР"

_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")

_ACTION_VERBS = {
    "присвоило": "assigned",
    "повысило": "upgraded",
    "понизило": "downgraded",
    "подтвердило": "confirmed",
    "отозвало": "withdrawn",
}

_OUTLOOK_RE_DASH = re.compile(r"прогноз\s*[—-]\s*([а-яё]+)", re.IGNORECASE)
_OUTLOOK_RE_PHRASE = re.compile(r"со?\s+([а-яё]+)\s+прогнозом", re.IGNORECASE)
# "со стабильным прогнозом" (instrumental case) -> "стабильный" (nominative),
# matching this project's own existing rating.outlook convention.
_OUTLOOK_NOMINATIVE = {
    "стабильным": "стабильный", "позитивным": "позитивный",
    "негативным": "негативный", "развивающимся": "развивающийся",
}


def find_issuer_slug(issuer_name: str, rows: list[NkrRating] | None = None,
                      backend: Backend | None = None) -> str | None:
    """Resolve an `issuer.name` value to its ratings.ru slug via the bulk
    listing (fetched fresh if `rows` isn't passed in -- callers backfilling
    many issuers in one run should fetch_issuers_page()/parse_issuers()
    once and pass `rows` through to avoid refetching the 258-row page per
    issuer)."""
    if rows is None:
        rows = parse_issuers(fetch_issuers_page(backend, snapshot=False))
    stripped = _TRAILING_PAREN_RE.sub("", issuer_name.strip())
    match = match_by_name(stripped, rows)
    return match.slug if match else None


def _extract_action(title: str) -> str | None:
    lower = title.lower()
    for verb, action in _ACTION_VERBS.items():
        if verb in lower:
            return action
    return None


def _extract_outlook(title: str) -> str | None:
    m = _OUTLOOK_RE_DASH.search(title) or _OUTLOOK_RE_PHRASE.search(title)
    if not m:
        return None
    raw = m.group(1).lower()
    return _OUTLOOK_NOMINATIVE.get(raw, raw)


class _RateItemsParser(HTMLParser):
    """Extracts every `.rate-item` block from an issuer page: rating value
    (`.rate-item__rating`), press-release title + href (`.rate-item__title`,
    an <a>), and action date (`.rate-item__date`)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._current: dict | None = None
        self._depth = 0
        self._capture: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div" and classes == ["rate-item"]:
            self._current = {"rating": "", "title": "", "date": "", "href": None}
            self._depth = 1
            return
        if self._current is None:
            return
        if tag == "div":
            self._depth += 1
        if "rate-item__rating" in classes:
            self._capture = "rating"
        elif "rate-item__title" in classes:
            self._capture = "title"
            self._current["href"] = attrs.get("href")
        elif "rate-item__date" in classes:
            self._capture = "date"

    def handle_endtag(self, tag):
        if self._current is None:
            return
        if self._capture and tag in ("p", "a"):
            self._capture = None
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                for k in ("rating", "title", "date"):
                    self._current[k] = " ".join(self._current[k].split())
                self.items.append(self._current)
                self._current = None

    def handle_data(self, data):
        if self._current is not None and self._capture:
            self._current[self._capture] += data


def fetch_issuer_ratings(slug: str, backend: Backend | None = None) -> list[dict]:
    """Fetch + parse the full rating-action history for one issuer slug.
    Every dict: {rating, outlook, action, rated_on (ISO), url}. `action`/
    `outlook` are None (never guessed) when the press-release title's
    phrasing doesn't match a known pattern; entries whose date fails to
    parse are dropped (a row with no date can't be a rating-history entry).

    Import `import re` etc. shared with the bulk-listing code above; this
    uses html.parser.HTMLParser rather than that section's line-oriented
    regexes because the issuer page's `.rate-item` blocks are nested divs
    (rating/title/date are siblings inside a wrapper), not one row per
    `<tr>` the way the bulk table is."""
    backend = backend or HttpBackend()
    url = f"{BASE}/ratings/issuers/{slug}/"
    html = backend.get(url)
    parser = _RateItemsParser()
    parser.feed(html)
    out = []
    for item in parser.items:
        if not item["rating"] or not item["date"]:
            continue
        iso_date = _to_iso_date(item["date"])
        if iso_date is None:
            continue
        href = item["href"]
        out.append({
            "rating": _to_ru_rating(item["rating"]),
            "outlook": _extract_outlook(item["title"]),
            "action": _extract_action(item["title"]),
            "rated_on": iso_date,
            "url": f"{BASE}{href}" if href else None,
        })
    return out
