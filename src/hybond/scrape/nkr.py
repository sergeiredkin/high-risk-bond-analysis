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
