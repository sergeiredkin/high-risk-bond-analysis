"""Automated tier=1 watchlist health monitoring (`hybond scan-watchlist-health`,
Watchlist Health Monitoring task, 2026-08-24; thresholds revised 2026-08-24
to be ВДО-appropriate and to replace the binary needs_review flag with a
4-level green/yellow/orange/red color scale).

Runs the same kind of signal checks promotion_audit.py already does for a
single promotion candidate, across every tier=1 issuer, and classifies each
into either hard-demotion (auto tier=1 -> tier=2, no confirmation -- reserved
for confirmed facts only: a critical insolvency/default filing, delisting, or
YTM pricing so extreme it implies the market already treats the bond as
defaulted) or a health_status color (green/yellow/orange/red) that does NOT
change tier, just flags severity for the dashboard.

Reuses promotion_audit.py directly for the two checks it already implements
identically (representative-secid selection, critical insolvency/default
events) -- see hard_critical_event()/_representative_secid below.

Two of the RED signals in the original spec -- "critical AZIPI event" and
"ФНС account block" -- have no dedicated category in the event taxonomy
(skills/hy-event-classify/references/taxonomy.md: reporting_delay,
coupon_event, offer_put_call, covenant, rating_action, governance,
insolvency, operations, capital_action, routine; severities noise/low/
medium/high/critical). "Critical AZIPI event" is implemented as "any
critical-severity event, regardless of category" (AZIPI is the disclosure
*source*, not a category -- nearly all event rows originate from it).
"ФНС account block" has no structured signal anywhere in the schema, so
fns_account_block_event() below is a best-effort keyword match over
event.summary text (see its docstring) -- flag this to a human as a known
limitation, not a verified detector, if it ever fires in practice.
"""
from __future__ import annotations

import json
from datetime import date

from . import promotion_audit
from .compute.default_probs import rating_rank
from .store import db

# --- Auto-demotion (hard, confirmed facts only) ---------------------------
YTM_DEMOTE_PCT = 300.0  # bond priced as if already in default

# --- RED: urgent review ----------------------------------------------------
RED_YTM_PCT = 150.0
RED_GSPREAD_WINDOW_DAYS = 7
RED_GSPREAD_REL_PCT = 100.0
RED_GSPREAD_ABS_BP = 2000.0
RED_CRITICAL_EVENT_LOOKBACK_DAYS = 3
RED_FNS_LOOKBACK_DAYS = 7

# --- ORANGE: watch this week -----------------------------------------------
ORANGE_YTM_LOW_PCT = 60.0
ORANGE_YTM_HIGH_PCT = 150.0
ORANGE_GSPREAD_WINDOW_DAYS = 30
ORANGE_GSPREAD_REL_PCT = 50.0
ORANGE_RATING_DROP_NOTCHES = 2
ORANGE_HIGH_EVENT_LOOKBACK_DAYS = 14

# --- YELLOW: monitor ---------------------------------------------------------
YELLOW_YTM_LOW_PCT = 30.0
YELLOW_YTM_HIGH_PCT = 60.0
YELLOW_GSPREAD_WINDOW_DAYS = 30
YELLOW_GSPREAD_REL_PCT = 30.0
YELLOW_MEDIUM_EVENT_LOOKBACK_DAYS = 7

RATING_LOOKBACK_DAYS = 90

# Best-effort keyword match for a tax-authority (ФНС) account block -- there
# is no dedicated event category for this, so this is a text heuristic, not
# a verified detector. Requires "фнс" AND one of the block/freeze words in
# the same summary to cut down on false positives from unrelated ФНС
# mentions (e.g. routine tax filings).
_FNS_BLOCK_WORDS = ("приостанов", "заблокир", "арест")

_PILLAR_KEYS = promotion_audit._PILLAR_KEYS


