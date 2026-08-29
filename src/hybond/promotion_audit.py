"""Pre-promotion audit for `hybond watchlist-promote` (tier=2 -> tier=1).

Pulls together everything already in the DB about one issuer -- G-spread
trend, current YTM/rating, critical events, detector flags, market notes,
pillar-score freshness -- and classifies the result as BLOCKED / WARNINGS /
CLEAN, so a human (or an explicit --force) makes the final promotion call
with full context instead of promoting on a bare `--reason` string alone.

Every finder function takes `con` + `issuer_id` (+ an optional `today`
override for deterministic testing) and returns plain dicts -- no CLI/print
concerns here, those live in __main__.py's cmd_watchlist_promote so this
module stays trivially unit-testable against a real tmp-file SQLite DB, same
pattern as portfolio.py/promote.py.
"""
from __future__ import annotations

import json
from datetime import date

YTM_BLOCK_PCT = 100.0
YTM_WARN_PCT = 30.0
GSPREAD_DELTA_BLOCK_BP = 500.0
GSPREAD_DELTA_WARN_BP = 200.0
GSPREAD_LEVEL_WARN_BP = 1000.0
PILLARS_STALE_DAYS = 90
BLOCKING_EVENT_CATEGORIES = {"insolvency", "default"}

_PILLAR_KEYS = ("score_leverage", "score_liquidity", "score_refinancing", "score_operations")


def _representative_secid(con, issuer_id: str) -> str | None:
    """Same pick as export_dashboard_data.spread_history_by_issuer(): the
    issuer's bond with the highest average traded volume (price_snapshot has
    volume, spread_snapshot doesn't) -- keeps a multi-bond issuer's trend
    reading as one coherent series instead of an arbitrary/first bond."""
    row = con.execute(
        "SELECT b.secid, AVG(ps.volume) avg_vol FROM bond b "
        "JOIN price_snapshot ps ON ps.secid = b.secid "
        "WHERE b.issuer_id = ? GROUP BY b.secid ORDER BY avg_vol DESC LIMIT 1",
        [issuer_id],
    ).fetchone()
    return row["secid"] if row else None


def gspread_trend(con, issuer_id: str, today: str | None = None) -> dict:
    """1. G-spread trend, last 30 days -- per-period delta vs the previous
    snapshot, computed over the FULL 30-day window (blocking/warning checks
    need every period, not just the last 5 shown), with only the last 5
    rows kept for display."""
    today = today or date.today().isoformat()
    secid = _representative_secid(con, issuer_id)
    if not secid:
        return {"secid": None, "rows": [], "max_abs_delta_bp": None, "current_g_spread_bp": None}
    rows = [dict(r) for r in con.execute(
        "SELECT snap_date, g_spread_bp FROM spread_snapshot "
        "WHERE secid = ? AND snap_date >= date(?, '-30 days') "
        "AND g_spread_bp IS NOT NULL ORDER BY snap_date ASC",
        [secid, today])]
    for i, r in enumerate(rows):
        r["delta_bp"] = (r["g_spread_bp"] - rows[i - 1]["g_spread_bp"]) if i > 0 else None
    max_abs_delta = max((abs(r["delta_bp"]) for r in rows if r["delta_bp"] is not None), default=None)
    return {
        "secid": secid,
        "rows": rows[-5:],
        "max_abs_delta_bp": max_abs_delta,
        "current_g_spread_bp": rows[-1]["g_spread_bp"] if rows else None,
    }


def current_universe_data(con, secid: str | None) -> dict | None:
    """2. Latest universe_snapshot row for the same representative secid
    the G-spread trend uses -- current YTM/G-spread/rating in one place."""
    if not secid:
        return None
    row = con.execute(
        "SELECT secid, snap_date, ytm, g_spread_bp, rating FROM universe_snapshot "
        "WHERE secid = ? ORDER BY snap_date DESC LIMIT 1", [secid]).fetchone()
    if not row:
        return None
    d = dict(row)
    d["ytm_pct"] = round(d["ytm"] * 100, 2) if d["ytm"] is not None else None
    return d


