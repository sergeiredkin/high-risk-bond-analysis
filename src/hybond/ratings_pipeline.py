"""Daily MOEX CCI rating refresh (`hybond refresh-ratings`).

Runs in daily_run.sh right before `hybond universe`, so today's screen,
promote shortlist and health scans see today's ratings.

Workload, kept incremental so the free anonymous endpoint sees the daily
minimum (Phase 0 measured a full pass at ~570 calls / ~5 min at 0.5 s):
  1. Targets: every issuer behind a bond in the last UNIVERSE_LOOKBACK_DAYS
     of universe_snapshot (the morning snapshot alone is ~2/3 of the day's
     universe), plus tier 1/2 issuers.
  2. Issuer call per emitent_id, skipped when that target was fetched
     successfully within ttl_hours -- daily_run.sh runs three times a day,
     only the first run spends calls.
  3. Issue (ISIN) call only for bonds whose issuer has no live national-scale
     rating after step 2: where the issuer rating resolves the bond, the
     issue call is skipped.
  4. max_calls caps one run; what doesn't fit is picked up next run, oldest
     first -- the slower multi-day rotation if MOEX ever needs one.

Health (pipeline_health, source 'moex'):
  MOEX_RATINGS_BLOCKED  error  HTTP 403/429 -- run stops at once, no retry
  MOEX_RATINGS_DENIED   error  non-JSON/HTML/"подписчик" bodies or errors
                               for > FAILURE_RATIO of calls (run stops
                               after MAX_CONSECUTIVE_FAILURES in a row)
  MOEX_RATINGS_EMPTY    error  > EMPTIED_RATIO of previously populated
                               targets came back empty: treated as a silent
                               denial, emptiness is NOT applied
  MOEX_RATINGS_EMPTIED  warn   a populated target came back empty (kept as
                               last known until EMPTY_CONFIRM_DAYS)
  MOEX_RATINGS_DEFAULT  warn   a grade in the default family appeared
Known ratings are never dropped because of a failed run; the resolver marks
them stale once last_seen is RATINGS_STALE_DAYS old (ratings.py).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import universe_pipeline
from .health import log_health
from .ingest import moex_ratings, sources
from .store import agency_ratings, db

UNIVERSE_LOOKBACK_DAYS = 7
DEFAULT_TTL_HOURS = 20
DEFAULT_MAX_CALLS = 1500
MAX_CONSECUTIVE_FAILURES = 5
FAILURE_RATIO = 0.2
EMPTIED_RATIO = 0.2
EMPTIED_MIN_COUNT = 10
EMPTY_CONFIRM_DAYS = 3


def target_bonds(con) -> dict[str, dict]:
    """secid -> emitent map row for every bond the pipeline cares about."""
    emitent_map = db.get_emitent_map(con)
    latest = db.latest_universe_snap_date(con)
    secids: set[str] = set()
    if latest:
        since = (date.fromisoformat(latest) - timedelta(days=UNIVERSE_LOOKBACK_DAYS)).isoformat()
        secids = {r["secid"] for r in con.execute(
            "SELECT DISTINCT secid FROM universe_snapshot WHERE snap_date >= ?", (since,))}
    return {s: emitent_map[s] for s in secids
            if s in emitent_map and emitent_map[s].get("emitent_id") is not None}


def target_issuers(con, bonds: dict[str, dict]) -> dict[int, str | None]:
    """emitent_id -> INN: issuers of the target bonds plus tier 1/2 issuers."""
    out: dict[int, str | None] = {}
    for row in bonds.values():
        out.setdefault(int(row["emitent_id"]), row.get("emitent_inn"))
    tier_inns = {r["issuer_id"] for r in con.execute(
        "SELECT issuer_id FROM issuer WHERE watchlist_tier IN (1, 2)")}
    if tier_inns:
        for r in con.execute("SELECT DISTINCT emitent_id, emitent_inn FROM emitent_map "
                             "WHERE emitent_id IS NOT NULL"):
            if r["emitent_inn"] in tier_inns:
                out.setdefault(int(r["emitent_id"]), r["emitent_inn"])
    return out


def _due(state: dict, key, now: datetime, ttl_hours: float) -> bool:
    st = state.get(key)
    if not st or not st.get("ok_at") or st.get("outcome") not in ("ok", "empty"):
        return True
    return datetime.fromisoformat(st["ok_at"]) <= now - timedelta(hours=ttl_hours)


def _order(keys, state) -> list:
    # never fetched first, then the oldest successful fetch
    return sorted(keys, key=lambda k: ((state.get(k) or {}).get("ok_at") or "", str(k)))


def run_refresh(con=None, *, client: moex_ratings.MoexRatingsClient | None = None,
                today: date | None = None, now: datetime | None = None,
                ttl_hours: float = DEFAULT_TTL_HOURS, max_calls: int = DEFAULT_MAX_CALLS,
                snapshot: bool = True, refresh_emitent_map: bool = True) -> dict:
    own_con = con is None
    con = con or db.connect()
    today = today or date.today()
    now = now or datetime.now()
    client = client or moex_ratings.MoexRatingsClient()
    db.init_db(con)
    db.init_universe(con)
    db.init_emitent_map(con)
    agency_ratings.init(con)

    if refresh_emitent_map:
        universe_pipeline.load_emitent_map(con, today, force_refresh=False)
    bonds = target_bonds(con)
    issuers = target_issuers(con, bonds)
    state = agency_ratings.fetch_state(con)

    result = {"issuers_targeted": len(issuers), "bonds_targeted": len(bonds),
              "issuer_calls": 0, "issue_calls": 0, "skipped_fresh": 0, "deferred": 0,
              "outcomes": {}, "changed": [], "emptied": [], "defaults": [],
              "blocked": None, "aborted": None, "calls": 0}
    raw: dict[str, object] = {}
    pending: list[tuple] = []   # (emitent_id, isin, inn, FetchResult)
    failures = consecutive = 0

    def fetch(url):
        nonlocal failures, consecutive
        res = client.fetch(url)
        result["outcomes"][res.outcome] = result["outcomes"].get(res.outcome, 0) + 1
        raw[url] = {"outcome": res.outcome, "detail": None if res.outcome in ("ok", "empty")
                    else res.detail, "payload": res.payload}
        if res.outcome in ("denied", "error"):
            failures += 1
            consecutive += 1
        else:
            consecutive = 0
        return res

    def run_targets(keys, url_for, inn_for, counter):
        for key in keys:
            if client.calls >= max_calls:
                result["deferred"] += 1
                continue
            res = fetch(url_for(key))
            result[counter] += 1
            pending.append((key[0], key[1], inn_for(key), res))
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                result["aborted"] = f"{consecutive} consecutive denied/error responses"
                return False
        return True

    try:
        issuer_keys = [(eid, "") for eid in issuers]
        due = [k for k in issuer_keys if _due(state, k, now, ttl_hours)]
        result["skipped_fresh"] += len(issuer_keys) - len(due)
        ok = run_targets(_order(due, state), lambda k: moex_ratings.issuer_url(k[0]),
                         lambda k: issuers[k[0]], "issuer_calls")
        _apply(con, pending, state, today, now, result)
        pending.clear()

        if ok:
            issue_keys = []
            for secid, row in bonds.items():
                eid = int(row["emitent_id"])
                if not (state.get((eid, "")) or {}).get("ok_at"):
                    continue  # issuer not fetched yet (deferred/failed): its turn first
                if agency_ratings.has_live_national(agency_ratings.current_rows(con, eid, "")):
                    continue
                issue_keys.append((eid, row.get("isin") or secid))
            due = [k for k in issue_keys if _due(state, k, now, ttl_hours)]
            result["skipped_fresh"] += len(issue_keys) - len(due)
            run_targets(_order(due, state), lambda k: moex_ratings.issue_url(k[0], k[1]),
                        lambda k: issuers.get(k[0]), "issue_calls")
            _apply(con, pending, state, today, now, result)
            pending.clear()
    except moex_ratings.RatingsBlocked as e:
        _apply(con, pending, state, today, now, result)
        result["blocked"] = str(e)
        log_health(con, "moex", "ingest", "error", "MOEX_RATINGS_BLOCKED",
                   f"{e} after {client.calls} calls -- run stopped, last known ratings kept")

    result["calls"] = client.calls
    if result["aborted"] or (client.calls and failures / client.calls > FAILURE_RATIO):
        log_health(con, "moex", "ingest", "error", "MOEX_RATINGS_DENIED",
                   f"{failures}/{client.calls} rating calls denied or failed"
                   + (f"; {result['aborted']}" if result["aborted"] else ""))
    if result.get("empty_suspect"):
        log_health(con, "moex", "ingest", "error", "MOEX_RATINGS_EMPTY",
                   f"{len(result['emptied'])} previously rated targets came back empty in one "
                   "run -- treated as a silent denial, known ratings kept")
    elif result["emptied"]:
        log_health(con, "moex", "ingest", "warn", "MOEX_RATINGS_EMPTIED",
                   f"{len(result['emptied'])} targets lost all MOEX ratings: "
                   + ", ".join(result["emptied"][:10]))
    if result["defaults"]:
        log_health(con, "moex", "ingest", "warn", "MOEX_RATINGS_DEFAULT",
                   "default-family grade appeared: " + "; ".join(result["defaults"][:10]))
    if snapshot and raw:
        sources._snapshot("moex_cci_ratings.json", json.dumps(raw, ensure_ascii=False), today)
    con.commit()
    if own_con:
        con.close()
    return result


def _apply(con, pending, state, today, now, result) -> None:
    fetched_at = now.isoformat(timespec="seconds")
    emptied_now = [p for p in pending if p[3].outcome == "empty"
                   and agency_ratings.current_rows(con, p[0], p[1])]
    suspect = len(emptied_now) >= EMPTIED_MIN_COUNT and len(emptied_now) > EMPTIED_RATIO * max(
        1, sum(1 for p in pending if p[3].outcome in ("ok", "empty")))
    if suspect:
        result["empty_suspect"] = True
    for eid, isin, inn, res in pending:
        outcome = res.outcome
        if outcome == "empty" and suspect:
            outcome = "denied"
        change = agency_ratings.apply_fetch(
            con, emitent_id=eid, isin=isin, inn=inn, rows=res.rows, today=today,
            fetched_at=fetched_at, outcome=outcome, empty_confirm_days=EMPTY_CONFIRM_DAYS)
        label = f"{inn or eid}{'/' + isin if isin else ''}"
        if change["emptied"]:
            result["emptied"].append(label)
        for agency, book, old, new in change["changed"]:
            result["changed"].append(f"{label} {agency}[{book}]: {old} -> {new}")
        for r in res.rows:
            if r.status == "default" and r.scale == "national":
                prev = [x for x in change["changed"] if x[0] == r.agency and x[1] == r.book_id]
                if prev:
                    result["defaults"].append(f"{label} {r.agency} {r.raw_level} {r.rated_on}")
        state[(eid, isin)] = {"ok_at": fetched_at if outcome in ("ok", "empty") else
                              (state.get((eid, isin)) or {}).get("ok_at"), "outcome": outcome}
    con.commit()
