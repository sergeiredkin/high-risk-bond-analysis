"""Company-scan pipeline: CLI/script-side port of dashboard/template.html's
"Проверить компанию" panel (generateCompanyScan()) -- same system prompt
(bond_screening_prompt_v3.5.1.md, embedded unedited), same rendering
addendum (NC_RENDER_ADDENDUM below, kept in sync with the JS constant of the
same name by hand -- there is no build-time sharing mechanism between the
Python pipeline and the single static dashboard HTML file), same Anthropic
Messages API call shape (web_search hosted tool, ephemeral cache_control on
both system blocks). The difference is what happens with the result: the
browser only ever rendered a card for one session; this persists every scan
into `company_scan_result` (db.init_company_scan) so results can be queried
and compared over time, and a batch mode can run unattended.

API key discipline mirrors financials_pipeline.py/PARSER_API_KEY exactly:
env var (ANTHROPIC_API_KEY) with an optional explicit override (CLI --key),
nothing ever written to a config file, nothing hardcoded.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

from .compute import inflection
from .store import db

ROOT = Path(__file__).resolve().parents[2]
COMPANY_SCAN_PROMPT_PATH = ROOT / "skills" / "bond-narrative-card" / "bond_screening_prompt_v3.5.1.md"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-sonnet-5"
MAX_TOKENS = 6000
WEB_SEARCH_MAX_USES = 8
REQUEST_TIMEOUT_S = 180  # web-search-heavy calls run 30-90s per the browser panel's own status text

# Identical wording to dashboard/template.html's NC_RENDER_ADDENDUM constant
# -- output-formatting only, doesn't touch the analytical framework of
# bond_screening_prompt_v3.5.1.md, which is passed unedited as the first
# system block below.
NC_RENDER_ADDENDUM = """## Rendering Addendum (dashboard integration only — not part of the analytical framework)

