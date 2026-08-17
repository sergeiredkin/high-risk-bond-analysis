"""Generic market-wide RSS ingestion (MOEX Listing + CBR Press task,
2026-08-17) -- for feeds whose items are NOT scoped to a single watchlist
issuer, unlike smartlab_news.py's NEWS_ALIASES-gated model.

Confirmed against live fetches before writing this module (both feeds
pulled and inspected 2026-08-17): CBR press releases (rate decisions,
macroprudential policy, licensing) essentially never name a single
watchlist issuer -- of 8 live items, zero matched any NEWS_ALIASES entry.
Gating insertion on an issuer match (smartlab_news.py's model) would have
silently dropped effectively the whole CBR feed. So these items are stored
market-wide with issuer_id=NULL; classify_utils.queue_bucket() (a
display-only filter, per its own docstring) is what surfaces relevance,
not an insertion-time gate.

Standard RSS 2.0 <item> shape (title/link/description/pubDate) -- same
shape smartlab_news.py already parses, just without the alias match.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from .edisclosure import Backend  # noqa: F401  (re-exported for typing)


@dataclass
class FeedConfig:
    url: str
    source: str
    feed_url: str


MOEX_LISTING_FEED = FeedConfig(
    url="https://moex.com/export/news.aspx?cat=104",
    source="moex_listing",
    feed_url="https://moex.com/export/news.aspx?cat=104",
)

CBR_PRESS_FEED = FeedConfig(
    url="https://www.cbr.ru/rss/RssPress",
    source="cbr_press",
    feed_url="https://www.cbr.ru/rss/RssPress",
)


@dataclass
class MarketNote:
    issuer_id: str | None
    occurred: str | None
    headline: str
    summary: str | None
    url: str | None
    body_hash: str

    @staticmethod
    def make(occurred, headline, url, summary=None):
        h = hashlib.sha1(f"{headline}|{url or ''}".encode("utf-8")).hexdigest()[:16]
        return MarketNote(None, occurred, headline, summary, url, h)


def fetch_feed(backend: Backend, cfg: FeedConfig) -> str:
    return backend.get(cfg.url)


def _to_iso(pub_date: str | None) -> str | None:
    """RSS pubDate is RFC 822 -- same parser smartlab_news.py uses. Returns
    None (never fabricates a date) if unparseable or absent."""
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
    """Every <item> becomes one MarketNote -- no issuer-match gate (see
    module docstring). Malformed XML raises, same fail-loud stance as
    smartlab_news.parse_feed (caller's job to log and skip this run)."""
    root = ET.fromstring(xml_text)
    notes: list[MarketNote] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        description = (item.findtext("description") or "").strip()
        pub_date = item.findtext("pubDate")
        if not title:
            continue
        occurred = _to_iso(pub_date)
        notes.append(MarketNote.make(occurred, title, link, summary=description or None))
    return notes
