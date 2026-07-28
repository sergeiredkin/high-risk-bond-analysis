"""e-disclosure.azipi.ru scraper — MESSAGE FEED ONLY (существенные факты /
material facts). No LLM. Snapshot-first: every raw page/body is cached before
parsing.

Scope decision (see PLAN.md, Slice 3): we scrape the *event stream* — the list
of material-fact messages per issuer and each message's text body. We do NOT
scrape financial-statement PDFs here: those are scanned, OCR-dependent, and
unregulated in layout (confirmed by practitioners), which is a Slice 4 problem.
Keeping scope to the message feed is what makes day-one automation realistic.

Source: originally e-disclosure.ru (НАУФОР), but that site's edge/WAF blocks
this environment's outbound IP outright — confirmed with a bare `curl`, no
cookies/JS/Selenium involved, so no scraper-side fix helps. АЗИПИ
(e-disclosure.azipi.ru) is a second Bank-of-Russia-authorized disclosure
agent publishing the same category of mandatory material-fact messages, is
plain server-rendered HTML (Bitrix), and is reachable from this environment
with a bare `requests.get` — no Selenium needed.

Two fetch paths against this source:
  - Homepage feed (`/`): today's messages across ALL issuers, each row
    carrying the org name + ИНН inline. One request covers the whole
    watchlist for "what filed today" — this is the PRIMARY, cheap path.
  - Per-issuer search (`/search/` -> `/search/index.php` GET by ИНН -> the
    org's `/organization/personal-pages/<id>/` page, which lists that
    issuer's message history, not just today). This is the SECONDARY path:
    used only for watchlist issuers the homepage feed didn't surface today,
    so a quiet issuer's older filings are still found.

`SeleniumBackend` is kept as an optional fallback (e.g. if a future source is
JS-gated again) but is not used by the default daily scrape path — AZIPI needs
plain HTTP only.

Because a live network fetch isn't always available (this environment,
future gate changes, CI), the PARSERS are separated from the FETCHER and
unit-tested against saved-shape HTML fixtures, so parsing correctness never
depends on network access.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

RAW_DIR = Path("data/raw/edisclosure")
BASE = "https://e-disclosure.azipi.ru"


@dataclass(frozen=True)
class Filing:
    issuer_id: str
    edisclosure_id: str
    published: str          # ISO date if parseable, else raw string
    title: str
    url: str | None
    body_hash: str          # sha1 of the title+url — stable id for dedupe
    body_text: str | None = None

    @staticmethod
    def make(issuer_id, edisclosure_id, published, title, url, body_text=None):
        h = hashlib.sha1(f"{title}|{url or ''}".encode("utf-8")).hexdigest()[:16]
        return Filing(issuer_id, edisclosure_id, published, title, url, h, body_text)


class Backend(Protocol):
    def get(self, url: str) -> str: ...


class HttpBackend:
    """Plain requests backend. Default for AZIPI — no JS gate to work around."""
    def __init__(self, timeout: int = 30):
        import requests
        self._requests = requests
        self.timeout = timeout

    def get(self, url: str) -> str:
        r = self._requests.get(
            url, timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0 Safari/537.36",
                     "Accept-Language": "ru,en"})
        if r.status_code != 200:
            raise RuntimeError(f"{url} -> HTTP {r.status_code}")
        return r.text


class SeleniumBackend:
    """Headless-browser backend, kept as an optional fallback only. Not used
    by the default AZIPI scrape path — selenium is an optional dependency
    imported only here so the rest of the system has no browser requirement."""
    def __init__(self, wait: float = 6.0, driver=None):
        self.wait = wait
        self._driver = driver  # inject for tests; else lazily created

    def _ensure(self):
        if self._driver is not None:
            return self._driver
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        self._driver = webdriver.Chrome(options=opts)
        return self._driver

    def get(self, url: str) -> str:
        import time
        d = self._ensure()
        d.get(url)
        time.sleep(self.wait)
        return d.page_source


def _snapshot(name: str, text: str, day: date | None = None) -> Path:
    day = day or date.today()
    p = RAW_DIR / day.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    fp = p / name
    fp.write_text(text, encoding="utf-8")
    return fp


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\s+", " ", text).strip()


_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _to_iso_date(s: str) -> str:
    m = _DATE_RE.search(s)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _abs_url(href: str) -> str:
    return href if href.startswith("http") else f"{BASE}{href}"


# --- primary path: today's homepage feed, all issuers at once --------------

def fetch_homepage(backend: Backend, snapshot: bool = True) -> str:
    html = backend.get(f"{BASE}/")
    if snapshot:
        _snapshot("homepage.html", html)
    return html


_ITEM_RE = re.compile(
    r'<div class="item">\s*<div class="date">\s*(?P<date>[\d.: ]+?)\s*</div>\s*'
    r'<div class="link">\s*<a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>\s*</div>\s*'
    r'<div class="info">\s*(?P<info>.*?)</div>',
    re.DOTALL,
)
_INN_RE = re.compile(r'ИНН организации:\s*<b>\s*(\d+)\s*</b>')


def parse_homepage_feed(html: str) -> list[Filing]:
    """Today's messages, across all issuers on the page. `issuer_id` and
    `edisclosure_id` are both set to the parsed ИНН — callers match that
    against their own watchlist keys (which are ИНН) to filter."""
    filings: list[Filing] = []
    for m in _ITEM_RE.finditer(html):
        inn_m = _INN_RE.search(m.group("info"))
        if not inn_m:
            continue
        inn = inn_m.group(1)
        title = _clean_text(m.group("title"))
        if not title:
            continue
        published = _to_iso_date(m.group("date"))
        url = _abs_url(m.group("href"))
        filings.append(Filing.make(inn, inn, published, title, url))
    return filings


# --- secondary path: search by ИНН -> per-issuer message history -----------

_SESSID_RE = re.compile(r'id="sessid"\s+value="([0-9a-f]+)"')
_ORG_LINK_RE = re.compile(r'href="(/organization/personal-pages/\d+/)"')


def search_org_by_inn(inn: str, backend: Backend) -> str | None:
    """Look up an issuer's АЗИПИ organization page URL by ИНН. Returns None if
    not found (new/unlisted issuer) rather than raising — the caller treats
    that the same as "no filings this path"."""
    search_page = backend.get(f"{BASE}/search/")
    sessid_m = _SESSID_RE.search(search_page)
    if not sessid_m:
        return None
    qs = urllib.parse.urlencode({
        "sessid": sessid_m.group(1), "orgs": "Y", "ORG_NAME": "",
        "ORG_INN": inn, "ORG_OGRN": "", "search_organization": "Поиск",
    })
    result = backend.get(f"{BASE}/search/index.php?{qs}")
    link_m = _ORG_LINK_RE.search(result)
    return _abs_url(link_m.group(1)) if link_m else None


_ORG_ROW_RE = re.compile(
    r'<tr>\s*<td>[\d.]+</td>\s*<td>(?P<pub>[\d.: ]+)</td>\s*'
    r'<td><a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a></td>',
    re.DOTALL,
)


def parse_org_page_feed(html: str, issuer_id: str) -> list[Filing]:
    """One issuer's message history table from their АЗИПИ organization page."""
    filings: list[Filing] = []
    for m in _ORG_ROW_RE.finditer(html):
        title = _clean_text(m.group("title"))
        if not title:
            continue
        published = _to_iso_date(m.group("pub"))
        url = _abs_url(m.group("href"))
        filings.append(Filing.make(issuer_id, issuer_id, published, title, url))
    return filings


def fetch_message_body(url: str, backend: Backend, snapshot: bool = True) -> str:
    """Fetch and strip one message's body text for the classifier."""
    html = backend.get(url)
    if snapshot:
        name = "msg_" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".html"
        _snapshot(name, html)
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def new_filings_since(
    filings: list[Filing], seen_hashes: set[str]
) -> list[Filing]:
    """The dedupe gate: only filings whose body_hash we haven't stored before.
    This is what keeps the classifier from re-processing (and re-paying for)
    yesterday's news."""
    return [f for f in filings if f.body_hash not in seen_hashes]


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
