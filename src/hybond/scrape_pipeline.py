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

import hashlib
import logging
import sqlite3
import time
from datetime import date
from pathlib import Path

import requests

from .classify_utils import queue_bucket
from .scrape import edisclosure as ed
from .scrape import smartlab as sl
from .store import db

logger = logging.getLogger(__name__)

# Total-runtime guard (2026-08-21, see edisclosure.py's module docstring for
# the live incident this whole task is about): even with per-call timeouts,
# a watchlist-sized run making one request per quiet issuer can still add up
# past any reasonable "this should be quick" expectation. 300s is well above
# a normal run (watchlist size x a few seconds per request) but short enough
# that a stuck/slow run gets cut off well before it becomes a multi-hour
# hang like the live incident.
MAX_RUNTIME_S = 300


def run_scrape(
    watchlist_path: Path | None,
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
    backend = ed.SeleniumBackend() if use_selenium else ed.HttpBackend()
    start_time = time.monotonic()

    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    # Issuer scope is `issuer` WHERE watchlist_tier IN (1, 2), matching
    # pipeline.py/financials_pipeline.py -- see the Watchlist Tier System
    # follow-up task, 2026-08-20. watchlist_path is accepted for signature
    # compatibility with existing callers but no longer read here; matching
    # is by ИНН, which the issuer table already carries as its own key.
    issuer_ids = [r["issuer_id"] for r in db.list_issuers_by_tier(con, (1, 2))]

    summary = {"snap_date": snap_date.isoformat(), "issuers": {}, "new_total": 0,
               "errors": {}, "timed_out": False}

    # --- primary: homepage feed, one request for the whole watchlist -------
    try:
        home_html = ed.fetch_homepage(backend)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        logger.warning("network error fetching AZIPI homepage feed: %s", e)
        home_html = ""
        summary["errors"]["_homepage"] = str(e)
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
        if time.monotonic() - start_time > MAX_RUNTIME_S:
            logger.warning(
                "run_scrape exceeded %ss total runtime, stopping before issuer %s "
                "(%d/%d issuers processed)",
                MAX_RUNTIME_S, issuer_id, len(summary["issuers"]), len(issuer_ids))
            summary["timed_out"] = True
            break

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
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning("network error scraping issuer %s (org search): %s", issuer_id, e)
                summary["errors"][issuer_id] = str(e)
                continue
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
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    logger.warning("network error fetching filing body %s: %s", f.url, e)
                    body = None
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
    if use_smartlab and time.monotonic() - start_time > MAX_RUNTIME_S:
        logger.warning("run_scrape exceeded %ss total runtime, skipping Smart-Lab pass",
                        MAX_RUNTIME_S)
        summary["timed_out"] = True
        use_smartlab = False
    if use_smartlab:
        try:
            sl_html = sl.fetch_feed(backend)
            sl_filings = [f for f in sl.parse_feed(sl_html) if f.issuer_id in issuer_ids]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning("network error fetching Smart-Lab feed: %s", e)
            sl_filings = []
            summary["errors"]["_smartlab"] = str(e)
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
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                        logger.warning("network error fetching Smart-Lab filing body %s: %s", f.url, e)
                        body = None
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


def run_market_notes(snap_date: date | None = None) -> dict:
    """Ingest all market-wide RSS feeds into `market_note` -- secondary
    business-news/announcement signal, distinct from the `filing`/`event`
    tables (regulatory disclosures). severity/category are left NULL:
    classifying this source is a separate, later step (hy-event-classify
    doesn't yet have a market_note input path), this only ingests + dedupes.

    Three feeds as of the MOEX Listing/CBR Press task (2026-08-17):
      - smart-lab.ru general news (scrape/smartlab_news.py) -- issuer-name-
        matched against NEWS_ALIASES; items with no match are dropped (its
        own existing behaviour, unchanged).
      - MOEX Listing + CBR Press (scrape/rss_generic.py) -- market-wide, no
        issuer match attempted (see that module's docstring for why: CBR
        press releases essentially never name a single watchlist issuer).
    Each feed is fetched independently -- one feed failing (network error,
    malformed XML) does not block the others; per-feed errors are collected
    under result['feeds'][source]['error'] as well as folded into the
    top-level 'error' string for backward compatibility with existing
    callers (cmd_market_notes) that only checked the aggregate."""
    from .scrape import rss_generic as rg
    from .scrape import smartlab_news as sln
    snap_date = snap_date or date.today()
    backend = ed.HttpBackend()

    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)

    result = {"snap_date": snap_date.isoformat(), "parsed": 0, "new": 0,
              "error": None, "feeds": {}}
    errors = []

    # --- smart-lab.ru general news: existing issuer-matched behaviour ------
    feed_result = {"parsed": 0, "new": 0, "error": None}
    try:
        xml_text = sln.fetch_feed(backend)
        notes = sln.parse_feed(xml_text)
        feed_result["parsed"] = len(notes)
        for n in notes:
            inserted = db.insert_market_note(
                con, source=sln.SOURCE_MARKER, issuer_id=n.issuer_id,
                occurred=n.occurred, headline=n.headline, summary=n.summary,
                url=n.url, severity=None, category=None, confidence=None,
                fetched_on=snap_date.isoformat(), body_hash=n.body_hash,
                feed_url=sln.FEED_URL)
            if inserted:
                feed_result["new"] += 1
        con.commit()
    except Exception as e:  # noqa: BLE001
        feed_result["error"] = str(e)
        errors.append(f"{sln.SOURCE_MARKER}: {e}")
    result["feeds"][sln.SOURCE_MARKER] = feed_result
    result["parsed"] += feed_result["parsed"]
    result["new"] += feed_result["new"]

    # --- MOEX Listing + CBR Press: market-wide, no issuer gate ------------
    for cfg in (rg.MOEX_LISTING_FEED, rg.CBR_PRESS_FEED):
        feed_result = {"parsed": 0, "new": 0, "error": None}
        try:
            xml_text = rg.fetch_feed(backend, cfg)
            notes = rg.parse_feed(xml_text)
            feed_result["parsed"] = len(notes)
            for n in notes:
                inserted = db.insert_market_note(
                    con, source=cfg.source, issuer_id=n.issuer_id,
                    occurred=n.occurred, headline=n.headline, summary=n.summary,
                    url=n.url, severity=None, category=None, confidence=None,
                    fetched_on=snap_date.isoformat(), body_hash=n.body_hash,
                    feed_url=cfg.feed_url)
                if inserted:
                    feed_result["new"] += 1
            con.commit()
        except Exception as e:  # noqa: BLE001
            feed_result["error"] = str(e)
            errors.append(f"{cfg.source}: {e}")
        result["feeds"][cfg.source] = feed_result
        result["parsed"] += feed_result["parsed"]
        result["new"] += feed_result["new"]

    con.close()
    result["error"] = "; ".join(errors) if errors else None
    return result


