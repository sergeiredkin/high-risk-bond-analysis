"""smart-lab.ru general news feed (RSS) — a SECONDARY, supplementary source,
distinct from the bonds-forum feed this package's smartlab.py already scrapes.
That module targets community bond-forum posts (smart-lab.ru/bonds/extra/);
this one targets the site's general market-news RSS (smart-lab.ru/news/rss/),
which covers the whole market (dividends, earnings, index moves, corporate
actions), not just bond-specific chatter. Real, structured XML -- confirmed
live 2026-07-30 (standard title/link/pubDate/description items), no JS gate.

Stored in `market_note` (store/db.py), NOT `event` or `filing` -- this is
business news, not a regulatory disclosure, and the distinction matters for
how hy-event-classify should weight it once classification is wired for this
source (a later step; this module only ingests headline/summary/url/date,
severity and category stay NULL here).

False-positive risk is higher here than in the bonds-forum feed: every post
scraped by smartlab.py is already bond-specific, so a name match there is
unlikely to be a false hit. This feed covers the WHOLE market, so a generic
single-word alias (e.g. bare "Система") would false-match constantly ("платёжная
система", "система страхования", ...). NEWS_ALIASES below deliberately uses
multi-word or distinctive aliases only, and public brand names where the
issuer's public identity differs from its legal entity name (e.g. "Делимобиль",
not "Каршеринг Руссия").
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from .edisclosure import Backend  # noqa: F401  (re-exported for typing)

BASE = "https://smart-lab.ru"
FEED_URL = f"{BASE}/news/rss/"

SOURCE_MARKER = "smartlab_news"

# Name/brand/ticker -> watchlist issuer_id (ИНН). Built fresh 2026-07-30,
# cross-checked against watchlist.json's current issuer_id values directly
# (not copied from smartlab.py's INTERFAX_ISSUERS, which targets a different
# feed's typical name forms) -- see test_smartlab_news.py's FK-consistency
# test, which asserts every value here resolves to a real `issuer` row.
NEWS_ALIASES = {
    "Европлан": "9705101614",
    "Самолёт": "9731004688",
    "Самолет": "9731004688",
    "SMLT": "9731004688",
    "МГКЛ": "7707600245",
    "Мосгорломбард": "7707600245",
    "ВУШ": "9717068640",
    "Whoosh": "9717068640",
    "Сегежа": "9703024202",
    "АйДи Коллект": "7730233723",
    "ЕвроТранс": "5029169023",
    "Селигдар": "1402047184",
    "Брусника": "6685151087",
    "ГлоракС": "9729387137",
    "Контрол Лизинг": "7805485840",
    "Монополия": "7810766685",
    "Мани Капитал": "6316103050",
    "ПКБ": "2723115222",
    "Первое коллекторское": "2723115222",
    "ПСБ Финанс": "7730634468",
    "CarMoney": "7730634468",
    "КарМани": "7730634468",
    "Интерлизинг": "7802131219",
    "Балтийский лизинг": "7826705374",
    "Эталон": "7705619586",
    "АФК Система": "7703104630",  # NOT bare "Система" -- see module docstring
    "Гарант-Инвест": "7726637843",
    "АПРИ": "7453326003",
    "Делимобиль": "9718236471",
    "Пионер-Лизинг": "2128702350",
    "Уральская Сталь": "5607019523",
    "Лизинг-Трейд": "1655096633",
    "Роделен": "7813379412",
}


@dataclass
class MarketNote:
    issuer_id: str
    occurred: str | None       # ISO date/datetime if parseable, else None
    headline: str
    summary: str | None
    url: str | None
    body_hash: str

    @staticmethod
    def make(issuer_id, occurred, headline, url, summary=None):
        h = hashlib.sha1(f"{headline}|{url or ''}".encode("utf-8")).hexdigest()[:16]
        return MarketNote(issuer_id, occurred, headline, summary, url, h)


def fetch_feed(backend: Backend) -> str:
    return backend.get(FEED_URL)


def _match_issuer(text: str) -> str | None:
    for name, issuer_id in NEWS_ALIASES.items():
        if name in text:
            return issuer_id
    return None


def _to_iso(pub_date: str | None) -> str | None:
    """RSS pubDate is RFC 822 (e.g. 'Thu, 30 Jul 2026 11:05:00 +0300').
    Returns an ISO datetime, or None if unparseable -- never fabricate a date."""
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def parse_feed(xml_text: str) -> list[MarketNote]:
    """Every <item> whose title OR description matches a NEWS_ALIASES entry
    becomes one MarketNote. Malformed XML raises (caller's problem to log
    and skip this run, not silently return nothing and look like 'no news
    today') -- consistent with edisclosure.py/smartlab.py's fail-loud stance
    on genuinely broken input."""
    root = ET.fromstring(xml_text)
    notes: list[MarketNote] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        description = (item.findtext("description") or "").strip()
        pub_date = item.findtext("pubDate")
        if not title:
            continue
        issuer_id = _match_issuer(title) or _match_issuer(description)
        if not issuer_id:
            continue
        occurred = _to_iso(pub_date)
        notes.append(MarketNote.make(
            issuer_id, occurred, title, link, summary=description or None))
    return notes


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