def critical_events(con, issuer_id: str, today: str | None = None) -> dict:
    """3. Critical/high events, last 90 days. `blocking` is the hard-stop
    subset: severity=critical AND category in {insolvency, default}."""
    today = today or date.today().isoformat()
    rows = [dict(r) for r in con.execute(
        "SELECT event_id, occurred, category, severity, summary FROM event "
        "WHERE issuer_id = ? AND occurred >= date(?, '-90 days') "
        "AND severity IN ('critical', 'high') ORDER BY occurred DESC",
        [issuer_id, today])]
    blocking = [r for r in rows
                if r["severity"] == "critical" and r["category"] in BLOCKING_EVENT_CATEGORIES]
    return {"rows": rows, "blocking": blocking}


def detector_flags(con, issuer_id: str, today: str | None = None) -> list[dict]:
    """4. Detector flags (flag_log), last 30 days -- informational only,
    not a blocking/warning trigger per this task's own spec (only items 1-3
    define pass/fail conditions)."""
    today = today or date.today().isoformat()
    return [dict(r) for r in con.execute(
        "SELECT snap_date, detector, signal, metric, detail FROM flag_log "
        "WHERE issuer_id = ? AND snap_date >= date(?, '-30 days') "
        "ORDER BY snap_date DESC", [issuer_id, today])]


def critical_market_notes(con, issuer_id: str, today: str | None = None) -> list[dict]:
    """5. market_note rows, last 90 days, severity IN (critical, high) --
    informational display, same as detector_flags above."""
    today = today or date.today().isoformat()
    return [dict(r) for r in con.execute(
        "SELECT occurred, headline, severity, category FROM market_note "
        "WHERE issuer_id = ? AND occurred >= date(?, '-90 days') "
        "AND severity IN ('critical', 'high') ORDER BY occurred DESC",
        [issuer_id, today])]


def pillar_summary(con, issuer_id: str, today: str | None = None) -> dict:
    """6. Latest pillars_snapshot row with its computed_on date, so the
    report shows freshness, not just the scores themselves."""
    today = today or date.today().isoformat()
    row = con.execute(
        "SELECT period, computed_on, payload FROM pillars_snapshot "
        "WHERE issuer_id = ? ORDER BY computed_on DESC LIMIT 1", [issuer_id]).fetchone()
    if not row:
        return {"found": False, "computed_on": None, "period": None,
                "days_old": None, "stale": None, "scores": {}}
    payload = json.loads(row["payload"]) if row["payload"] else {}
    days_old = (date.fromisoformat(today) - date.fromisoformat(row["computed_on"])).days
    return {
        "found": True, "computed_on": row["computed_on"], "period": row["period"],
        "days_old": days_old, "stale": days_old > PILLARS_STALE_DAYS,
        "scores": {k: payload.get(k) for k in _PILLAR_KEYS},
    }


def moex_confirmed_default(con, issuer_id: str) -> list[dict]:
    """moex_default rows for this issuer with status='Дефолт' (confirmed --
    not 'Технический дефолт', a missed-then-cured or still-open short
    delay, which is a warning-grade signal at most, not an automatic
    block). Empty list (never raises) if the moex_default table doesn't
    exist yet -- this task's import is optional infrastructure, not a
    dependency every audit caller is guaranteed to have run."""
    from . import moex_defaults
    from .store import db
    db.init_moex_default(con)
    rows = moex_defaults.get_defaults_for_issuer(con, issuer_id)
    return [r for r in rows if r["status"] == "Дефолт"]