def run_google_news(snap_date: date | None = None, limit: int | None = None) -> dict:
    """Google News RSS per-issuer monitoring (`hybond google-news`, Google
    News RSS Monitoring task, 2026-08-24): fetches news for every tier=1
    watchlist issuer, storing genuinely new items as market_note (severity/
    category NULL, same later-classify convention as run_market_notes()'s
    other feeds).

    Unlike run_market_notes()'s market-wide feeds, this is one HTTP request
    PER issuer -- SLEEP_BETWEEN_ISSUERS_S between requests (2s) to stay
    under Google's rate limiting, so a full tier=1 run takes roughly
    (issuer count x 2s) at minimum; `limit` caps how many issuers are
    processed in one run (--limit N) for a quicker manual/interactive check,
    the hourly cron path omits it and processes the whole tier=1 list.

    One issuer's fetch failing (after google_news.fetch_issuer_news()'s own
    internal timeout retries are exhausted) is recorded in result['errors']
    and does not block the remaining issuers -- same per-item fault
    isolation as run_market_notes()'s per-feed try/except.

    Dedup is belt-and-suspenders: seen_urls (an explicit URL pre-check, the
    task's own stated dedup rule) skips a known URL before even attempting
    an insert, on top of insert_market_note's own body_hash INSERT OR
    IGNORE (still needed for the case of two DIFFERENT issuer queries
    incidentally surfacing the same URL within one run, which the
    once-per-run seen_urls set already covers, but keeps as defense in
    depth rather than relying on a single dedup path)."""
    from .scrape import google_news as gn
    snap_date = snap_date or date.today()

    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)

    issuers = db.list_issuers_by_tier(con, 1)
    if limit is not None:
        issuers = issuers[:limit]

    seen_urls = db.seen_market_note_urls(con)
    result = {"snap_date": snap_date.isoformat(), "scanned": len(issuers),
              "parsed": 0, "new": 0, "skipped_dup": 0, "errors": []}

    for i, row in enumerate(issuers):
        issuer_id, name = row["issuer_id"], row["name"]
        try:
            items = gn.fetch_issuer_news(name, issuer_id)
        except Exception as e:  # noqa: BLE001 -- one issuer's failure must not block the rest
            result["errors"].append(f"{name} ({issuer_id}): {e}")
            items = []
        result["parsed"] += len(items)
        for item in items:
            if item["url"] in seen_urls:
                result["skipped_dup"] += 1
                continue
            body_hash = hashlib.sha1(
                f"{item['title']}|{item['url']}".encode("utf-8")).hexdigest()[:16]
            inserted = db.insert_market_note(
                con, source=gn.SOURCE, issuer_id=issuer_id,
                occurred=item["published"], headline=item["title"], summary=None,
                url=item["url"], severity=None, category=None, confidence=None,
                fetched_on=snap_date.isoformat(), body_hash=body_hash,
                feed_url=gn.build_url(name))
            if inserted:
                result["new"] += 1
                seen_urls.add(item["url"])
            else:
                result["skipped_dup"] += 1
        con.commit()
        if i < len(issuers) - 1:
            time.sleep(gn.SLEEP_BETWEEN_ISSUERS_S)

    con.close()
    return result