After the full markdown report in the Step 16 format above, on a new line append exactly one fenced ```json code block (nothing after it) with this structure, for card rendering only. Every value must be consistent with the markdown report above -- restate facts already established there, do not introduce new ones. Use null / empty arrays for anything not applicable.

{
  "issuer_name": string, "date": "YYYY-MM-DD", "track": "Large" | "Small",
  "issuer_quality": "Strong" | "Adequate" | "Weak" | "Distressed", "why_rating": string,
  "bond_suitability": "Buy" | "Speculative" | "Watchlist" | "Avoid",
  "confidence": "High" | "Medium" | "Low", "data_coverage_pct": number,
  "traffic_lights": [{"name": string, "color": "green"|"yellow"|"red", "note": string}],
  "strengths": [{"title": string, "desc": string}],
  "risks": [{"title": string, "desc": string}],
  "bottom_line": string,
  "default_pathway": {"primary_risk": string, "pathway": [string], "mitigants": [string], "probability": string},
  "monitoring": [string],
  "data_conflicts": [string],
  "missing_data": [string]
}"""

CS_MIN_TENOR_YRS = 0.5
CS_MAX_TENOR_YRS = 10.0
# Task A (PD-adjusted expected-value inflection, compute/inflection.py)
# replaced the flat ≤35% cap with a rating-tier-aware floor/ceiling -- BUT
# only for RATED bonds. list_scan_candidates() below only ever considers the
# UNRATED subset by construction (`if r["rating"]: continue`), since a rated
# issuer is by definition already known to hybond and isn't a "new
# candidate" to scan -- so every bond this cap applies to has no PD to
# compute a principled threshold from, and the flat cap is the correct,
# honest fallback here, not a stopgap awaiting replacement. Sourced from
# inflection.UNRATED_MAX_YIELD_PCT (single definition now, not a second
# hand-copy) -- dashboard/template.html's CS_MAX_YIELD_PCT mirrors the same
# module's constant by hand on the JS side, same duplication pattern as
# CS_MIN/MAX_TENOR_YRS above (no shared build step between the static
# dashboard and this CLI).
CS_MAX_YIELD_PCT = inflection.UNRATED_MAX_YIELD_PCT

INN_RE = re.compile(r"^\d{10,12}$")


def resolve_inn(query: str) -> str | None:
    """Same heuristic as the browser's csResolveInn(): a bare 10-12 digit
    string is treated as an ИНН, anything else (a free-text company name)
    can't be tracked for drift-based rescanning."""
    q = (query or "").strip()
    return q if INN_RE.match(q) else None


def _api_key(explicit: str | None) -> str | None:
    return explicit or os.environ.get("ANTHROPIC_API_KEY")


def call_anthropic_scan(query: str, api_key: str) -> dict:
    """DEPRECATED along with scan_company()/run_scan_candidates() above --
    see their docstrings. One Messages API call -- same shape as
    callAnthropicMessages() + generateCompanyScan()'s call site in
    dashboard/template.html (system blocks, user text, web_search tool,
    max_tokens)."""
    system = [
        {"type": "text", "text": COMPANY_SCAN_PROMPT_PATH.read_text(encoding="utf-8"),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": NC_RENDER_ADDENDUM, "cache_control": {"type": "ephemeral"}},
    ]
    user_text = (
        f"Company / bond issuer to screen: {query}\n\n"
        "This issuer is not in hybond's database -- research it from scratch using "
        "free public web sources per every rule in the prompt above (track selection, "
        "financial health, refinancing profile, risk checks, bond terms, liquidity, "
        "event risk, default pathway, \"Why This Bond?\", data coverage, decision "
        "logic, cross-check). Produce the full Step 16 output, then the "
        "rendering-addendum JSON block."
    )
    body = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}],
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    t0 = time.monotonic()
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT_S)
    elapsed_ms = round((time.monotonic() - t0) * 1000)
    resp.raise_for_status()
    payload = resp.json()
    # Same filter as the browser: with web_search enabled, content also
    # carries server_tool_use/web_search_tool_result blocks -- only 'text'
    # blocks are the actual report.
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    return {"text": text, "usage": payload.get("usage", {}), "elapsed_ms": elapsed_ms}


def parse_scan_response(text: str) -> tuple[dict | None, str]:
    """Same split as the browser's parseNarrativeResponse(): one fenced
    ```json block -> structured card; everything before it -> markdown
    report. Falls back to markdown-only if the block is missing or doesn't
    parse -- never fabricates a card."""
    m = re.search(r"```json\s*([\s\S]*?)```", text)
    if not m:
        return None, text.strip()
    try:
        card = json.loads(m.group(1))
    except json.JSONDecodeError:
        card = None
    return card, text[:m.start()].strip()


def _usage_tokens(usage: dict) -> dict:
    stu = usage.get("server_tool_use") or {}
    return {
        "input_tokens": usage.get("input_tokens") or 0,
        "cache_creation_tokens": usage.get("cache_creation_input_tokens") or 0,
        "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "web_searches": stu.get("web_search_requests"),
    }


def _issuer_snapshot(con, inn: str | None) -> dict:
    """What hybond currently knows about this issuer (rating + filing
    count), for the Task-B drift snapshot stored on every scan row -- same
    two fields the browser's csRecordScan() snapshots, sourced live from
    the DB here instead of DATA.known_issuers (there is no exported DATA
    blob server-side)."""
    if not inn:
        return {"rating": None, "rated_on": None, "filing_count": 0, "filing_latest": None}
    db.init_rating(con)
    db.init_filing(con)
    rating_row = db.current_rating_row(con, inn)
    filing_row = con.execute(
        "SELECT COUNT(*) cnt, MAX(published) latest FROM filing WHERE issuer_id=?",
        (inn,)).fetchone()
    return {
        "rating": rating_row["rating"] if rating_row else None,
        "rated_on": rating_row["rated_on"] if rating_row else None,
        "filing_count": filing_row["cnt"] if filing_row else 0,
        "filing_latest": filing_row["latest"] if filing_row else None,
    }


def store_scan_result(
    con, *, query: str, markdown: str, card: dict | None,
    model: str = MODEL, elapsed_ms: int | None = None, tokens: dict | None = None,
) -> dict:
    """Persist one already-produced scan (markdown report + optional parsed
    JSON card) to `company_scan_result`. This is the persistence half of
    what used to be `scan_company()`'s single API-calling function, split out
    so it can be reused by two producers of a scan: `scan_company()` below
    (the deprecated keyed HTTP path) and, primarily now, the `skills/
    company-scan` skill -- Claude Code runs the methodology itself (its own
    web search, no API key, no HTTP call) and calls this via `hybond
    scan-company-store` (see __main__.cmd_scan_company_store) to write the
    row exactly like the old keyed path did. Same schema, same table, same
    downstream dashboard rendering either way -- the row doesn't record
    which path produced it beyond the free-text `model` field."""
    tok = tokens or {"input_tokens": None, "output_tokens": None,
                      "cache_read_tokens": None, "cache_creation_tokens": None,
                      "web_searches": None}
    inn = resolve_inn(query)
    snap = _issuer_snapshot(con, inn)

    scanned_at = datetime.now().isoformat(timespec="seconds")
    db.init_company_scan(con)
    row_id = db.insert_company_scan_result(
        con, inn=inn, query=query, scanned_at=scanned_at, model=model,
        issuer_name=(card or {}).get("issuer_name"),
        track=(card or {}).get("track"),
        issuer_quality=(card or {}).get("issuer_quality"),
        bond_suitability=(card or {}).get("bond_suitability"),
        confidence=(card or {}).get("confidence"),
        data_coverage_pct=(card or {}).get("data_coverage_pct"),
        raw_markdown=markdown,
        json_addendum=json.dumps(card, ensure_ascii=False) if card is not None else None,
        input_tokens=tok.get("input_tokens"), output_tokens=tok.get("output_tokens"),
        cache_read_tokens=tok.get("cache_read_tokens"), cache_creation_tokens=tok.get("cache_creation_tokens"),
        web_searches=tok.get("web_searches"), elapsed_ms=elapsed_ms,
        rating_snapshot=snap["rating"], rated_on_snapshot=snap["rated_on"],
        filing_count_snapshot=snap["filing_count"], filing_latest_snapshot=snap["filing_latest"],
    )
    con.commit()

    return {
        "id": row_id, "inn": inn, "query": query, "scanned_at": scanned_at,
        "card": card, "markdown_len": len(markdown),
        "tokens": tok, "elapsed_ms": elapsed_ms,
        "no_json_block": card is None,
    }


def scan_company(con, query: str, api_key: str | None = None) -> dict:
    """DEPRECATED: keyed HTTP path. Prefer the `company-scan` Claude Code
    skill (skills/company-scan/SKILL.md), which runs the same methodology
    via Claude Code's own web search -- no ANTHROPIC_API_KEY needed -- and
    persists through store_scan_result() above / `hybond scan-company-store`.
    Kept working (schema and rendering are unchanged either way) for
    unattended/scripted use where a key is already provisioned."""
    key = _api_key(api_key)
    if not key:
        return {"error": "no ANTHROPIC_API_KEY set and none passed; "
                         "company scan needs a key (see feedback: same "
                         "discipline as PARSER_API_KEY -- env var or --key, never a file). "
                         "Prefer the company-scan Claude Code skill instead -- no key needed."}
    result = call_anthropic_scan(query, key)
    card, markdown = parse_scan_response(result["text"])
    tok = _usage_tokens(result["usage"])
    return store_scan_result(
        con, query=query, markdown=markdown, card=card,
        model=MODEL, elapsed_ms=result["elapsed_ms"], tokens=tok,
    )


def list_scan_candidates(con) -> list[dict]:
    """Server-side port of renderCompanyScanCandidates()'s Part A (dashboard/
    template.html): one row per issuer NOT already in hybond's `issuer`
    table, cleared vdo_scope + liquidity + tenor + "no rating" gates, best
    (highest G-spread) bond kept per issuer. Reads live from
    universe_snapshot/issuer -- there is no exported DATA.universe_now blob
    server-side, so this re-derives the same thing from the same source
    table, same gates, same VDO_MIN_VOLUME constant the browser panel's
    DATA.promotion_thresholds.min_volume is itself sourced from
    (promote.py re-exports it, see that module's docstring)."""
    from .universe_pipeline import VDO_MIN_VOLUME

    db.init_universe(con)
    db.init_company_scan(con)
    snap_date = db.latest_universe_snap_date(con)
    if not snap_date:
        return []
    known = {r["issuer_id"] for r in con.execute("SELECT issuer_id FROM issuer")}
    # Deliberately NOT excluding already-scanned INNs here (unlike the JS
    # "new candidates" chip list) -- run_scan_candidates() below relies on
    # this list still containing previously-scanned issuers so its own
    # needs_rescan() drift check can decide skip-vs-rescan per issuer. That
    # decision happens one layer up; folding it in here would make a
    # previously-scanned-with-real-drift issuer silently disappear instead
    # of being rescanned.
    rows = con.execute(
        "SELECT secid, emitent_inn, emitent_title, g_spread_bp, ytm, tenor_yrs, "
        "volume, vdo_scope, rating FROM universe_snapshot WHERE snap_date=?",
        (snap_date,)).fetchall()

    by_issuer: dict[str, dict] = {}
    for r in rows:
        inn = r["emitent_inn"]
        if not inn or inn in known:
            continue  # already watchlisted or already investigated (promoted or explicitly held back)
        if not r["vdo_scope"]:
            continue
        vol = r["volume"]
        if vol is None or vol < VDO_MIN_VOLUME:
            continue
        tenor = r["tenor_yrs"]
        if tenor is None or tenor < CS_MIN_TENOR_YRS or tenor > CS_MAX_TENOR_YRS:
            continue
        ytm = r["ytm"]
        if ytm is not None and ytm * 100 > CS_MAX_YIELD_PCT:
            continue  # distressed-quote outlier, not a real screening candidate
        if r["rating"]:  # candidates are the UNRATED subset only, same as the browser panel
            continue
        gsp = r["g_spread_bp"]
        cur = by_issuer.get(inn)
        cur_gsp = cur["g_spread_bp"] if cur else None
        if cur is None or (gsp if gsp is not None else float("-inf")) > (cur_gsp if cur_gsp is not None else float("-inf")):
            by_issuer[inn] = {
                "inn": inn, "name": r["emitent_title"] or inn, "secid": r["secid"],
                "g_spread_bp": gsp,
                "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
                "tenor_yrs": tenor,
            }
    return sorted(
        by_issuer.values(),
        key=lambda c: c["g_spread_bp"] if c["g_spread_bp"] is not None else float("-inf"),
        reverse=True,
    )


def needs_rescan(con, inn: str, last_scan: dict) -> list[str]:
    """Server-side port of the browser's Part B comparison (rating identity/
    date, filing count increase) -- against a company_scan_result row
    instead of a localStorage snapshot. Returns human-readable reasons
    (empty list = no drift, skip)."""
    reasons = []
    snap = _issuer_snapshot(con, inn)
    if snap["rating"] != last_scan.get("rating_snapshot") or snap["rated_on"] != last_scan.get("rated_on_snapshot"):
        reasons.append(f"новый рейтинг {snap['rating']} ({snap['rated_on'] or '?'})"
                        if snap["rating"] else "рейтинг снят/изменился")
    if (snap["filing_count"] or 0) > (last_scan.get("filing_count_snapshot") or 0):
        reasons.append(f"новые раскрытия AZIPI ({last_scan.get('filing_count_snapshot') or 0} -> {snap['filing_count']})")
    return reasons


def list_scan_todo(con, include_rescan: bool = True) -> list[dict]:
    """The same candidate-plus-rescan-due todo list run_scan_candidates()
    below acts on, exposed standalone with no API key and no scanning --
    just the list. This is what `hybond scan-candidates-list` prints and
    what the `company-scan` Claude Code skill's batch mode pulls from: the
    skill runs each scan itself (its own web search) in-session and decides
    how many to do, so there's no need for the keyed path's --limit safety
    net here (see run_scan_candidates's docstring for why that guard exists
    there but not here)."""
    db.init_company_scan(con)
    already_scanned = db.latest_company_scan_by_inn(con)
    candidates = list_scan_candidates(con)
    todo = [{"inn": c["inn"], "name": c["name"], "reason": "новый кандидат"}
            for c in candidates if c["inn"] not in already_scanned]
    if include_rescan:
        for inn, last in already_scanned.items():
            reasons = needs_rescan(con, inn, last)
            if reasons:
                todo.append({"inn": inn, "name": last.get("query") or inn, "reason": "; ".join(reasons)})
    return todo


def run_scan_candidates(
    con, api_key: str | None = None, limit: int | None = None,
    include_rescan: bool = True, log: Callable[[str], None] = lambda s: None,
) -> dict:
    """DEPRECATED: keyed HTTP batch path, built on scan_company() above.
    Prefer asking Claude Code to run the `company-scan` skill across
    `hybond scan-candidates-list`'s output in-session instead -- same todo
    list (list_scan_todo), no key, no unattended-batch cost risk since a
    human is directly in the loop deciding how many to do. Kept working for
    scripted/cron use where a key is already provisioned.

    Scans the auto-flagged candidate list (list_scan_candidates) plus, if
    include_rescan, any previously-scanned issuer whose rating/filing state
    has drifted since its last scan (needs_rescan) -- the same two source
    lists the browser's amber panel shows (Part A / Part B), just driven
    from the DB instead of localStorage. Skips anything already scanned with
    no drift (Task B). `limit` caps how many NEW API calls this run makes --
    candidates beyond the cap are simply left for a later run, not dropped
    -- so an unattended batch can't accidentally trigger dozens of paid
    web-search-heavy calls at once. `log` is called with one progress/cost
    line per scan (stderr in the CLI) -- reuses the same token-count fields
    the browser's cost-line does, see _usage_tokens."""
    key = _api_key(api_key)
    if not key:
        return {"error": "no ANTHROPIC_API_KEY set and none passed; company scan needs a key. "
                         "Prefer the company-scan Claude Code skill instead -- no key needed."}

    db.init_company_scan(con)
    already_scanned = db.latest_company_scan_by_inn(con)
    todo = list_scan_todo(con, include_rescan=include_rescan)
    candidates = list_scan_candidates(con)
    total_todo = len(todo)
    if limit is not None:
        todo = todo[:limit]

    results = []
    total_in = total_out = total_cache_read = total_searches = 0
    for i, item in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {item['name']} ({item['inn']}) -- {item['reason']}")
        r = scan_company(con, item["inn"], api_key=key)
        if "error" in r:
            log(f"  ERROR: {r['error']}")
            results.append({**item, "error": r["error"]})
            continue
        tok = r["tokens"]
        total_in += tok["input_tokens"] + tok["cache_creation_tokens"] + tok["cache_read_tokens"]
        total_out += tok["output_tokens"]
        total_cache_read += tok["cache_read_tokens"]
        if tok["web_searches"]:
            total_searches += tok["web_searches"]
        card = r.get("card") or {}
        verdict = (f"issuer={card.get('issuer_quality', '?')} bond={card.get('bond_suitability', '?')}"
                   if card else "no JSON card parsed")
        log(f"  done in {r['elapsed_ms']/1000:.1f}s -- {verdict} -- "
            f"running total: ~{total_in} in ({total_cache_read} cached) / {total_out} out"
            + (f" -- {total_searches} web searches" if total_searches else ""))
        results.append({**item, "id": r["id"], "card": card or None})

    return {
        "candidates_found": len(candidates),
        "already_scanned": len(already_scanned),
        "due_this_run": total_todo,
        "scanned_this_run": len([r for r in results if "error" not in r]),
        "errors": len([r for r in results if "error" in r]),
        "skipped_by_limit": max(total_todo - len(todo), 0),
        "total_input_tokens": total_in, "total_output_tokens": total_out,
        "total_cache_read_tokens": total_cache_read, "total_web_searches": total_searches,
        "results": results,
    }
