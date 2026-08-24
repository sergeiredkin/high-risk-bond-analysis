"""Google News RSS per-issuer monitoring (`hybond google-news`, Google News
RSS Monitoring task, 2026-08-24).

Secondary market-news signal for tier=1 watchlist issuers, stored in
`market_note` with severity/category left NULL for a later auto-classify
pass -- same storage convention as scrape_pipeline.run_market_notes()'s
existing feeds (smart-lab.ru, MOEX Listing/CBR Press RSS).

Per-issuer, not market-wide (unlike scrape/rss_generic.py): Google News's
search endpoint takes a free-text query, so gating happens at fetch time
(one request per issuer name) rather than by matching a market-wide feed
against a NEWS_ALIASES table after the fact (scrape/smartlab_news.py's
model). The orchestration loop (one fetch per tier=1 issuer, 2s sleep
between requests, URL dedup, DB writes) lives in
scrape_pipeline.run_google_news() -- this module stays fetch/parse only,
same split as every other scrape/*.py module.

Uses xml.etree.ElementTree (stdlib), not feedparser -- feedparser is not an
existing dependency of this project and Google News RSS is a standard
RSS 2.0 <item> feed (title/link/pubDate), the exact same shape
scrape/rss_generic.py and scrape/smartlab_news.py already parse with
ElementTree; adding a new third-party parser for an identically-shaped feed
would be pure duplication.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from .edisclosure import Backend, HttpBackend  # noqa: F401  (Backend re-exported for typing)

logger = logging.getLogger(__name__)

SOURCE = "google_news"
BASE_URL = "https://news.google.com/rss/search"
# Fixed OR-clause narrowing the search to bond-relevant news -- the task's
# own query template, not user-configurable per issuer.
QUERY_KEYWORDS = "облигации OR дефолт OR рейтинг OR оферта OR суд"
SLEEP_BETWEEN_ISSUERS_S = 2.0
MAX_RETRIES = 3

# Legal-form prefixes stripped when deriving a default alias from an
# issuer's full legal name (e.g. 'АО "РОЛЬФ"' -> 'РОЛЬФ') -- same set
# smartlab_news.py's NEWS_ALIASES entries implicitly encode by hand.
_LEGAL_FORM_RE = re.compile(r'^(ООО|ОАО|ЗАО|АО|ПАО|ИП)\s+', re.IGNORECASE)


def _default_alias(issuer_name: str) -> str:
    name = _LEGAL_FORM_RE.sub("", issuer_name.strip())
    return name.strip('"\'«»').strip()


def _title_matches(title: str, aliases: list[str]) -> bool:
    lowered = title.lower()
    return any(alias.lower() in lowered for alias in aliases if alias)


def build_url(issuer_name: str) -> str:
    """https://news.google.com/rss/search?q={issuer_name}+облигации+OR+дефолт+OR+рейтинг+OR+оферта+OR+суд&hl=ru&gl=RU&ceid=RU:ru
    -- built via urlencode rather than an f-string so the issuer name and
    keywords are always correctly percent-encoded (Cyrillic, punctuation,
    legal-form quotes in issuer names like ООО "..." all need it)."""
    q = f"{issuer_name} {QUERY_KEYWORDS}"
    params = {"q": q, "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def _to_iso(pub_date: str | None) -> str | None:
    """RSS pubDate is RFC 822 (Google News uses the standard form, e.g.
    "Mon, 24 Aug 2026 10:00:00 GMT") -- same parser rss_generic.py/
    smartlab_news.py already use for the identical field. Returns None
    (never fabricates a date) if unparseable or absent."""
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def parse_feed(xml_text: str) -> list[dict]:
    """Every <item> with a title and link becomes one dict. Malformed XML
    raises (same fail-loud stance as rss_generic.parse_feed) -- the caller's
    job to log and skip this issuer's run."""
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip() or None
        if not title or not link:
            continue
        items.append({
            "title": title,
            "url": link,
            "published": _to_iso(item.findtext("pubDate")),
            "source": SOURCE,
        })
    return items


def fetch_issuer_news(issuer_name: str, inn: str, backend: Backend | None = None,
                       max_retries: int = MAX_RETRIES,
                       issuer_aliases: list[str] | None = None) -> list[dict]:
    """Fetch + parse Google News RSS for one issuer, then keep only items
    whose title actually mentions the issuer.

    The query's OR-clause (see QUERY_KEYWORDS) is a disjunction across the
    whole query string, not a per-issuer AND -- Google News can and does
    return an item that matches one of the credit keywords (e.g. "дефолт")
    without mentioning this issuer at all (a market-wide "Волна дефолтов
    2026" roundup surfacing for every single issuer query is the concrete
    case this filter exists for). `issuer_aliases` defaults to a single
    alias derived from `issuer_name` by stripping the legal-form prefix and
    quotes (e.g. 'АО "РОЛЬФ"' -> 'РОЛЬФ'); pass explicit aliases (e.g.
    smartlab_news.py's NEWS_ALIASES entries for this issuer_id) for names
    whose public/brand form differs from the legal name.

    Retries up to `max_retries` times on a network timeout only (requests.
    exceptions.Timeout/ConnectionError) -- a non-timeout failure (bad XML,
    non-200 status, which HttpBackend.get() raises as a plain RuntimeError)
    is not transient and is not worth retrying, so it propagates
    immediately."""
    import requests
    backend = backend or HttpBackend()
    url = build_url(issuer_name)
    aliases = issuer_aliases if issuer_aliases is not None else [_default_alias(issuer_name)]
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            xml_text = backend.get(url)
            items = parse_feed(xml_text)
            return [it for it in items if _title_matches(it["title"], aliases)]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            logger.warning("google_news fetch attempt %d/%d timed out for %s (%s): %s",
                            attempt, max_retries, issuer_name, inn, e)
    raise RuntimeError(
        f"google_news fetch failed for {issuer_name} ({inn}) after "
        f"{max_retries} attempt(s): {last_error}")