def hard_critical_event(con, issuer_id: str, today: str | None = None) -> list[dict]:
    """Auto-demote #1: category IN (insolvency, default) AND severity=critical,
    occurred in the last 1 day. Same category set as promotion_audit's
    BLOCKING_EVENT_CATEGORIES, deliberately imported rather than
    hand-copied so the two can't silently drift apart. This already covers
    a confirmed bankruptcy-petition filing -- the taxonomy has no separate
    category for "petition" vs. other insolvency proceedings."""
    today = today or date.today().isoformat()
    cats = ",".join(f"'{c}'" for c in sorted(promotion_audit.BLOCKING_EVENT_CATEGORIES))
    return [dict(r) for r in con.execute(
        f"SELECT event_id, occurred, category, severity, summary FROM event "
        f"WHERE issuer_id = ? AND occurred >= date(?, '-1 day') "
        f"AND severity = 'critical' AND category IN ({cats}) "
        f"ORDER BY occurred DESC", [issuer_id, today])]


def current_ytm_pct(con, secid: str | None) -> float | None:
    """Current YTM from the latest universe_snapshot row."""
    if not secid:
        return None
    row = con.execute(
        "SELECT ytm FROM universe_snapshot WHERE secid = ? "
        "ORDER BY snap_date DESC LIMIT 1", [secid]).fetchone()
    if not row or row["ytm"] is None:
        return None
    return round(row["ytm"] * 100, 2)


def is_delisted(con, issuer_id: str) -> bool:
    """Auto-demote #2: no currently-listed bonds left. Reuses
    issuer_bond.delisted (pipeline.py's own refresh_delisted_flags(), the
    same source of truth list_bonds_for_issuer() already reads for tier=1)
    rather than a fresh universe_snapshot-absence heuristic. Returns False
    (not "delisted") when the issuer has no known bonds at all -- that's a
    data gap, not evidence of delisting."""
    ever_known = con.execute(
        "SELECT COUNT(*) c FROM issuer_bond WHERE issuer_id = ?", [issuer_id]).fetchone()["c"]
    if ever_known == 0:
        return False
    return len(db.list_bonds_for_issuer(con, issuer_id)) == 0


def gspread_window_change(con, secid: str | None, window_days: int, today: str | None = None) -> dict | None:
    """G-spread % change over a window: (latest - earliest) / earliest * 100
    using the first and last spread_snapshot rows inside the window. Shared
    by the RED (7d), ORANGE (30d) and YELLOW (30d) checks -- same query
    shape, different window, so this stays one implementation. Returns None
    when there's fewer than 2 rows in the window or the earliest value is
    non-positive (can't compute a meaningful % change)."""
    today = today or date.today().isoformat()
    if not secid:
        return None
    rows = [dict(r) for r in con.execute(
        "SELECT snap_date, g_spread_bp FROM spread_snapshot "
        "WHERE secid = ? AND snap_date >= date(?, ?) AND g_spread_bp IS NOT NULL "
        "ORDER BY snap_date ASC",
        [secid, today, f"-{int(window_days)} days"])]
    if len(rows) < 2:
        return None
    start, current = rows[0]["g_spread_bp"], rows[-1]["g_spread_bp"]
    if start is None or start <= 0:
        return None
    return {"start_bp": start, "current_bp": current, "pct_change": (current - start) / start * 100}


def severity_event_count(con, issuer_id: str, severity: str, lookback_days: int,
                          today: str | None = None) -> int:
    """Count of event rows at a given severity within the last N days --
    shared by the RED (critical, 3d), ORANGE (high, 14d) and YELLOW
    (medium, 7d) event checks."""
    today = today or date.today().isoformat()
    return con.execute(
        "SELECT COUNT(*) c FROM event WHERE issuer_id = ? "
        "AND occurred >= date(?, ?) AND severity = ?",
        [issuer_id, today, f"-{int(lookback_days)} days", severity]).fetchone()["c"]


def fns_account_block_event(con, issuer_id: str, lookback_days: int, today: str | None = None) -> list[dict]:
    """Best-effort keyword match for a ФНС (tax authority) account block --
    see module docstring. Requires "фнс" AND a block/freeze word in the same
    event.summary; not a verified structured signal."""
    today = today or date.today().isoformat()
    rows = [dict(r) for r in con.execute(
        "SELECT event_id, occurred, category, severity, summary FROM event "
        "WHERE issuer_id = ? AND occurred >= date(?, ?) ORDER BY occurred DESC",
        [issuer_id, today, f"-{int(lookback_days)} days"])]
    hits = []
    for r in rows:
        text = (r["summary"] or "").lower()
        if "фнс" in text and any(w in text for w in _FNS_BLOCK_WORDS):
            hits.append(r)
    return hits