NKR_SLEEP_BETWEEN_ISSUERS_S = 2.0


def run_nkr_backfill(inn: str | None = None) -> dict:
    """НКР historical rating backfill (`hybond backfill-ratings --agency nkr`,
    Rating History Backfill task, 2026-08-25): for every tier=1 issuer that
    currently carries an active (is_current=1) НКР rating, look the issuer
    up on ratings.ru by name and insert any of its historical rating
    ACTIONS this project's `rating` table doesn't already have.

    Scope is deliberately "issuers already carrying a live НКР rating" per
    the task spec, not "every tier=1 issuer" -- looking up an issuer НКР
    never rated on the bulk listing would either find nothing (wasted
    request) or, worse, risk a wrong-company match on a common name.

    Issuer discovery fetches ratings.ru's bulk `/ratings/issuers/` listing
    ONCE up front (nkr.fetch_issuers_page()/parse_issuers(), the same
    already-proven bulk source this project's earlier НКР pass used to
    apply 22 current ratings on 2026-07-31) and resolves every issuer's
    slug against that one in-memory table via nkr.find_issuer_slug(...,
    rows=...) -- not a live per-issuer /search/ request, which was tried
    first and dropped (it returned zero results for a short name like
    'АПРИ' that the bulk listing found immediately).

    `inn` restricts the run to one issuer_id (the task's own --inn flag),
    otherwise every qualifying tier=1 issuer is processed with
    NKR_SLEEP_BETWEEN_ISSUERS_S between requests (politeness -- same
    2s-between-issuers convention as run_google_news()).

    insert_historical_rating() is idempotent on the (issuer_id, agency,
    rated_on) primary key, so "skip if row already exists" (the task's own
    requirement) needs no separate existence check here -- re-running this
    against an issuer already fully backfilled inserts nothing new.

    Flags (result['newer_than_current']) any scraped action dated AFTER
    this project's current is_current=1 row for that issuer+agency -- that
    would mean our "current" rating is actually stale, which is a real
    data-integrity finding worth surfacing, not something this backfill
    (scoped to HISTORY, not to refreshing the current rating) silently
    fixes on its own."""
    from .scrape import nkr

    con = db.connect()
    db.init_db(con)
    db.init_rating(con)

    query = ("SELECT DISTINCT r.issuer_id, i.name, r.rated_on AS current_rated_on "
             "FROM rating r JOIN issuer i ON i.issuer_id = r.issuer_id "
             "WHERE r.agency=? AND r.is_current=1 AND i.watchlist_tier=1")
    params: list = [nkr.AGENCY]
    if inn is not None:
        query += " AND r.issuer_id=?"
        params.append(inn)
    issuers = con.execute(query, params).fetchall()

    result = {"scanned": len(issuers), "total_new": 0, "new_by_issuer": {},
              "not_found": [], "newer_than_current": [], "errors": []}
    if not issuers:
        con.close()
        return result

    bulk_rows = nkr.parse_issuers(nkr.fetch_issuers_page(snapshot=False))

    for i, row in enumerate(issuers):
        issuer_id, name = row["issuer_id"], row["name"]
        try:
            slug = nkr.find_issuer_slug(name, rows=bulk_rows)
            if slug is None:
                result["not_found"].append(f"{name} ({issuer_id})")
                continue
            actions = nkr.fetch_issuer_ratings(slug)
        except Exception as e:  # noqa: BLE001 -- one issuer's failure must not block the rest
            result["errors"].append(f"{name} ({issuer_id}): {e}")
            continue

        new_count = 0
        for a in actions:
            if row["current_rated_on"] and a["rated_on"] > row["current_rated_on"]:
                result["newer_than_current"].append(
                    f"{name} ({issuer_id}): scraped {a['rated_on']} ({a['rating']}) is "
                    f"newer than our current row ({row['current_rated_on']})")
            # db.connect()'s 5s busy_timeout can still lose a race against a
            # long-held writer (the hourly/30-min auto-classify cron holds
            # its connection open for 30-40+ min at a time -- see that
            # connect()'s own comment) -- retried here rather than letting
            # one transient collision abort the whole remaining tier=1 run.
            for attempt in range(5):
                try:
                    inserted = db.insert_historical_rating(
                        con, issuer_id=issuer_id, agency=nkr.AGENCY, rating=a["rating"],
                        outlook=a["outlook"], action=a["action"], rated_on=a["rated_on"],
                        source=nkr.SOURCE)
                    break
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower() or attempt == 4:
                        raise
                    time.sleep(3)
            if inserted:
                new_count += 1
        for attempt in range(5):
            try:
                con.commit()
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 4:
                    raise
                time.sleep(3)
        if new_count:
            result["new_by_issuer"][name] = new_count
            result["total_new"] += new_count

        if i < len(issuers) - 1:
            time.sleep(NKR_SLEEP_BETWEEN_ISSUERS_S)

    con.close()
    return result


