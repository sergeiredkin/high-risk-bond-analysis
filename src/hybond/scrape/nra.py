"""ra-national.ru (НРА -- Национальное Рейтинговое Агентство) -- rating-
database lookup. Same due-diligence question as the earlier НКР pass: does
this agency expose a bulk-listable structure, or is it JS/AJAX-gated like
Эксперт РА and АКРА?

Confirmed scrapable: `/list-of-credit-ratings/` is a single plain-HTTP page
(no JS, no anti-bot) containing SIX separate WordPress-DataTable tables, one
per year (2020-2025, ~414 rows total) -- there is no 2026 table yet, so this
page is a retroactive yearly archive, not a live-updated feed; any 2026
rating action won't show here until НРА publishes that year's table. That
staleness risk is exactly what the 12-month verification_flags() convention
exists to catch: a match found here still gets checked against its own
rated_on before being marked "current".

Better than НКР in one respect: the 2023/2024/2025 tables (ids 40/45/51)
carry an ИНН column directly, so those rows can be matched by exact ИНН
instead of name normalisation. The older 2020-2022 tables (16/17/34) have no
ИНН column at all -- those fall back to the same normalize_name() core-name
match used for НКР (imported from there rather than reimplemented, since the
logic is agency-agnostic).

Table shapes differ (with/without ИНН, with/without a status column), so
parsing reads each table's own header row and maps columns by name rather
than assuming a fixed position -- more robust than hardcoding indices across
6 differently-shaped tables.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .nkr import normalize_name  # agency-agnostic core-name normaliser

BASE = "https://www.ra-national.ru"
LIST_URL = f"{BASE}/list-of-credit-ratings/"
RAW_DIR = Path("data/raw/nra")


@dataclass(frozen=True)
class NraRating:
    name: str
    name_core: str
    inn: str | None
    rating: str | None      # converted to our 'ruXX' convention; None if withdrawn
    outlook: str | None
    rated_on: str | None       # ISO date
    is_withdrawn: bool = False
    source_table: str | None = None   # wpdtSimpleTable id, for provenance


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


def fetch_list_page(backend: Backend | None = None, snapshot: bool = True) -> str:
    backend = backend or HttpBackend()
    html = backend.get(LIST_URL)
    if snapshot:
        _snapshot("list-of-credit-ratings.html", html)
    return html


# --- parsing ----------------------------------------------------------------

_TABLE_RE = re.compile(
    r'<table[^>]*id="(wpdtSimpleTable-\d+)"[^>]*>(.*?)</table>', re.DOTALL)
_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL)
_CELL_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')

# Header text (lowercased, whitespace-collapsed) -> our field name. Table
# shapes vary (some have no ИНН/Статус column at all) -- read each table's
# own header row rather than assume a fixed column position.
_COLUMN_KEY_MAP = {
    "наименование рейтингуемого лица": "name",
    "инн": "inn",
    "присвоен рейтинг": "rating",
    "прогноз по рейтингу": "outlook",
    "дата публикации": "rated_on",
    "статус рейтинга": "status",
}


def _strip_tags(html: str) -> str:
    return " ".join(_TAG_RE.sub(" ", html).split())


def _to_ru_rating(raw: str) -> str:
    """НРА's own scale ('A|ru|', 'BB-|ru|') -> this codebase's 'ruXX'
    convention (matches default_probs.DEFAULT_PROBS keys).

    НРА's page renders the minus modifier as a Unicode en-dash ('–',
    U+2013) in some rows rather than an ASCII hyphen -- confirmed live
    2026-07-31 (e.g. 'BBB–|ru|' for Оил Ресурс). default_probs.rating_rank()
    only recognises the ASCII form, so an unnormalised en-dash would silently
    fail every downstream rating lookup (PD, implied spread, AEY) for that
    row -- normalise before prefixing."""
    core = raw.replace("|ru|", "").strip()
    core = core.replace("–", "-").replace("−", "-")
    return "ru" + core


def _to_iso_date(s: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def parse_ratings(html: str) -> list[NraRating]:
    out: list[NraRating] = []
    for table_m in _TABLE_RE.finditer(html):
        table_id, body = table_m.groups()
        rows = _ROW_RE.findall(body)
        if not rows:
            continue
        header_cells = [_strip_tags(c).lower() for c in _CELL_RE.findall(rows[0])]
        col_index: dict[str, int] = {}
        for idx, h in enumerate(header_cells):
            key = _COLUMN_KEY_MAP.get(h)
            if key:
                col_index[key] = idx
        if "name" not in col_index or "rating" not in col_index:
            continue  # not a ratings table in the shape we understand

        for row_html in rows[1:]:
            cells = [_strip_tags(c) for c in _CELL_RE.findall(row_html)]
            if len(cells) <= max(col_index.values()):
                continue
            name = cells[col_index["name"]]
            if not name:
                continue
            inn = cells[col_index["inn"]].strip() if "inn" in col_index else None
            rating_raw = cells[col_index["rating"]]
            outlook = cells[col_index["outlook"]] if "outlook" in col_index else None
            rated_on = (_to_iso_date(cells[col_index["rated_on"]])
                        if "rated_on" in col_index else None)
            status = cells[col_index["status"]] if "status" in col_index else None
            withdrawn = bool(status and "отозв" in status.lower())
            # withdrawn rows show a dash placeholder ('—') in the rating cell
            # instead of a grade -- guard on that directly too, not just the
            # status column (older tables have no status column at all).
            no_grade = not rating_raw or rating_raw.strip() in ("—", "-", "")
            withdrawn = withdrawn or no_grade

            out.append(NraRating(
                name=name, name_core=normalize_name(name),
                inn=inn or None,
                rating=None if withdrawn else _to_ru_rating(rating_raw),
                outlook=None if withdrawn else (outlook or None),
                rated_on=rated_on, is_withdrawn=withdrawn,
                source_table=table_id))
    return out


def _latest(candidates: list[NraRating]) -> NraRating | None:
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.rated_on or "")


def match_by_inn(inn: str, rows: list[NraRating]) -> NraRating | None:
    """Exact ИНН match, most recent `rated_on` among (possibly several
    across different yearly tables) matches for that ИНН."""
    return _latest([r for r in rows if r.inn == inn])


def match_by_name(target_name: str, rows: list[NraRating]) -> NraRating | None:
    """Fallback for the older (2020-2022) tables that carry no ИНН column --
    same exact-core-name matching as НКР's, for the same false-positive-
    avoidance reason (see scrape/nkr.py's match_by_name docstring)."""
    target_core = normalize_name(target_name)
    return _latest([r for r in rows if r.name_core == target_core])


def match(inn: str | None, name: str, rows: list[NraRating]) -> NraRating | None:
    """ИНН match first (exact, most reliable); name-core match as fallback
    for rows that have no ИНН at all."""
    if inn:
        hit = match_by_inn(inn, rows)
        if hit:
            return hit
    return match_by_name(name, rows)