def run_audit(con, issuer_id: str, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    trend = gspread_trend(con, issuer_id, today)
    universe = current_universe_data(con, trend["secid"])
    events = critical_events(con, issuer_id, today)
    flags = detector_flags(con, issuer_id, today)
    notes = critical_market_notes(con, issuer_id, today)
    pillars = pillar_summary(con, issuer_id, today)
    moex_defaults_confirmed = moex_confirmed_default(con, issuer_id)

    ytm_pct = universe["ytm_pct"] if universe else None
    max_delta = trend["max_abs_delta_bp"]
    current_gspread = trend["current_g_spread_bp"]

    blocking: list[str] = []
    warnings: list[str] = []

    if moex_defaults_confirmed:
        blocking.append("Подтверждённый дефолт в реестре MOEX")
    if events["blocking"]:
        cats = sorted({r["category"] for r in events["blocking"]})
        blocking.append(
            f"{len(events['blocking'])} critical event(s) in category {cats} in last 90 days")
    if ytm_pct is not None and ytm_pct > YTM_BLOCK_PCT:
        blocking.append(f"YTM {ytm_pct:.1f}% > {YTM_BLOCK_PCT:.0f}% (extreme distress)")
    if max_delta is not None and max_delta > GSPREAD_DELTA_BLOCK_BP:
        blocking.append(
            f"G-spread delta {max_delta:.0f}bp > {GSPREAD_DELTA_BLOCK_BP:.0f}bp "
            "in a single period (last 30 days)")

    if ytm_pct is not None and YTM_WARN_PCT < ytm_pct <= YTM_BLOCK_PCT:
        warnings.append(f"YTM {ytm_pct:.1f}% > {YTM_WARN_PCT:.0f}% (distress territory for ВДО)")
    if current_gspread is not None and current_gspread > GSPREAD_LEVEL_WARN_BP:
        warnings.append(f"current G-spread {current_gspread:.0f}bp > {GSPREAD_LEVEL_WARN_BP:.0f}bp")
    if max_delta is not None and GSPREAD_DELTA_WARN_BP < max_delta <= GSPREAD_DELTA_BLOCK_BP:
        warnings.append(
            f"G-spread delta {max_delta:.0f}bp > {GSPREAD_DELTA_WARN_BP:.0f}bp "
            "in a single period (last 30 days)")
    high_sev = [r for r in events["rows"] if r["severity"] == "high"]
    if high_sev:
        warnings.append(f"{len(high_sev)} high-severity event(s) in last 90 days")
    # A critical event NOT in the insolvency/default block set still deserves
    # a warning -- the task's own "Warning conditions" list only names
    # "high-severity", but silently saying nothing about e.g. a critical
    # governance event would defeat the point of a full audit.
    other_critical = [r for r in events["rows"]
                      if r["severity"] == "critical" and r not in events["blocking"]]
    if other_critical:
        warnings.append(
            f"{len(other_critical)} critical-severity event(s) outside "
            "insolvency/default in last 90 days")
    if pillars["found"] and pillars["stale"]:
        warnings.append(f"pillar scores are {pillars['days_old']} days old (> {PILLARS_STALE_DAYS})")
    if not pillars["found"]:
        warnings.append("no financial data found (ГИР БО не найден)")

    status = "BLOCKED" if blocking else ("WARNINGS" if warnings else "CLEAN")

    return {
        "issuer_id": issuer_id, "as_of": today,
        "gspread_trend": trend, "universe": universe, "events": events,
        "flags": flags, "market_notes": notes, "pillars": pillars,
        "moex_defaults_confirmed": moex_defaults_confirmed,
        "blocking": blocking, "warnings": warnings, "status": status,
    }


def format_report(audit: dict, issuer_name: str | None = None) -> str:
    """Plain-text terminal report, printed in full before any confirmation
    prompt -- matches the task's "print the full audit report to terminal
    before asking for confirmation" requirement."""
    lines: list[str] = []
    label = issuer_name or audit["issuer_id"]
    lines.append(f"=== Pre-promotion audit: {label} ({audit['issuer_id']}) as of {audit['as_of']} ===")

    lines.append("\n1. G-spread trend (last 30 days, spread_snapshot)")
    trend = audit["gspread_trend"]
    if not trend["rows"]:
        lines.append("   нет данных (нет представительной бумаги / нет истории spread_snapshot)")
    else:
        for r in trend["rows"]:
            delta = f"{r['delta_bp']:+.0f}bp" if r["delta_bp"] is not None else "—"
            lines.append(f"   {r['snap_date']}  g_spread={r['g_spread_bp']:.0f}bp  delta={delta}")
        if trend["max_abs_delta_bp"] is not None:
            lines.append(f"   max |delta| in window: {trend['max_abs_delta_bp']:.0f}bp")

    lines.append("\n2. Current YTM / universe data (universe_snapshot)")
    uni = audit["universe"]
    if not uni:
        lines.append("   нет данных universe_snapshot")
    else:
        g_spread = uni["g_spread_bp"]
        lines.append(f"   snap_date={uni['snap_date']}  YTM={uni['ytm_pct']}%  "
                     f"g_spread={g_spread:.0f}bp  rating={uni['rating'] or '—'}"
                     if g_spread is not None else
                     f"   snap_date={uni['snap_date']}  YTM={uni['ytm_pct']}%  "
                     f"g_spread=—  rating={uni['rating'] or '—'}")

    lines.append("\n3. Critical/high events (last 90 days, event)")
    events = audit["events"]
    if not events["rows"]:
        lines.append("   нет событий critical/high за последние 90 дней")
    else:
        for r in events["rows"]:
            flag = " [BLOCKING]" if r in events["blocking"] else ""
            lines.append(f"   {r['occurred']}  {r['severity']:<8} {r['category']:<14} "
                         f"{(r['summary'] or '')[:70]}{flag}")

    lines.append("\n4. Detector flags (last 30 days, flag_log)")
    if not audit["flags"]:
        lines.append("   нет флагов за последние 30 дней")
    else:
        for r in audit["flags"]:
            metric = f"{r['metric']:.1f}" if r["metric"] is not None else "—"
            lines.append(f"   {r['snap_date']}  {r['detector']:<8} {r['signal']:<14} "
                         f"metric={metric}  {r['detail'] or ''}")

    lines.append("\n5. Market notes (last 90 days, critical/high, market_note)")
    if not audit["market_notes"]:
        lines.append("   нет market_note critical/high за последние 90 дней")
    else:
        for r in audit["market_notes"]:
            lines.append(f"   {r['occurred']}  {r['severity']:<8} {(r['headline'] or '')[:80]}")

    lines.append("\n6. Pillar scores (pillars_snapshot)")
    p = audit["pillars"]
    if not p["found"]:
        lines.append("   ГИР БО не найден — финансовые данные отсутствуют")
    else:
        staleness = f"УСТАРЕЛИ ({p['days_old']}д)" if p["stale"] else f"свежие ({p['days_old']}д)"
        lines.append(f"   computed_on={p['computed_on']} period={p['period']}  {staleness}")
        lines.append(f"   {p['scores']}")

    lines.append("\n7. MOEX default registry (moex_default)")
    if not audit["moex_defaults_confirmed"]:
        lines.append("   нет подтверждённых записей 'Дефолт' в реестре MOEX")
    else:
        for r in audit["moex_defaults_confirmed"]:
            lines.append(f"   {r['planned_date']}  {r['obligation_type']:<10} "
                         f"{r['status']:<20} {r['isin']}")

    lines.append("")
    if audit["blocking"]:
        lines.append("BLOCKING CONDITIONS:")
        for b in audit["blocking"]:
            lines.append(f"  ✗ {b}")
    if audit["warnings"]:
        lines.append("WARNINGS:")
        for w in audit["warnings"]:
            lines.append(f"  ⚠ {w}")
    if not audit["blocking"] and not audit["warnings"]:
        lines.append("No blocking conditions or warnings.")

    return "\n".join(lines)
