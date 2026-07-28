"""Slice 3 scrape pipeline: pull the e-disclosure.azipi.ru message feed for
each watchlist issuer, dedupe against what we've already stored, persist
genuinely new filings, and fetch their bodies for the classifier. No LLM
here — this only gathers and dedupes; classification is the skill's job.

Two-tier fetch, matched to watchlist issuers by ИНН (watchlist keys already
are ИНН, so no separate id mapping is needed):
  1. PRIMARY — fetch the homepage once; it lists today's messages across all
     issuers with ИНН inline. Cheap: one request covers the whole watchlist.
  2. SECONDARY — for any watchlist issuer with zero hits in today's feed
     (quiet today, or newly added), look them up by ИНН via /search/ and pull
     their organization page's message history. One extra request pair per
     such issuer, only run for issuers the primary pass didn't already cover.

Plain HTTP throughout (HttpBackend) — no JS gate on this source, no Selenium
needed for the default path.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .scrape import edisclosure as ed
from .scrape import smartlab as sl
from .store import db


def load_watchlist(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_scrape(
    watchlist_path: Path,
    use_selenium: bool = False,
    fetch_bodies: bool = True,
    snap_date: date | None = None,
    debug: bool = False,
    use_smartlab: bool = False,
) -> dict:
    """debug: dump the homepage feed HTML (and, if the secondary path fires,
    the first issuer's organization-page HTML) to data/raw/edisclosure/ before
    parsing, for inspecting the real DOM when a parser yields nothing.

    use_smartlab: also scrape smart-lab.ru's bonds news feed (SECONDARY source
    — supplements AZIPI, doesn't replace it) for issuers matched by name/ticker
    in smartlab.INTERFAX_ISSUERS. Filings from this source are marked
    edisclosure_id='smartlab' so hy-event-classify treats them as secondary."""
    snap_date = snap_date or date.today()
    wl = load_watchlist(watchlist_path)
    issuer_ids = [k for k in wl if not k.startswith("_")]
    backend = ed.SeleniumBackend() if use_selenium else ed.HttpBackend()

    con = db.connect()
    db.init_db(con)
    db.init_filing(con)

    summary = {"snap_date": snap_date.isoformat(), "issuers": {}, "new_total": 0,
               "errors": {}}

    # --- primary: homepage feed, one request for the whole watchlist -------
    try:
        home_html = ed.fetch_homepage(backend)
    except Exception as e:  # noqa: BLE001
        home_html = ""
        summary["errors"]["_homepage"] = str(e)
    if debug and home_html:
        debug_path = ed.RAW_DIR / "debug_page.html"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(home_html, encoding="utf-8")
        summary["debug_page"] = str(debug_path)

    home_filings_by_inn: dict[str, list] = {}
    if home_html:
        for f in ed.parse_homepage_feed(home_html):
            home_filings_by_inn.setdefault(f.issuer_id, []).append(f)

    debug_org_written = False

    for issuer_id in issuer_ids:
        filings = home_filings_by_inn.get(issuer_id, [])

        # --- secondary: this issuer had nothing in today's feed — check
        # their own message history via ИНН search instead.
        if not filings:
            try:
                org_url = ed.search_org_by_inn(issuer_id, backend)
                if org_url:
                    org_html = backend.get(org_url)
                    if debug and not debug_org_written:
                        debug_org_path = ed.RAW_DIR / "debug_org_page.html"
                        debug_org_path.write_text(org_html, encoding="utf-8")
                        summary["debug_org_page"] = str(debug_org_path)
                        debug_org_written = True
                    filings = ed.parse_org_page_feed(org_html, issuer_id)
            except Exception as e:  # noqa: BLE001
                summary["errors"][issuer_id] = str(e)
                continue

        seen = db.seen_filing_hashes(con, issuer_id)
        fresh = ed.new_filings_since(filings, seen)

        for f in fresh:
            body = None
            if fetch_bodies and f.url:
                try:
                    body = ed.fetch_message_body(f.url, backend)
                except Exception:  # noqa: BLE001
                    body = None
            db.insert_filing(
                con, body_hash=f.body_hash, issuer_id=issuer_id,
                edisclosure_id=f.edisclosure_id, published=f.published,
                title=f.title, url=f.url, fetched_on=snap_date.isoformat())
            # body text is cached to disk by the scraper's snapshot; the filing
            # row holds the pointer. Classifier reads body from the snapshot/url.

        summary["issuers"][issuer_id] = {
            "parsed": len(filings), "new": len(fresh),
            "titles": [f.title for f in fresh[:5]],
        }
        summary["new_total"] += len(fresh)

    # --- secondary source: smart-lab.ru, community news + IR posts ---------
    if use_smartlab:
        try:
            sl_html = sl.fetch_feed(backend)
            sl_filings = [f for f in sl.parse_feed(sl_html) if f.issuer_id in issuer_ids]
        except Exception as e:  # noqa: BLE001
            sl_filings = []
            summary["errors"]["_smartlab"] = str(e)

        sl_new_total = 0
        for issuer_id in {f.issuer_id for f in sl_filings}:
            issuer_filings = [f for f in sl_filings if f.issuer_id == issuer_id]
            seen = db.seen_filing_hashes(con, issuer_id)
            fresh = ed.new_filings_since(issuer_filings, seen)
            for f in fresh:
                body = None
                if fetch_bodies and f.url:
                    try:
                        body = ed.fetch_message_body(f.url, backend)
                    except Exception:  # noqa: BLE001
                        body = None
                db.insert_filing(
                    con, body_hash=f.body_hash, issuer_id=issuer_id,
                    edisclosure_id=sl.SOURCE_MARKER, published=f.published,
                    title=f.title, url=f.url, fetched_on=snap_date.isoformat())
            entry = summary["issuers"].setdefault(
                issuer_id, {"parsed": 0, "new": 0, "titles": []})
            entry["parsed"] += len(issuer_filings)
            entry["new"] += len(fresh)
            entry["titles"] = (entry["titles"] + [f"[Smart-Lab] {f.title}" for f in fresh])[:5]
            sl_new_total += len(fresh)
        summary["new_total"] += sl_new_total
        summary["smartlab_new"] = sl_new_total

    con.commit()
    con.close()
    return summary


def classify_queue_view(snap_date: date | None = None) -> dict:
    """What the classifier skill would work on next: unclassified filings. This
    is the hand-off point to hy-event-classify (manual invoke now, API later)."""
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    rows = db.unclassified_filings(con)
    con.close()
    return {
        "unclassified_count": len(rows),
        "queue": [{"issuer_id": r["issuer_id"], "published": r["published"],
                   "title": r["title"], "url": r["url"],
                   "body_hash": r["body_hash"],
                   "source": "smartlab" if r["edisclosure_id"] == sl.SOURCE_MARKER
                             else "primary"} for r in rows[:50]],
    }