def market_note_queue_view() -> dict:
    """market_note counterpart to classify_queue_view: rows still awaiting
    classification (severity IS NULL). hy-event-classify reads headline+summary
    for these (no filing body -- see the skill's Inputs section) and stores the
    result via db.set_market_note_classification.

    Bucketed by classify_utils.queue_bucket() against the headline, same
    noise/priority pre-filter classify_queue_view() applies to filings below
    -- a DISPLAY split only, nothing here is written back to the DB.
    Source-aware as of the MOEX Listing/CBR Press task (2026-08-17):
    queue_bucket() takes the row's own `source` so moex_listing/cbr_press
    rows use their own pattern lists instead of the AZIPI-oriented default."""
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    rows = db.unclassified_market_notes(con)
    con.close()

    def to_dict(r):
        return {"note_id": r["note_id"], "issuer_id": r["issuer_id"],
                "occurred": r["occurred"], "headline": r["headline"],
                "summary": r["summary"], "url": r["url"], "source": r["source"]}

    priority, normal, noise = [], [], []
    for r in rows:
        bucket = queue_bucket(r["headline"], source=r["source"])
        (priority if bucket == "priority" else noise if bucket == "noise" else normal).append(r)

    return {
        "unclassified_count": len(rows),
        "priority_count": len(priority), "normal_count": len(normal), "noise_count": len(noise),
        "priority": [to_dict(r) for r in priority[:50]],
        "queue": [to_dict(r) for r in normal[:50]],
        "noise": [to_dict(r) for r in noise[:50]],
    }


def classify_queue_view(snap_date: date | None = None) -> dict:
    """What the classifier skill would work on next: unclassified filings. This
    is the hand-off point to hy-event-classify (manual invoke now, API later).

    Bucketed by classify_utils.queue_bucket() against the title into
    priority/normal/noise -- a DISPLAY split only (see that module's
    docstring); everything is still in `filing` with classified=0 regardless
    of bucket. `queue` (the actually-actionable list) stays the "normal"
    bucket for backward compatibility with existing callers of this dict."""
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    rows = db.unclassified_filings(con)
    con.close()

    def to_dict(r):
        return {"issuer_id": r["issuer_id"], "published": r["published"],
                "title": r["title"], "url": r["url"], "body_hash": r["body_hash"],
                "source": "smartlab" if r["edisclosure_id"] == sl.SOURCE_MARKER else "primary"}

    priority, normal, noise = [], [], []
    for r in rows:
        bucket = queue_bucket(r["title"])
        (priority if bucket == "priority" else noise if bucket == "noise" else normal).append(r)

    return {
        "unclassified_count": len(rows),
        "priority_count": len(priority), "normal_count": len(normal), "noise_count": len(noise),
        "priority": [to_dict(r) for r in priority[:50]],
        "queue": [to_dict(r) for r in normal[:50]],
        "noise": [to_dict(r) for r in noise[:50]],
    }