def rating_drop_notches(con, issuer_id: str, today: str | None = None) -> int | None:
    """rank(current rating) - rank(rating as of 90 days ago), positive =
    worse. None when there's no rating row at/before the 90-day cutoff to
    establish a baseline from."""
    today = today or date.today().isoformat()
    current = con.execute(
        "SELECT rating FROM rating WHERE issuer_id = ? ORDER BY rated_on DESC LIMIT 1",
        [issuer_id]).fetchone()
    baseline = con.execute(
        "SELECT rating FROM rating WHERE issuer_id = ? AND rated_on <= date(?, ?) "
        "ORDER BY rated_on DESC LIMIT 1",
        [issuer_id, today, f"-{RATING_LOOKBACK_DAYS} days"]).fetchone()
    if not current or not baseline:
        return None
    cur_rank, base_rank = rating_rank(current["rating"]), rating_rank(baseline["rating"])
    if cur_rank is None or base_rank is None:
        return None
    return cur_rank - base_rank


def rating_outlook_turned_negative(con, issuer_id: str) -> bool:
    """The 2 most recent rating rows: previous outlook wasn't 'negative',
    current outlook is -- a genuine change, not just "currently negative"
    (which could have been true for months)."""
    rows = con.execute(
        "SELECT outlook FROM rating WHERE issuer_id = ? ORDER BY rated_on DESC LIMIT 2",
        [issuer_id]).fetchall()
    if len(rows) < 2:
        return False
    current = (rows[0]["outlook"] or "").strip().lower()
    previous = (rows[1]["outlook"] or "").strip().lower()
    return current == "negative" and previous != "negative"


