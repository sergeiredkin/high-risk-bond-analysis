"""MOEX ISS and CBR ingestion. Snapshot-first: every raw response is written to
data/raw/ before parsing, so any parse bug is replayable against the exact bytes
we received. No LLM, no computation here — fetch and normalise only.

Endpoints (all free, no auth, 15-min delayed for MOEX market data):
  MOEX bond master   : /iss/engines/stock/markets/bonds/securities.json
  MOEX one security  : /iss/securities/<SECID>.json
  MOEX bondization   : /iss/securities/<SECID>/bondization.json  (coupons+amort)
  CBR G-curve        : /hd_base/zcyc_params/  (parameters / values)

The CBR ZCYC is also mirrored via MOEX. We keep the CBR site as the canonical
source and allow a MOEX fallback.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

MOEX_BASE = "https://iss.moex.com/iss"
CBR_ZCYC = "https://www.cbr.ru/hd_base/zcyc_params/"

RAW_DIR = Path("data/raw")


class IngestError(RuntimeError):
    pass


def _get(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> requests.Response:
    r = requests.get(url, params=params or {}, timeout=timeout,
                     headers={"User-Agent": "hybond/0.1 (research)"})
    if r.status_code != 200:
        raise IngestError(f"{url} -> HTTP {r.status_code}")
    return r


def _snapshot(name: str, text: str, day: date | None = None) -> Path:
    day = day or date.today()
    d = RAW_DIR / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def fetch_bond_master(snapshot: bool = True) -> list[dict[str, Any]]:
    """All traded bonds with descriptor columns. Returns list of row dicts.

    Note: ISQUALIFIEDINVESTORS is requested but MOEX silently drops it from
    this endpoint's response (verified empirically -- it's simply absent
    from every row, even alone in the column list). `qualified` downstream
    has therefore always resolved to falsy/0; not fixed here, out of scope
    for the change that surfaced it -- flagging in case it matters later.
    BOARDID and OFFERDATE are used by universe_pipeline.py's screening
    (exclude OFZ board TQOB) and offer-date display.
    """
    cols = ["SECID", "ISIN", "SHORTNAME", "SECNAME", "MATDATE", "FACEVALUE",
            "FACEUNIT", "COUPONVALUE", "COUPONPERCENT", "COUPONPERIOD",
            "LISTLEVEL", "ISQUALIFIEDINVESTORS", "BOARDID", "OFFERDATE"]
    r = _get(f"{MOEX_BASE}/engines/stock/markets/bonds/securities.json",
             params={"iss.meta": "off", "iss.only": "securities",
                     "securities.columns": ",".join(cols)})
    if snapshot:
        _snapshot("moex_bond_master.json", r.text)
    return _rows(r.json(), "securities")


def fetch_bonds_by_inn(inn: str, snapshot: bool = True) -> list[dict[str, Any]]:
    """All currently-outstanding MOEX-listed bonds for one issuer by ИНН —
    every series, not just the ones in the watchlist.

    `/iss/engines/stock/markets/bonds/securities.json` ignores its own `q`
    param and always returns the full ~3400-row bond master regardless of
    what's passed (verified against a live query) -- do not add `q` to that
    endpoint expecting a filter. The general `/iss/securities.json` endpoint
    is the one that actually searches, and it returns issuer-identity columns
    (emitent_inn, is_traded) but not descriptor columns (MATDATE,
    COUPONPERCENT, FACEVALUE, ...). So this fetches from both: search by ИНН
    there, filter to an exact ИНН match and currently-traded series, then
    backfill descriptors from fetch_bond_master() joined on SECID.

    is_traded=0 (matured/delisted) series are excluded -- "outstanding" means
    still on the board, not just ever issued.
    """
    r = _get(f"{MOEX_BASE}/securities.json",
             params={"q": inn, "engine": "stock", "market": "bonds"})
    if snapshot:
        _snapshot(f"moex_securities_by_inn_{inn}.json", r.text)
    hits = _rows(r.json(), "securities")
    hits = [h for h in hits if h.get("emitent_inn") == inn and h.get("is_traded") == 1]
    if not hits:
        return []

    master = {m["SECID"]: m for m in fetch_bond_master(snapshot=False)}
    out = []
    for h in hits:
        secid = h.get("secid")
        m = master.get(secid, {})
        out.append({
            "SECID": secid,
            "ISIN": h.get("isin") or m.get("ISIN"),
            "SHORTNAME": h.get("shortname") or m.get("SHORTNAME"),
            "MATDATE": m.get("MATDATE"),
            "FACEVALUE": m.get("FACEVALUE"),
            "FACEUNIT": m.get("FACEUNIT"),
            "COUPONPERCENT": m.get("COUPONPERCENT"),
            "COUPONPERIOD": m.get("COUPONPERIOD"),
            "LISTLEVEL": m.get("LISTLEVEL"),
            "ISQUALIFIEDINVESTORS": m.get("ISQUALIFIEDINVESTORS"),
        })
    return out


def fetch_emitent_map(snapshot: bool = True) -> dict[str, dict[str, str | None]]:
    """SECID -> {emitent_inn, emitent_title} across the whole MOEX stock-bonds
    universe (~10k securities incl. delisted/matured -- corporate + munis +
    non-OFZ sovereign issues). Used only for the universe screener's rating
    join (universe_pipeline.py).

    Necessary evil: fetch_bond_master()'s single-call endpoint does NOT
    expose emitent_inn at all -- verified empirically, requesting the column
    returns nothing, not even a null. The only endpoint that returns
    emitent_inn in bulk (`/iss/securities.json?engine=stock&market=bonds`,
    no `q`) is hard-capped at 100 rows/page with no larger `limit` accepted,
    so full coverage takes ~105 paginated requests (~45s). Still categorically
    cheaper than one fetch_bondization() call per screened universe bond
    (~650+ calls, each needing coupon-schedule parsing) -- this is a flat,
    fixed bulk metadata fetch, once per universe run, not per-bond.
    """
    out: dict[str, dict[str, str | None]] = {}
    all_rows: list[dict[str, Any]] = []
    start = 0
    page_size = 100
    while True:
        r = _get(f"{MOEX_BASE}/securities.json",
                 params={"engine": "stock", "market": "bonds",
                         "iss.meta": "off", "iss.only": "securities",
                         "start": start, "limit": page_size})
        rows = _rows(r.json(), "securities")
        if not rows:
            break
        all_rows.extend(rows)
        start += page_size
        time.sleep(0.05)  # polite pacing across ~100 sequential requests
    if snapshot:
        _snapshot("moex_emitent_map.json",
                  json.dumps(all_rows, ensure_ascii=False))
    for row in all_rows:
        secid = row.get("secid")
        if secid:
            out[secid] = {"emitent_inn": row.get("emitent_inn"),
                          "emitent_title": row.get("emitent_title")}
    return out


def fetch_security_qualified(secid: str, snapshot: bool = True) -> bool | None:
    """Reliable per-security ISQUALIFIEDINVESTORS, since fetch_bond_master()'s
    bulk endpoint always drops this field (see its docstring). Confirmed by
    live query that `/iss/securities/<SECID>.json`'s `description` block
    carries it correctly and with real variation (checked several secids,
    got both 0 and 1) -- the board-securities endpoints
    (`.../boards/<board>/securities.json`) do NOT expose it at all, even
    unrestricted, so that route was ruled out.

    One call per bond, so only used for the watchlist (~25-30 bonds), not
    the ~1000-bond universe screen -- there `qualified` stays the (always-0)
    heuristic from fetch_bond_master(), documented as such in the dashboard.
    """
    r = _get(f"{MOEX_BASE}/securities/{secid}.json", params={"iss.meta": "off"})
    if snapshot:
        _snapshot(f"moex_security_desc_{secid}.json", r.text)
    for row in _rows(r.json(), "description"):
        if row.get("name") == "ISQUALIFIEDINVESTORS":
            return row.get("value") == "1"
    return None


def fetch_marketdata(secids: list[str] | None = None, snapshot: bool = True) -> list[dict[str, Any]]:
    """EOD/delayed board data: last price (% of face), yield, volume."""
    cols = ["SECID", "LAST", "LASTVALUE", "YIELD", "DURATION", "VALTODAY",
            "NUMTRADES", "WAPRICE", "MATDATE", "SYSTIME", "ACCRUEDINT"]
    r = _get(f"{MOEX_BASE}/engines/stock/markets/bonds/securities.json",
             params={"iss.meta": "off", "iss.only": "marketdata",
                     "marketdata.columns": ",".join(cols)})
    if snapshot:
        _snapshot("moex_marketdata.json", r.text)
    rows = _rows(r.json(), "marketdata")
    if secids:
        s = set(secids)
        rows = [x for x in rows if x.get("SECID") in s]
    return rows


def fetch_bondization(secid: str, snapshot: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Coupon + amortization schedule for one bond."""
    r = _get(f"{MOEX_BASE}/securities/{secid}/bondization.json",
             params={"iss.meta": "off", "limit": "unlimited"})
    if snapshot:
        _snapshot(f"moex_bondization_{secid}.json", r.text)
    j = r.json()
    return {
        "coupons": _rows(j, "coupons"),
        "amortizations": _rows(j, "amortizations"),
    }


