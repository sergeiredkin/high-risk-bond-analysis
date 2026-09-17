"""MOEX ISS "CCI" (ЦКИ) credit ratings over the free anonymous endpoints.

Two endpoints, both anonymous, both keyed by MOEX's own emitent_id (the
`emitent_id` column /iss/securities.json already returns -- see
sources.fetch_emitent_map()):

  issuer : /iss/cci/rating/companies/ecbd_<emitent_id>.json
  issue  : /iss/cci/rating/companies/ecbd_<emitent_id>/securities/isin_<ISIN>.json

Each returns every agency's CURRENT rating on every scale MOEX carries for
that issuer/issue: agency, rating_book_id (the scale), the level string as
the agency writes it, and the action date. Withdrawn ratings stay listed as
"Отозван". No outlook, watch status or action type -- that is only in the
paid ЦКИ product, which this project deliberately does not use.

Terms / limits (Phase 0 of the ratings-coverage task, 2026-09-17): MOEX
publishes no request limit for ISS. moex.com/a2193 says ISS data is "только
для ознакомления, не может быть использована для извлечения прибыли". Every
anonymous response carries `X-MicexPassport-Marker: denied` even when the
data is complete, so the marker is NOT a failure signal -- an empty or
non-JSON body is. Measured: two back-to-back full daily workloads (2x569
calls at 0.5 s spacing) all returned HTTP 200 JSON with identical data, no
throttling headers, median latency ~0.17 s. REQUEST_SPACING_S below is that
measured spacing; do not tighten it.

Nothing here matches by name. Keys are emitent_id, ISIN and INN only.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

MOEX_BASE = "https://iss.moex.com/iss"
USER_AGENT = "hybond/0.1 (research)"
REQUEST_SPACING_S = 0.5
RETRY_BACKOFF_S = (2.0, 6.0)
TIMEOUT_S = 30

# rating_book_id -> scale kind, from /iss/cci/reference/rating-books.
# Only 'national' feeds PD. 'sf' (structured finance) and 'structural'
# (Эксперт РА structural notes) are shown but never modelled; 'osk'
# (standalone credit assessments) and everything else are not ratings of
# the bond's credit and are ignored.
NATIONAL_BOOKS = {1: "АКРА", 18: "Эксперт РА", 29: "НКР", 32: "НРА"}
SF_BOOKS = {2, 20}
STRUCTURAL_BOOKS = {19}
OSK_BOOKS = {11, 43, 48, 53, 54, 55, 56, 57, 58}

WITHDRAWN = "Отозван"
SUSPENDED = "Приостановлен"

# One agency grade body: AAA, BB+, CCC, SD, RD, D ... with either dash.
_GRADE = r"([A-Za-z]{1,3}[+\-–]?)"
_PATTERNS = (
    re.compile(rf"^ru{_GRADE}(\.sf)?$"),            # Эксперт РА: ruBB-, ruAAA.sf
    re.compile(rf"^{_GRADE}\((?:RU|ru)(\.sf)?\)$"),  # АКРА: BB-(RU), AAA(ru.sf)
    re.compile(rf"^{_GRADE}\.ru$"),                  # НКР: BB-.ru
    re.compile(rf"^{_GRADE}\|ru\|$"),                # НРА: BB-|ru|
)
_DEFAULT_FAMILY = {"D", "SD", "RD"}
_NATIONAL_GRADES = {
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C",
}


def scale_kind(book_id: int | None, raw: str | None = None) -> str:
    """'national' / 'sf' / 'structural' / 'osk' / 'other'. Unknown books fall
    back to the text: '.sf' is structured finance, anything else unknown is
    'other' (never silently promoted to national)."""
    if book_id in NATIONAL_BOOKS:
        return "national"
    if book_id in SF_BOOKS:
        return "sf"
    if book_id in STRUCTURAL_BOOKS:
        return "structural"
    if book_id in OSK_BOOKS:
        return "osk"
    if raw and ".sf" in raw:
        return "sf"
    return "other"


def normalize_level(book_id: int | None, raw: str | None) -> tuple[str | None, str]:
    """(grade, status) for one MOEX rating level.

    grade is the project's canonical key -- 'ruBB-' as in
    compute/default_probs.DEFAULT_PROBS_1YR, 'D' for the whole default family
    (SD/RD/D on any agency's scale), or 'ruAAA.sf' for a structured-finance
    grade (display only). status is 'live', 'default', 'withdrawn',
    'suspended' or 'unknown' (a level string this mapper doesn't recognise:
    kept raw, never guessed)."""
    text = (raw or "").strip()
    if text == WITHDRAWN:
        return None, "withdrawn"
    if text == SUSPENDED:
        return None, "suspended"
    kind = scale_kind(book_id, text)
    for pat in _PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        body = m.group(1).replace("–", "-")
        if body != body.upper():
            return None, "unknown"  # lowercase = standalone assessment, not a rating
        if body in _DEFAULT_FAMILY:
            return "D", "default"
        if body not in _NATIONAL_GRADES:
            return None, "unknown"
        suffix = ".sf" if kind == "sf" else ""
        return f"ru{body}{suffix}", "live"
    return None, "unknown"


@dataclass
class ParsedRating:
    agency: str
    agency_id: int | None
    book_id: int
    raw_level: str
    grade: str | None
    scale: str
    status: str
    rated_on: str | None
    published_on: str | None


def parse_payload(payload: dict) -> tuple[list[ParsedRating], dict | None]:
    """Rows of an issuer or issue response, plus its cursor (issuer responses
    carry one; issue responses don't). Raises ValueError when the payload has
    no rating block at all -- that is a changed or denied response, not an
    empty rating list."""
    block = next((k for k in payload
                  if k.startswith("cci_rating_") and not k.endswith(".cursor")), None)
    if block is None or "columns" not in payload[block]:
        raise ValueError(f"no cci_rating block in payload (keys: {list(payload)[:5]})")
    cols = payload[block]["columns"]
    out = []
    for values in payload[block].get("data", []):
        r = dict(zip(cols, values))
        book_id = r.get("rating_book_id")
        raw = r.get("rating_level_name_short_ru") or ""
        grade, status = normalize_level(book_id, raw)
        out.append(ParsedRating(
            agency=(r.get("agency_name_short_ru") or "").strip(),
            agency_id=r.get("agency_id"),
            book_id=book_id,
            raw_level=raw,
            grade=grade,
            scale=scale_kind(book_id, raw),
            status=status,
            rated_on=(r.get("rating_date") or "")[:10] or None,
            published_on=(r.get("rating_publicate_date") or "")[:10] or None,
        ))
    cursor = None
    cur_block = payload.get(f"{block}.cursor")
    if cur_block and cur_block.get("data"):
        cursor = dict(zip(cur_block["columns"], cur_block["data"][0]))
    return out, cursor


def issuer_url(emitent_id: int) -> str:
    return f"{MOEX_BASE}/cci/rating/companies/ecbd_{int(emitent_id)}.json"


def issue_url(emitent_id: int, isin: str) -> str:
    return f"{MOEX_BASE}/cci/rating/companies/ecbd_{int(emitent_id)}/securities/isin_{isin}.json"


class RatingsBlocked(RuntimeError):
    """HTTP 403/429 from ISS: stop the whole run, never retry into a block."""


@dataclass
class FetchResult:
    outcome: str                       # 'ok' / 'empty' / 'denied' / 'error'
    rows: list[ParsedRating] = field(default_factory=list)
    detail: str | None = None
    payload: Any = None


class MoexRatingsClient:
    """Sequential, spaced, retried fetcher. `get` and `sleep` are injectable so
    tests run offline and instantly."""

    def __init__(self, spacing_s: float = REQUEST_SPACING_S,
                 get: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.spacing_s = spacing_s
        self._session = None if get else requests.Session()
        self._get = get or self._session.get
        self._sleep = sleep
        self._clock = clock
        self._last = None
        self.calls = 0

    def _request(self, url: str, params: dict | None = None):
        if self._last is not None:
            wait = self.spacing_s - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.calls += 1
        return self._get(url, params=params or {}, timeout=TIMEOUT_S,
                         headers={"User-Agent": USER_AGENT})

    def fetch(self, url: str) -> FetchResult:
        rows: list[ParsedRating] = []
        payloads = []
        start = 0
        while True:
            params = {"iss.meta": "off"}
            if start:
                params["start"] = start
            result = self._fetch_page(url, params)
            if result.outcome not in ("ok", "empty"):
                return result
            rows.extend(result.rows)
            payloads.append(result.payload)
            cursor = result.detail
            if not cursor:
                break
            total, index, size = cursor.get("TOTAL"), cursor.get("INDEX"), cursor.get("PAGESIZE")
            if not (total and size) or index + size >= total:
                break
            start = index + size
        return FetchResult("ok" if rows else "empty", rows,
                           payload=payloads[0] if len(payloads) == 1 else payloads)

    def _fetch_page(self, url: str, params: dict) -> FetchResult:
        last_err = None
        for attempt in range(len(RETRY_BACKOFF_S) + 1):
            if attempt:
                self._sleep(RETRY_BACKOFF_S[attempt - 1])
            try:
                r = self._request(url, params)
            except requests.RequestException as e:
                last_err = f"network: {e}"
                continue
            if r.status_code in (403, 429):
                raise RatingsBlocked(f"{url} -> HTTP {r.status_code}")
            if r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                return FetchResult("error", detail=f"HTTP {r.status_code}")
            text = r.text or ""
            if "<html" in text[:500].lower() or "подписчик" in text:
                return FetchResult("denied", detail=text[:200])
            try:
                payload = r.json()
                rows, cursor = parse_payload(payload)
            except ValueError as e:
                return FetchResult("denied", detail=f"unparseable: {e}"[:200])
            return FetchResult("ok" if rows else "empty", rows, detail=cursor, payload=payload)
        return FetchResult("error", detail=last_err)