def scan_issuer(con, issuer_id: str, today: str | None = None) -> dict:
    """Runs the auto-demote checks and, if none fire, the RED/ORANGE/YELLOW
    checks for one issuer. Returns hard_reasons (non-empty = auto-demote)
    plus red/orange/yellow_reasons and the resolved health_status/
    health_reason (worst tier that has any reason; 'green' if none)."""
    today = today or date.today().isoformat()
    secid = promotion_audit._representative_secid(con, issuer_id)
    ytm_pct = current_ytm_pct(con, secid)

    hard_reasons: list[str] = []

    crit_events = hard_critical_event(con, issuer_id, today)
    if crit_events:
        cats = sorted({e["category"] for e in crit_events})
        hard_reasons.append(
            f"critical event(s) category={cats} occurred in last 1 day (event_id="
            f"{[e['event_id'] for e in crit_events]})")

    if ytm_pct is not None and ytm_pct > YTM_DEMOTE_PCT:
        hard_reasons.append(f"YTM {ytm_pct:.1f}% > {YTM_DEMOTE_PCT:.0f}% (effective default pricing)")

    if is_delisted(con, issuer_id):
        hard_reasons.append("no bonds found in current universe_snapshot snap_date (delisted)")

    red_reasons: list[str] = []
    orange_reasons: list[str] = []
    yellow_reasons: list[str] = []

    if not hard_reasons:
        # RED
        if ytm_pct is not None and ytm_pct > RED_YTM_PCT:
            red_reasons.append(f"YTM {ytm_pct:.1f}% > {RED_YTM_PCT:.0f}%")

        red_window = gspread_window_change(con, secid, RED_GSPREAD_WINDOW_DAYS, today)
        if (red_window and red_window["pct_change"] > RED_GSPREAD_REL_PCT
                and red_window["current_bp"] > RED_GSPREAD_ABS_BP):
            red_reasons.append(
                f"G-spread +{red_window['pct_change']:.0f}% over {RED_GSPREAD_WINDOW_DAYS}d to "
                f"{red_window['current_bp']:.0f}bp (> {RED_GSPREAD_REL_PCT:.0f}% and "
                f"> {RED_GSPREAD_ABS_BP:.0f}bp)")

        crit_recent = severity_event_count(con, issuer_id, "critical", RED_CRITICAL_EVENT_LOOKBACK_DAYS, today)
        if crit_recent:
            red_reasons.append(
                f"{crit_recent} critical-severity event(s) in last {RED_CRITICAL_EVENT_LOOKBACK_DAYS} days")

        fns_hits = fns_account_block_event(con, issuer_id, RED_FNS_LOOKBACK_DAYS, today)
        if fns_hits:
            red_reasons.append(
                f"possible ФНС account block, keyword match ({len(fns_hits)} event(s) in last "
                f"{RED_FNS_LOOKBACK_DAYS} days, event_id={[e['event_id'] for e in fns_hits]})")

        # ORANGE
        if ytm_pct is not None and ORANGE_YTM_LOW_PCT <= ytm_pct <= ORANGE_YTM_HIGH_PCT:
            orange_reasons.append(f"YTM {ytm_pct:.1f}% in [{ORANGE_YTM_LOW_PCT:.0f}%, {ORANGE_YTM_HIGH_PCT:.0f}%]")

        orange_window = gspread_window_change(con, secid, ORANGE_GSPREAD_WINDOW_DAYS, today)
        if orange_window and orange_window["pct_change"] > ORANGE_GSPREAD_REL_PCT:
            orange_reasons.append(
                f"G-spread +{orange_window['pct_change']:.0f}% over {ORANGE_GSPREAD_WINDOW_DAYS}d "
                f"(> {ORANGE_GSPREAD_REL_PCT:.0f}%)")

        notches = rating_drop_notches(con, issuer_id, today)
        if notches is not None and notches >= ORANGE_RATING_DROP_NOTCHES:
            orange_reasons.append(f"rating dropped {notches} notch(es) in last {RATING_LOOKBACK_DAYS} days")

        high_recent = severity_event_count(con, issuer_id, "high", ORANGE_HIGH_EVENT_LOOKBACK_DAYS, today)
        if high_recent:
            orange_reasons.append(
                f"{high_recent} high-severity event(s) in last {ORANGE_HIGH_EVENT_LOOKBACK_DAYS} days")

        # YELLOW
        if ytm_pct is not None and YELLOW_YTM_LOW_PCT <= ytm_pct < YELLOW_YTM_HIGH_PCT:
            yellow_reasons.append(f"YTM {ytm_pct:.1f}% in [{YELLOW_YTM_LOW_PCT:.0f}%, {YELLOW_YTM_HIGH_PCT:.0f}%)")

        yellow_window = gspread_window_change(con, secid, YELLOW_GSPREAD_WINDOW_DAYS, today)
        if yellow_window and yellow_window["pct_change"] > YELLOW_GSPREAD_REL_PCT:
            yellow_reasons.append(
                f"G-spread +{yellow_window['pct_change']:.0f}% over {YELLOW_GSPREAD_WINDOW_DAYS}d "
                f"(> {YELLOW_GSPREAD_REL_PCT:.0f}%)")

        if rating_outlook_turned_negative(con, issuer_id):
            yellow_reasons.append("rating outlook changed to negative")

        medium_recent = severity_event_count(con, issuer_id, "medium", YELLOW_MEDIUM_EVENT_LOOKBACK_DAYS, today)
        if medium_recent:
            yellow_reasons.append(
                f"{medium_recent} medium-severity event(s) in last {YELLOW_MEDIUM_EVENT_LOOKBACK_DAYS} days")

    if hard_reasons:
        health_status, color_reasons = "red", hard_reasons
    elif red_reasons:
        health_status, color_reasons = "red", red_reasons
    elif orange_reasons:
        health_status, color_reasons = "orange", orange_reasons
    elif yellow_reasons:
        health_status, color_reasons = "yellow", yellow_reasons
    else:
        health_status, color_reasons = "green", []

    return {
        "issuer_id": issuer_id, "secid": secid,
        "hard_reasons": hard_reasons,
        "red_reasons": red_reasons, "orange_reasons": orange_reasons, "yellow_reasons": yellow_reasons,
        "health_status": health_status,
        "health_reason": "; ".join(color_reasons) if color_reasons else None,
    }


def scan_watchlist(con, today: str | None = None) -> dict:
    """Scans every tier=1 issuer. Does NOT apply any DB writes itself --
    the CLI (cmd_scan_watchlist_health) owns the action layer (tier change,
    market_note, health_status) so this stays a read-only, trivially
    testable function."""
    today = today or date.today().isoformat()
    tier1 = db.list_issuers_by_tier(con, 1)
    results = [dict(scan_issuer(con, row["issuer_id"], today), name=row["name"])
               for row in tier1]
    return {"as_of": today, "issuers": results}