def fetch_cbr_zcyc(snapshot: bool = True) -> list[tuple[float, float]]:
    """CBR zero-coupon government yield curve as (tenor_years, rate_decimal).

    The CBR page exposes the parameter table; here we request the values page
    and parse the tenor/rate grid. Returns sorted points. Raises if the layout
    is unrecognised so we never silently ship a wrong curve.
    """
    r = _get(CBR_ZCYC)
    if snapshot:
        _snapshot("cbr_zcyc.html", r.text)
    pts = _parse_cbr_zcyc(r.text)
    if not pts:
        raise IngestError("CBR ZCYC parse produced no points; layout changed?")
    return sorted(pts)


# --- parsing helpers -------------------------------------------------------

def _rows(payload: dict, block: str) -> list[dict[str, Any]]:
    b = payload.get(block, {})
    cols = b.get("columns", [])
    return [dict(zip(cols, row)) for row in b.get("data", [])]


def _parse_cbr_zcyc(html: str) -> list[tuple[float, float]]:
    """Extract (tenor_years, rate_decimal) from the CBR ZCYC values table.

    CBR publishes rates in percent at tenors like 0.25, 0.5, 1, 2, ... years.
    We scan table cells for a tenor row and a rate row. This is intentionally
    defensive and returns [] on an unrecognised layout so the caller raises.
    """
    import re
    # Grab the most recent data row of the ZCYC table. The page renders a table
    # whose header cells are tenors (in years) and the latest row are the rates.
    # We look for numeric pairs; if the structure changes, this yields [] and
    # the caller raises rather than shipping garbage.
    tenors = re.findall(r'>\s*(0[.,]25|0[.,]5|0[.,]75|1|2|3|5|7|10|15|20|30)\s*<', html)
    if not tenors:
        return []
    # find the first data row: sequence of percent numbers after the tenor header
    nums = re.findall(r'>\s*(\d{1,2}[.,]\d{1,2})\s*<', html)
    # the sub-1y tenors (0.25/0.5/0.75) match the same decimal pattern as rates,
    # so the header row leaks that many duplicate entries into `nums` before the
    # real rate row begins — skip them.
    header_leak = sum(1 for t in tenors if "." in t or "," in t)
    nums = nums[header_leak:]
    # heuristic: take as many rate numbers as tenors, immediately following
    rates = [float(n.replace(",", ".")) / 100.0 for n in nums[: len(tenors)]]
    tvals = [float(t.replace(",", ".")) for t in tenors]
    return list(zip(tvals, rates))


# --- normalisation to typed records ---------------------------------------

def parse_date(s: str | None) -> date | None:
    if not s or s in ("0000-00-00",):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
