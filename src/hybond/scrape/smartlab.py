"""smart-lab.ru bonds news feed — a SECONDARY, supplementary source. Not a
primary disclosure agent: this is community-aggregated news and commentary
(plus, occasionally, an issuer's own IR account posting directly), scraped
for rating changes, defaults, and material events on issuers that have no
coverage on e-disclosure.ru (WAF-blocked from this environment) or on the
two primary agents this project already scrapes — АЗИПИ (edisclosure.py).

This module supplements the AZIPI/material-facts feed; it does not replace
it. Filings sourced here are marked `edisclosure_id='smartlab'` in the filing
table so hy-event-classify knows to treat them as secondary: lower confidence,
and a "via Smart-Lab" note rather than presenting them as primary disclosure.

Feed: https://smart-lab.ru/bonds/extra/ — page 1 (~50 most recent posts,
today back through the last day or two) is enough for a same-day cron scrape;
no pagination needed for that cadence.

Plain HTTP, UTF-8, no JS gate — reuses edisclosure.HttpBackend so there's no
new fetch machinery. Matching is against the visible title AND author name,
since some of the highest-signal posts are an issuer's own IR account
publishing directly (e.g. /profile/CarMoney/, /profile/Etalon_Group/) with a
title that doesn't repeat the company name.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .edisclosure import Filing, Backend  # noqa: F401  (Backend re-exported for typing)

BASE = "https://smart-lab.ru"
FEED_URL = f"{BASE}/bonds/extra/"

SOURCE_MARKER = "smartlab"

# Name/ticker -> watchlist issuer_id (ИНН). Multiple keys can map to the same
# issuer (Cyrillic name, Latin ticker, alternate spelling).
INTERFAX_ISSUERS = {
    # 2026-07-30: 8 of these 11 entries carried an INN that didn't match
    # watchlist.json's current issuer_id for the same company (confirmed via
    # the same cross-check that found wrong INNs in watchlist.json itself
    # earlier this session) -- a name-match here would have "succeeded" but
    # then silently failed to attach to any real issuer row, since the
    # target issuer_id didn't exist. Corrected against watchlist.json.
    "Самолёт": "9731004688",
    "Самолет": "9731004688",
    "SMLT": "9731004688",
    "Сегежа": "9703024202",
    "АФК Система": "7703104630",
    "ГлоракС": "9729387137",
    "Монополия": "7810766685",
    "CarMoney": "7730634468",
    "КарМани": "7730634468",
    "Интерлизинг": "7802131219",
    "Балтийский лизинг": "7826705374",
    "Эталон": "7705619586",
    "Гарант-Инвест": "7726637843",
}

_ITEM_RE = re.compile(
    r'<li class="bluid_\d+">\s*<b>.*?</b>\s*'
    r'(?P<when>[\d:/]+)\s*'
    r'<i><a href="/profile/[^"]+/">(?P<author>[^<]*)</a></i>\s*'
    r'<a href="(?P<href>/blog/[^"]+\.php)"[^>]*>(?P<title>.*?)</a>\s*</li>',
    re.DOTALL,
)


def fetch_feed(backend: Backend) -> str:
    return backend.get(FEED_URL)


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\s+", " ", text).strip()


def _to_iso_date(when: str, today: date | None = None) -> str:
    """`when` is either 'HH:MM' (posted today) or 'DD/MM' (older, no year —
    the feed only shows year-less dates within the current year)."""
    today = today or date.today()
    if ":" in when:
        return today.isoformat()
    try:
        d, m = when.split("/")
        dt = date(today.year, int(m), int(d))
        if dt > today:  # e.g. scraped in early Jan, item dated late Dec
            dt = date(today.year - 1, int(m), int(d))
        return dt.isoformat()
    except (ValueError, IndexError):
        return ""


def _match_issuer(text: str) -> str | None:
    for name, issuer_id in INTERFAX_ISSUERS.items():
        if name in text:
            return issuer_id
    return None


def parse_feed(html: str, today: date | None = None) -> list[Filing]:
    """Every matched post becomes one Filing, issuer_id = the matched ИНН,
    edisclosure_id = SOURCE_MARKER ('smartlab') — that's the tag the store and
    the classifier queue use to treat it as secondary."""
    filings: list[Filing] = []
    for m in _ITEM_RE.finditer(html):
        title = _clean_text(m.group("title"))
        author = _clean_text(m.group("author"))
        if not title:
            continue
        issuer_id = _match_issuer(title) or _match_issuer(author)
        if not issuer_id:
            continue
        published = _to_iso_date(m.group("when"), today=today)
        url = f"{BASE}{m.group('href')}"
        filings.append(Filing.make(issuer_id, SOURCE_MARKER, published, title, url))
    return filings


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
