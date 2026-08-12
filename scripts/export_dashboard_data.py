"""Export everything the dashboard needs into one JSON file:
data/dashboard_data.json.

Sources combined:
  - watchlist.json           issuer metadata (name, sector, risk_tier, rating)
  - data/factlists/*.json    latest per-issuer factlist (bonds, flags, events,
                              six_pillars, thesis_status) — Slice 2 output
  - data/db/hybond.sqlite    full thesis text (assumptions/invalidation),
                              flag history for the sparkline
  - data/report.json         day-1 spread screen + maturity wall (Slice 1)

Does not compute anything new — pure aggregation for the dashboard renderer
(scripts/build_dashboard.py) to consume.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from hybond.classify_utils import queue_bucket  # noqa: E402
from hybond.compute.default_probs import (  # noqa: E402
    DEFAULT_PROBS, RATING_ORDER, RECOVERY_RATE, implied_fair_spread_bp, lookup)
from hybond.compute import inflection  # noqa: E402
from hybond.store import db  # noqa: E402
from hybond import portfolio  # noqa: E402
from hybond import promote  # noqa: E402
from hybond.universe_pipeline import VDO_MIN_VOLUME  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FACTLIST_DIR = ROOT / "data" / "factlists"
OUT_PATH = ROOT / "data" / "dashboard_data.json"

# Rating tiers the market map draws fair-value overlay lines for (spec: AA/A/BBB/BB).
FAIR_VALUE_TIERS = ["ruAA", "ruA", "ruBBB", "ruBB"]


def latest_factlists() -> dict[str, dict]:
    """One factlist per issuer_id: the most recent snap_date on disk."""
    best: dict[str, tuple[str, dict]] = {}
    for p in FACTLIST_DIR.glob("*.json"):
        issuer_id, snap_date = p.stem.rsplit("_", 1)
        data = json.loads(p.read_text(encoding="utf-8"))
        if issuer_id not in best or snap_date > best[issuer_id][0]:
            best[issuer_id] = (snap_date, data)
    return {k: v[1] for k, v in best.items()}


def flag_history(con) -> dict[str, list[dict]]:
    rows = con.execute(
        "SELECT issuer_id, snap_date, detector, signal, metric, detail "
        "FROM flag_log ORDER BY snap_date"
    ).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["issuer_id"]].append({
            "snap_date": r["snap_date"], "detector": r["detector"],
            "signal": r["signal"], "metric": r["metric"], "detail": r["detail"],
        })
    return out


def filing_counts_by_issuer(con) -> dict[str, dict]:
    rows = con.execute(
        "SELECT issuer_id, COUNT(*) total, SUM(classified) classified "
        "FROM filing GROUP BY issuer_id"
    ).fetchall()
    return {r["issuer_id"]: {"total": r["total"], "classified": r["classified"] or 0}
            for r in rows}


def queue_count(con) -> int:
    """Unclassified filings + market_notes, noise-pattern matches excluded --
    the home screen / Вотчлист tab's "N в очереди на классификацию"
    indicator. Same classify_utils.queue_bucket() the CLI's `hybond queue`/
    `hybond market-notes-queue` three-section display uses, so the
    dashboard's count can never disagree with what those commands show."""
    from hybond.classify_utils import queue_bucket
    filing_rows = con.execute("SELECT title FROM filing WHERE classified=0").fetchall()
    note_rows = con.execute("SELECT headline FROM market_note WHERE severity IS NULL").fetchall()
    n = sum(1 for r in filing_rows if queue_bucket(r["title"]) != "noise")
    n += sum(1 for r in note_rows if queue_bucket(r["headline"]) != "noise")
    return n


def spread_history_by_secid(con) -> dict[str, list[dict]]:
    rows = con.execute(
        "SELECT secid, snap_date, g_spread_bp, ytm FROM spread_snapshot "
        "ORDER BY snap_date"
    ).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["secid"]].append({
            "snap_date": r["snap_date"], "g_spread_bp": r["g_spread_bp"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
        })
    return out


def convexity_by_secid(con) -> dict[str, float | None]:
    """secid -> latest computed convexity from spread_snapshot -- Task B's
    verdict component (per-bond drawer, Layer 3/pro-only) wants it alongside
    G-spread/mod_dur, neither of which the factlist bond dict currently
    carries convexity for (assemble.py's Bond dataclass stops at mod_dur).
    NULL when yield_source='moex' (floaters -- see spread_snapshot's own
    column comment), same as every other convexity consumer in this
    codebase."""
    rows = con.execute(
        "SELECT secid, convexity FROM spread_snapshot s1 WHERE snap_date = "
        "(SELECT MAX(snap_date) FROM spread_snapshot s2 WHERE s2.secid = s1.secid)"
    ).fetchall()
    return {r["secid"]: r["convexity"] for r in rows}


def mispricing_delta_by_secid(con) -> dict[str, dict]:
    """Per-bond mispricing_bp trend: current vs. previous available
    universe_snapshot day for that secid. A plain two-point comparison (see
    compute.pillars.pillars_delta's docstring for why this isn't a
    statistical model) -- universe_snapshot already stores one row per day
    per secid, so this is a query over existing data, not new storage."""
    rows = con.execute(
        "SELECT secid, snap_date, mispricing_bp FROM universe_snapshot "
        "WHERE mispricing_bp IS NOT NULL ORDER BY secid, snap_date DESC"
    ).fetchall()
    by_secid: dict[str, list] = defaultdict(list)
    for r in rows:
        by_secid[r["secid"]].append((r["snap_date"], r["mispricing_bp"]))
    out: dict[str, dict] = {}
    for secid, vals in by_secid.items():
        if len(vals) < 2:
            out[secid] = {"delta_bp": None, "direction": None}
            continue
        cur, prev = vals[0][1], vals[1][1]
        d = cur - prev
        out[secid] = {
            "delta_bp": round(d, 0),
            "direction": "up" if d > 0 else "down" if d < 0 else "flat",
            "since_date": vals[1][0],
        }
    return out


def full_events_by_issuer(con) -> dict[str, list[dict]]:
    """ALL classified events/market_notes per issuer, not just the 30-day
    'since_last' window assemble.py builds for the notification panel
    (events_since_last/market_notes_since_last -- deliberately narrow for
    that use). Part D's per-bond detail panel needs the real history (the
    2026-07-31 classification pass alone covered filings back to January),
    so this is a second, separate query rather than widening the shared
    30-day one and risking changing what the notification panel shows."""
    events_by_issuer: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute(
            "SELECT issuer_id, occurred, category, severity, summary "
            "FROM event ORDER BY occurred DESC"):
        events_by_issuer[r["issuer_id"]].append({
            "occurred": r["occurred"], "category": r["category"],
            "severity": r["severity"], "summary": r["summary"], "source": "event",
        })
    try:
        for r in con.execute(
                "SELECT issuer_id, occurred, category, severity, headline, source, url "
                "FROM market_note WHERE severity IS NOT NULL ORDER BY occurred DESC"):
            events_by_issuer[r["issuer_id"]].append({
                "occurred": r["occurred"], "category": r["category"],
                "severity": r["severity"], "summary": r["headline"],
                "source": r["source"], "url": r["url"],
            })
    except Exception:  # noqa: BLE001 -- market_note may not exist yet
        pass
    for issuer_id, evs in events_by_issuer.items():
        evs.sort(key=lambda e: e["occurred"] or "", reverse=True)
    return events_by_issuer


def expected_returns_by_secid(con) -> dict[str, dict]:
    """Part B projections (compute/expected_return.py), keyed by secid, each
    holding whatever of {1y, 2y, maturity} was computed. Reads the MOST
    RECENT snap_date available per secid rather than requiring an exact
    match to today -- pipeline.py (hybond run) computes these, and if that
    hasn't run yet today (or a network hiccup skipped a bond), this degrades
    to the last good value instead of silently going blank. Empty for any
    secid pipeline.py hasn't reached yet (e.g. floaters, which have no fixed
    schedule to project -- see expected_return.py's own scoping note)."""
    try:
        rows = con.execute(
            "SELECT secid, snap_date, horizon_label, horizon_yr, irr_pct, "
            "undiscounted_npv, undiscounted_total_cashflow, "
            "horizon_capped_by_maturity, uses_extrapolated_pd "
            "FROM expected_return_snapshot ORDER BY secid, snap_date DESC"
        ).fetchall()
    except Exception:  # noqa: BLE001 -- table may not exist yet
        return {}
    out: dict[str, dict] = {}
    seen_date: dict[str, str] = {}
    for r in rows:
        secid = r["secid"]
        if secid in seen_date and seen_date[secid] != r["snap_date"]:
            continue  # already have this secid's most-recent date; skip older rows
        seen_date[secid] = r["snap_date"]
        out.setdefault(secid, {})[r["horizon_label"]] = {
            "horizon_yr": r["horizon_yr"], "irr_pct": r["irr_pct"],
            "undiscounted_npv": r["undiscounted_npv"],
            "undiscounted_total_cashflow": r["undiscounted_total_cashflow"],
            "horizon_capped_by_maturity": bool(r["horizon_capped_by_maturity"]),
            "uses_extrapolated_pd": bool(r["uses_extrapolated_pd"]),
        }
    return out


def data_conflicts_for_issuer(con, issuer_id: str, ratings_all: list[dict]) -> list[dict]:
    """Detects genuine disagreements between hybond's own DB rows for one
    issuer, for the narrative-card generator (skills/bond-narrative-card) to
    surface explicitly rather than silently resolving. Two kinds, matching
    the two the prompt's Data Sources section calls out:
      - cross-agency rating disagreement: more than one currently-live
        `rating` row (db.all_current_ratings) whose letter grade differs.
        Differing outlook/action alone is not a conflict (agencies routinely
        run on different review cadences); differing grade is.
      - pillars re-computation drift: the two most recent pillars_snapshot
        rows share a fiscal `period` but their payload disagrees on a
        headline metric -- a same-period recompute that changed the answer
        (e.g. a restated financials_snapshot feeding it), not just a new
        period arriving.
    Best-effort: returns [] rather than raising if a table/column is
    missing, since this is a display-only enrichment, not core pipeline
    output."""
    conflicts: list[dict] = []

    grades = {r["rating"] for r in ratings_all if r.get("rating")}
    if len(grades) > 1:
        conflicts.append({
            "type": "rating_disagreement",
            "detail": [
                {"agency": r["agency"], "rating": r["rating"],
                 "outlook": r.get("outlook"), "rated_on": r.get("rated_on")}
                for r in ratings_all
            ],
        })

    try:
        hist = db.pillars_history(con, issuer_id, limit=2)
    except Exception:  # noqa: BLE001 -- pillars_snapshot may not exist yet
        hist = []
    if len(hist) == 2 and hist[0]["period"] == hist[1]["period"]:
        try:
            cur = json.loads(hist[0]["payload"])
            prev = json.loads(hist[1]["payload"])
        except (ValueError, TypeError, KeyError):
            cur = prev = None
        if cur is not None and prev is not None and cur != prev:
            conflicts.append({
                "type": "pillars_recompute_drift",
                "period": hist[0]["period"],
                "detail": [
                    {"computed_on": hist[1]["computed_on"], "payload": prev},
                    {"computed_on": hist[0]["computed_on"], "payload": cur},
                ],
            })

    return conflicts


def _nearest_snap_date_on_or_before(con, cutoff: str) -> str | None:
    row = con.execute(
        "SELECT snap_date FROM universe_snapshot WHERE snap_date<=? "
        "ORDER BY snap_date DESC LIMIT 1", (cutoff,)).fetchone()
    return row["snap_date"] if row else None


def _coupon_freq_bucket(coupon_period: int | None) -> str | None:
    """MOEX ISS COUPONPERIOD is days between coupons -- bucket into the
    approximate payment frequency the filter panel offers. Thresholds are
    generous bands around the nominal 30/91/182/365-day periods to absorb
    real-world day-count variance (leap years, non-30-day months), not exact
    calendar-day cutoffs. None when the field itself is unknown (e.g. rows
    from a snapshot taken before this column existed) -- never guessed."""
    if coupon_period is None or coupon_period <= 0:
        return None  # 0/negative -- MOEX reports this for a handful of zero-coupon/discount
                      # instruments with no periodic coupon at all, not a monthly one
    if coupon_period <= 35:
        return "monthly"
    if coupon_period <= 100:
        return "quarterly"
    if coupon_period <= 200:
        return "semiannual"
    return "annual"


def _bonds_for_snap_date(con, snap_date: str, issuer_sectors: dict[str, str]) -> list[dict]:
    """One time-bucket of universe_snapshot, shaped for the screener AND the
    market-map tabs. Coupon type is a best-effort heuristic (COUPONPERCENT
    null -> floater), NOT a reliable classification -- confirmed false
    negatives exist (e.g. some known floaters report a current-period
    percent and read as 'fixed' here). Getting this right needs per-bond
    bondization, which universe_pipeline.py deliberately never calls.
    Surfaced in the UI as a caveat, not hidden.

    Market-map fields (pd_market/pd_rating/pd_gap/mod_dur): pd_market is
    the default probability implied by today's market price alone (same
    g_spread_bp -> PD inversion the mispricing signal already uses, just
    exposed as a probability instead of a spread); pd_rating is the
    agency-implied PD looked up from the same default_probs table
    mispricing_bp is built from, so pd_gap is arithmetically identical
    information to mispricing_bp -- just rescaled for the map's PD axis
    options. mod_dur reuses duration_moex (MOEX's own reported modified
    duration) -- this pipeline never computes duration from cashflows for
    universe-wide bonds, same tradeoff as the g_spread proxy itself.
    """
    rows = db.universe_snapshot_rows(con, snap_date)
    bonds = []
    for r in rows:
        signal_excluded = bool(r["signal_excluded"])
        rating = r["rating"]
        g_spread_bp = r["g_spread_bp"]
        # implied_fair_spread_bp/mispricing_bp are already NULL in the DB
        # when signal_excluded (universe_pipeline.py nulls them before
        # insert) -- but pd_market/pd_rating/pd_gap are recomputed here from
        # the still-present raw rating/g_spread_bp, so they need the same
        # exclusion applied explicitly or they'd silently un-exclude the
        # signal on the PD axis while the bp axis stays correctly excluded.
        pd_rating = lookup(rating)["yr1"] if (rating and not signal_excluded) else None
        pd_market = ((g_spread_bp / 10000.0) / (1 - RECOVERY_RATE)
                     if (g_spread_bp is not None and not signal_excluded) else None)
        pd_gap = (pd_market - pd_rating) if (pd_market is not None and pd_rating is not None) else None
        mod_dur = round(r["duration_moex"] / 365.0, 2) if r["duration_moex"] else None
        bonds.append({
            "secid": r["secid"],
            "short_name": r["short_name"],
            "emitent_title": r["emitent_title"],
            "emitent_inn": r["emitent_inn"],
            "sector": issuer_sectors.get(r["emitent_inn"]),
            "mat_date": r["mat_date"],
            "offer_date": r["offer_date"],
            "coupon_pct": r["coupon_pct"],
            "coupon_type": "fixed" if r["coupon_pct"] is not None else "floater",
            "coupon_period": r["coupon_period"],
            "coupon_freq": _coupon_freq_bucket(r["coupon_period"]),
            "list_level": r["list_level"],
            "qualified": bool(r["qualified"]),
            "last_pct": r["last_pct"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
            "g_spread_bp": round(g_spread_bp, 0) if g_spread_bp is not None else None,
            "duration_yrs": mod_dur,
            "mod_dur": mod_dur,
            "tenor_yrs": round(r["tenor_yrs"], 2) if r["tenor_yrs"] is not None else None,
            "rf_rate_pct": round(r["rf_rate"] * 100, 3) if r["rf_rate"] is not None else None,
            "volume": r["volume"],
            "num_trades": r["num_trades"],
            "yield_source": r["yield_source"],
            "rating": rating,
            "implied_fair_spread_bp": (round(r["implied_fair_spread_bp"], 0)
                                        if r["implied_fair_spread_bp"] is not None else None),
            "mispricing_bp": (round(r["mispricing_bp"], 0)
                              if r["mispricing_bp"] is not None else None),
            "pd_market": round(pd_market, 5) if pd_market is not None else None,
            "pd_rating": round(pd_rating, 5) if pd_rating is not None else None,
            "pd_gap": round(pd_gap, 5) if pd_gap is not None else None,
            "in_watchlist": bool(r["in_watchlist"]),
            "signal_excluded": signal_excluded,
            "vdo_scope": bool(r["vdo_scope"]),
            "aey_base_pct": round(r["aey_base"] * 100, 2) if r["aey_base"] is not None else None,
            "aey_stress_pct": (round(r["aey_stress"] * 100, 2)
                                if r["aey_stress"] is not None else None),
            "aey_bull_pct": round(r["aey_bull"] * 100, 2) if r["aey_bull"] is not None else None,
            "aey_base_risk_adj_pct": (round(r["aey_base_risk_adj"] * 100, 2)
                                       if r["aey_base_risk_adj"] is not None else None),
            "aey_risk_decomposition": _aey_risk_decomposition_pct(r["aey_base"], rating),
            # Task A -- PD-adjusted expected-value inflection
            # (compute/inflection.py), computed and stored per-bond by
            # universe_pipeline.run_universe()'s second pass. yield_zone is
            # one of 'green'/'caution'/'negative_ev'/'unrated'; ev_positive
            # is the pure floor_ytm check (None when unrated/unclassifiable,
            # distinct from False).
            "yield_zone": r["yield_zone"],
            "ev_positive": bool(r["ev_positive"]) if r["ev_positive"] is not None else None,
        })
    return bonds


def _aey_risk_decomposition_pct(aey_base: float | None, rating: str | None) -> dict | None:
    """Percent-scaled view of portfolio.aey_risk_decomposition() for export --
    same PD/recovery-rate split that aey_base_risk_adj_pct already blends,
    just exposed as the two scenarios instead of only their weighted sum.
    expected_value_pct always equals aey_base_risk_adj_pct (mod rounding)."""
    d = portfolio.aey_risk_decomposition(aey_base, rating)
    if d is None:
        return None
    return {
        "p_survive_pct": round(d["p_survive"] * 100, 3),
        "return_if_survive_pct": round(d["return_if_survive"] * 100, 2),
        "p_default_pct": round(d["p_default"] * 100, 3),
        "return_if_default_pct": round(d["return_if_default"] * 100, 2),
        "expected_value_pct": round(d["expected_value"] * 100, 2),
    }


def ofz_bundle(con, scenarios: dict) -> dict:
    """Real OFZ data (ofz_pipeline.py, `hybond ofz`) + carry/roll-down
    projections under the same base/stress/bull CBR scenarios used
    everywhere else in this dashboard -- not a separate/hardcoded scenario
    set. `scenarios` is the SAME dict main() already computed once via
    portfolio.get_rate_scenarios(con), passed in rather than recomputed."""
    from hybond import ofz_pipeline

    db.init_ofz(con)
    snap_date = db.latest_ofz_snap_date(con)
    if not snap_date:
        return {"snap_date": None, "bonds": [], "is_fallback": scenarios.get("is_fallback"),
                "rate_scenarios": {}}

    rows = db.ofz_snapshot_rows(con, snap_date)
    projections_by_secid = {
        p["secid"]: p["projections"]
        for p in ofz_pipeline.carry_rolldown_for_snapshot(con, snap_date, scenarios)
    }

    bonds = []
    for r in rows:
        bonds.append({
            "secid": r["secid"], "short_name": r["short_name"], "isin": r["isin"],
            "board_id": r["board_id"], "instrument_type": r["instrument_type"],
            "mat_date": r["mat_date"], "coupon_pct": r["coupon_pct"],
            "last_pct": r["last_pct"], "dirty_price_pct": r["dirty_price_pct"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
            "mod_dur": round(r["mod_dur"], 3) if r["mod_dur"] is not None else None,
            "convexity": round(r["convexity"], 3) if r["convexity"] is not None else None,
            "tenor_yrs": r["tenor_yrs"], "volume": r["volume"], "num_trades": r["num_trades"],
            "yield_source": r["yield_source"],
            "carry_rolldown": projections_by_secid.get(r["secid"]),  # None if excluded (near-maturity/no coupon)
        })

    return {
        "snap_date": snap_date,
        "bonds": bonds,
        "rate_scenarios": {
            "base": scenarios.get("base"), "stress": scenarios.get("stress"),
            "bull": scenarios.get("bull"), "source_meeting": scenarios.get("source_date"),
            # Provenance fields for the OFZ tab's Base/Stress/Bull labels
            # (Task 5): the exact meeting date + forecast ranges the base/
            # stress/bull deltas above were derived from, straight from the
            # same get_rate_scenarios() dict -- no second copy, no separate
            # lookup that could drift from it.
            "current_rate": scenarios.get("current_rate"),
            "cbr_2026": scenarios.get("cbr_2026"),
            "cbr_2027": scenarios.get("cbr_2027"),
            "cbr_2029": scenarios.get("cbr_2029"),
        },
        "is_fallback": scenarios.get("is_fallback"),
        "min_tenor_for_projection_years": ofz_pipeline.MIN_TENOR_FOR_PROJECTION_YEARS,
        "graduated_shift": ofz_pipeline.ofz_carry.GRADUATED_SHIFT,
    }


def universe_snapshot_bundle(con) -> dict:
    """Three time-buckets of universe data for the market map's time toggle
    (Сейчас / -1 нед. / -1 мес.), plus the derived curve/fair-value overlays.

    universe_snapshot only has history for days `hybond universe` actually
    ran -- on a fresh install that's a single date. Each bucket falls back
    to the nearest available snap_date ON OR BEFORE its target cutoff (never
    a future date relative to the bucket's intent) and reports how many days
    stale that fallback is, so the dashboard can show an honest "no history
    yet" note instead of silently mislabeling today's data as a week old.
    """
    db.init_universe(con)
    latest = db.latest_universe_snap_date(con)
    if not latest:
        empty = {"snap_date": None, "bonds": []}
        return {"universe_now": empty, "universe_week_ago": empty,
                "universe_month_ago": empty, "rf_curve": [],
                "fair_value_bp_by_tier": {}, "yield_inflection_by_rating": {},
                "yield_inflection_ceiling_multiple": None,
                "yield_inflection_caution_multiple": None,
                "yield_inflection_unrated_max_pct": inflection.UNRATED_MAX_YIELD_PCT,
                "yield_inflection_rated_absolute_max_pct": inflection.RATED_ABSOLUTE_MAX_AEY_PCT}

    issuer_sectors = {r["issuer_id"]: r["sector"]
                      for r in con.execute("SELECT issuer_id, sector FROM issuer")}
    latest_d = date.fromisoformat(latest)
    earliest = con.execute(
        "SELECT MIN(snap_date) m FROM universe_snapshot").fetchone()["m"]

    buckets = {}
    for key, days_back in (("universe_now", 0), ("universe_week_ago", 7),
                           ("universe_month_ago", 30)):
        cutoff = (latest_d - timedelta(days=days_back)).isoformat()
        sd = _nearest_snap_date_on_or_before(con, cutoff)
        no_history = False
        if not sd:
            # Not enough accumulated history for this lookback yet -- fall
            # back to the oldest snapshot we do have rather than showing an
            # empty chart; if that's the same day as "now" (fresh install,
            # one day of data total), there's genuinely no comparison point.
            sd = earliest
            no_history = (sd == latest) if days_back > 0 else False
        stale_days = (date.fromisoformat(latest) - date.fromisoformat(sd)).days
        buckets[key] = {
            "snap_date": sd,
            "bonds": _bonds_for_snap_date(con, sd, issuer_sectors),
            "stale_days": stale_days,
            "no_history": no_history,
        }

    # rf_curve: derived from the (tenor_yrs, rf_rate) pairs already
    # interpolated per bond for today's run -- no extra network fetch,
    # always in sync with the curve actually used for today's G-spreads.
    seen = {}
    for b in buckets["universe_now"]["bonds"]:
        if b["tenor_yrs"] is not None and b["rf_rate_pct"] is not None:
            seen[round(b["tenor_yrs"], 1)] = b["rf_rate_pct"]
    rf_curve = [{"tenor_yrs": t, "rf_pct": r} for t, r in sorted(seen.items())]

    fair_value_bp_by_tier = {
        tier[2:]: round(implied_fair_spread_bp(tier), 0)
        for tier in FAIR_VALUE_TIERS
    }

    # Task A -- PD-adjusted expected-value inflection (compute/inflection.py):
    # per-rating-tier floor_ytm/ceiling for the Рынок tab's "Показать предел
    # доходности" overlay. The ceiling/caution multiples must match what
    # universe_pipeline.run_universe() actually used to classify today's
    # bonds' yield_zone -- recomputed here from the SAME live distribution
    # (today's rated, non-excluded bonds' g_spread_bp/implied_fair_spread_bp
    # ratios) rather than re-derived independently, since the per-run
    # multiples themselves aren't persisted anywhere, only their effect
    # (yield_zone/ev_positive per bond).
    now_bonds = buckets["universe_now"]["bonds"]
    ratios = [
        b["g_spread_bp"] / b["implied_fair_spread_bp"]
        for b in now_bonds
        if not b["signal_excluded"] and b["implied_fair_spread_bp"]
        and b["g_spread_bp"] is not None and b["g_spread_bp"] > 0
    ]
    ceiling_mult, caution_mult = inflection.ceiling_multiples_from_distribution(ratios)
    inflection_table = inflection.yield_inflection_by_rating(ceiling_mult, caution_mult)
    yield_inflection_by_rating = {
        rating: {
            "floor_ytm_pct": round(row["floor_ytm"] * 100, 2),
            "ceiling_spread_bp": round(row["ceiling_spread_bp"], 0),
            "caution_spread_bp": round(row["caution_spread_bp"], 0),
        }
        for rating, row in inflection_table.items()
    }

    return {**buckets, "rf_curve": rf_curve,
            "fair_value_bp_by_tier": fair_value_bp_by_tier,
            "yield_inflection_by_rating": yield_inflection_by_rating,
            "yield_inflection_ceiling_multiple": round(ceiling_mult, 2),
            "yield_inflection_caution_multiple": round(caution_mult, 2),
            "yield_inflection_unrated_max_pct": inflection.UNRATED_MAX_YIELD_PCT,
            "yield_inflection_rated_absolute_max_pct": inflection.RATED_ABSOLUTE_MAX_AEY_PCT}


def known_issuers_meta(con) -> dict[str, dict]:
    """Every issuer_id already in the `issuer` table -- NOT just the current
    watchlist.json subset (DATA.issuers). The issuer table also holds
    held-back candidates that were investigated and explicitly NOT promoted
    (e.g. Альфа Дон Транс, ЛКХ, ПЛАЗА-Телеком, СМАК -- see
    project_watchlist_flags memory) -- those must not be re-surfaced as
    "new" scan candidates just because they're absent from watchlist.json.

    Also carries current rating (value + rated_on) and filing count/latest
    published date per issuer, so the dashboard's company-scan panel can
    detect "hybond's own data changed since you last scanned this issuer"
    (Part B: new rating captured, new AZIPI filing appeared) without a
    second export round-trip -- a plain snapshot comparison against
    whatever was true at scan time (stored client-side), not a live diff.
    """
    rows = con.execute("SELECT issuer_id, name FROM issuer").fetchall()
    filing_stats = {
        r["issuer_id"]: {"count": r["cnt"], "latest_published": r["latest"]}
        for r in con.execute(
            "SELECT issuer_id, COUNT(*) cnt, MAX(published) latest "
            "FROM filing GROUP BY issuer_id")
    }
    out = {}
    for r in rows:
        rating_row = db.current_rating_row(con, r["issuer_id"])
        fstat = filing_stats.get(r["issuer_id"], {"count": 0, "latest_published": None})
        out[r["issuer_id"]] = {
            "name": r["name"],
            "rating": rating_row["rating"] if rating_row else None,
            "rated_on": rating_row["rated_on"] if rating_row else None,
            "rating_needs_verification": bool(rating_row["needs_verification"]) if rating_row else None,
            "filing_count": fstat["count"],
            "filing_latest_published": fstat["latest_published"],
        }
    return out


def data_notes(con, watchlist: dict) -> dict:
    """Slice status banners, computed live from the store every export run --
    these used to be hand-maintained strings (a stale one claimed synthetic
    day-2 data and an '11 issuers' Slice 4 count long after both were
    overtaken by real runs, 2026-07-30). Regenerating from queries means the
    banner can't drift from reality again the way the strings did."""
    real_dates = [r[0] for r in con.execute(
        "SELECT DISTINCT snap_date FROM spread_snapshot ORDER BY snap_date")]
    synthetic_events = con.execute(
        "SELECT COUNT(*) c FROM event WHERE summary LIKE '%SYNTHETIC%'").fetchone()["c"]

    slice1_slice2 = (
        f"Real data throughout: {len(real_dates)} trading day(s) now in "
        f"spread_snapshot ({real_dates[0]} to {real_dates[-1]}) from live "
        f"`hybond run` pulls -- no synthetic/jittered data remains (the "
        f"original single-day demo, scripts/seed_day2_demo.py, was superseded "
        f"by real runs on those same calendar dates). Floaters with a "
        f"not-yet-fixed next coupon can't be priced from cashflows (see "
        f"pipeline.py) -- those use MOEX's own reported yield as a G-spread "
        f"proxy instead (marked '~'/'*' in the dashboard, yield_source='moex' "
        f"in spread_snapshot). A bond still shows no price only if it had "
        f"zero trades on the snapshot date."
    )

    classified_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM event")}
    filing_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM filing")}
    backlog_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM filing WHERE classified=0")}
    wl_ids = [k for k in watchlist if not k.startswith("_")]
    zero_filing_ids = [i for i in wl_ids if i not in filing_issuers]
    zero_filing_names = ", ".join(
        watchlist[i].get("name", i) for i in zero_filing_ids)

    slice3 = (
        f"{len(classified_issuers)} issuer(s) have at least one real "
        f"CLASSIFIED event (hy-event-classify), {len(backlog_issuers)} "
        f"issuer(s) have real filings scraped from e-disclosure.azipi.ru "
        f"sitting unclassified in the `filing` table (pipeline backlog, not "
        f"missing data -- see `hybond queue`), and {len(zero_filing_ids)} "
        f"issuer(s) genuinely have zero filings scraped at all: "
        f"{zero_filing_names or '(none)'}."
        + (f" {synthetic_events} [SYNTHETIC DEMO] placeholder event(s) still "
           f"remain -- investigate before trusting this note." if synthetic_events
           else " No [SYNTHETIC DEMO] placeholder events remain platform-wide.")
    )

    pillars_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM pillars_snapshot")}
    zero_pillars_ids = [i for i in wl_ids if i not in pillars_issuers]
    zero_pillars_names = ", ".join(
        watchlist[i].get("name", i) for i in zero_pillars_ids)

    slice4 = (
        f"Slice 4 financials: real data only. {len(pillars_issuers)} of "
        f"{len(wl_ids)} watchlist issuers have a real ГИР БО financials row "
        f"(parser-api.com, `hybond financials`) feeding hybond.compute.pillars. "
        f"{len(zero_pillars_ids)} issuer(s) have no financials_snapshot/"
        f"pillars_snapshot row: {zero_pillars_names or '(none)'} -- for the "
        f"MFI-regulated names among these this is a confirmed structural gap "
        f"(regulated under CBR, not indexed in ГИР БО), not a fetch failure; "
        f"see their watchlist.json notes. The weekly cron "
        f"(scripts/weekly_financials.sh) that keeps this current was fixed "
        f"2026-07-30 but had not yet reached its first scheduled Sunday run "
        f"as of this export -- check financials_snapshot dates directly "
        f"rather than assuming the cron has already run."
    )

    return {"slice1_slice2": slice1_slice2, "slice3": slice3, "slice4": slice4}


def _aey_source_note(scenarios: dict) -> str:
    if scenarios["is_fallback"]:
        return ("AEY рассчитан по консервативным значениям по умолчанию "
                 "(нет данных последнего заседания ЦБ)")
    d = scenarios["source_date"]
    return f"AEY рассчитан по данным заседания ЦБ от {d[8:10]}.{d[5:7]}.{d[0:4]}"


def universe_for_dashboard(con, scenarios: dict) -> dict:
    """Back-compat shape for the existing 'Универсум' screener tab (single
    latest snapshot, no market-map fields it doesn't use)."""
    db.init_universe(con)
    snap_date = db.latest_universe_snap_date(con)
    if not snap_date:
        return {"snap_date": None, "bonds": []}
    issuer_sectors = {r["issuer_id"]: r["sector"]
                      for r in con.execute("SELECT issuer_id, sector FROM issuer")}
    return {
        "snap_date": snap_date,
        "bonds": _bonds_for_snap_date(con, snap_date, issuer_sectors),
        "aey_source_date": scenarios["source_date"],
        "aey_is_fallback": scenarios["is_fallback"],
        "aey_source_note": _aey_source_note(scenarios),
    }


def universe_aey_by_secid(con, snap_date: str | None) -> dict[str, dict]:
    """secid -> {mispricing_bp, aey_base, aey_stress, aey_bull,
    aey_base_risk_adj, yield_zone, ev_positive} from the latest
    universe_snapshot, for enriching the watchlist tab's per-issuer bonds
    (which come from factlists -- a completely separate pipeline from
    universe_pipeline.py). This is a JOIN by secid, not a recomputation --
    there is exactly one AEY calculation in the codebase (portfolio.py), and
    exactly one yield-zone classification (compute/inflection.py, Task A),
    each read from two places. yield_zone/ev_positive feed Task B's verdict
    component on the per-bond detail drawer."""
    if not snap_date:
        return {}
    rows = con.execute(
        "SELECT secid, mispricing_bp, aey_base, aey_stress, aey_bull, aey_base_risk_adj, "
        "yield_zone, ev_positive FROM universe_snapshot WHERE snap_date=?", (snap_date,)).fetchall()
    return {r["secid"]: dict(r) for r in rows}


def cbr_export(con, scenarios: dict) -> dict:
    """CBR key-rate meeting tracker, shaped for the dashboard's fourth tab.
    Reads cbr_meeting + cbr_meeting_diff only -- no computation happens
    here, `hybond cbr --fetch-all`/`--diff-all` (cbr_tracker.py) already did
    it. current_rate/next_meeting/cbr_2026_forecast/cbr_2027_forecast come
    straight from cbr_meeting's own fields (rate_after, rate_2026_mid,
    rate_2027_mid) rather than analysis_json's cbr_2026_avg_forecast/
    cbr_2027_avg_forecast, which still carry a previously-flagged factual
    mismatch against the real forecast documents (analysis_json was seeded
    verbatim from a user-authored draft that predates that correction) --
    cbr_meeting's own extracted fields are the verified source of truth."""
    from hybond import cbr_tracker

    db.init_db(con)
    db.init_cbr(con)
    db.init_cbr_diff(con)

    today = date.today().isoformat()
    schedule = cbr_tracker.CBR_MEETINGS_2026

    latest = con.execute(
        "SELECT * FROM cbr_meeting WHERE rate_after IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1").fetchone()
    latest = dict(latest) if latest else None

    next_meeting = next((m["date"] for m in schedule if m["date"] > today), None)

    def _normalise_range(s):
        """"14,5–14,61" -> "14.5-14.61" -- display-only normalisation to the
        dashboard's Latin-decimal convention; does NOT touch the trailing
        digit itself (see cbr_tracker.py's module docstring on the
        occasional fused-footnote-marker caveat -- that's a data-quality
        note for whoever reads the number, not something to silently trim)."""
        return s.replace(",", ".").replace("–", "-").replace("—", "-") if s else s

    # Deltas come from portfolio.get_rate_scenarios(con) -- the SAME live
    # computation universe_pipeline.py uses for per-bond AEY -- rather than
    # analysis_json's static aey_delta_rate_* fields. Reading two different
    # sources for what should be one number was a correctness bug: this
    # tab and the universe/portfolio view could show different scenario
    # deltas for the same meeting date. is_fallback is always surfaced
    # (never silently swallowed) so the dashboard can show a warning badge
    # instead of presenting a fallback constant as a real forecast-derived one.
    rate_scenarios = {
        "base_annual_bp": round(scenarios["base"] * 10000),
        "stress_annual_bp": round(scenarios["stress"] * 10000),
        "bull_annual_bp": round(scenarios["bull"] * 10000),
        "source_meeting": scenarios["source_date"],
        "cbr_2026_forecast": _normalise_range(scenarios["cbr_2026"]),
        "cbr_2027_forecast": _normalise_range(scenarios["cbr_2027"]),
        "is_fallback": scenarios["is_fallback"],
    }

    meetings = []
    for m in schedule:
        row = con.execute("SELECT * FROM cbr_meeting WHERE meeting_date=?",
                          (m["date"],)).fetchone()
        row = dict(row) if row else {}
        diff_row = con.execute("SELECT * FROM cbr_meeting_diff WHERE meeting_date=?",
                               (m["date"],)).fetchone()
        diff = None
        if diff_row:
            diff_row = dict(diff_row)
            diff = {
                "pace_change": diff_row.get("pace_change"),
                "forecast_2026_shift_bp": diff_row.get("forecast_2026_shift_bp"),
                "forecast_2027_shift_bp": diff_row.get("forecast_2027_shift_bp"),
                "portfolio_signal": diff_row.get("portfolio_signal"),
                "signal_magnitude": diff_row.get("signal_magnitude"),
                "tone_shift": diff_row.get("tone_shift"),
                "diff_note_ru": diff_row.get("diff_note_ru"),
            }
        meetings.append({
            "date": m["date"],
            "is_anchor": m["anchor"],
            "rate_after": row.get("rate_after"),
            "rate_change_bp": row.get("rate_change_bp"),
            "is_past": m["date"] <= today,
            "has_press": bool(row.get("press_release_text")),
            "has_forecast": bool(row.get("forecast_summary")),
            "press_release_excerpt": (row.get("press_release_text") or "")[:400] or None,
            "press_release_url": (f"https://cbr.ru/press/pr/?file="
                                   f"{m['date'][8:10]}{m['date'][5:7]}{m['date'][0:4]}_133000key.htm"
                                   if row.get("press_release_text") else None),
            "diff": diff,
        })

    recent_diffs = []
    for r in con.execute(
            "SELECT * FROM cbr_meeting_diff ORDER BY meeting_date DESC LIMIT 3").fetchall():
        r = dict(r)
        recent_diffs.append({
            "date": r["meeting_date"],
            "note": r.get("diff_note_ru"),
            "signal": (r.get("portfolio_signal") or "neutral").upper(),
            "signal_magnitude": r.get("signal_magnitude"),
        })

    return {
        "current_rate": latest.get("rate_after") if latest else None,
        "next_meeting": next_meeting,
        "rate_scenarios": rate_scenarios,
        "meetings": meetings,
        "recent_diffs": recent_diffs,
    }


def company_scan_history_export(con) -> list[dict]:
    """Latest company_scan_result row per ИНН (src/hybond/company_scan_pipeline.py,
    `hybond scan-company`/`scan-candidates`) -- lets the dashboard's
    "Проверить компанию" panel render a past scan straight from the DB, no
    fresh API call needed. Free-text queries that never resolved to an ИНН
    (see company_scan_pipeline.resolve_inn) aren't trackable this way and
    are excluded here, same limitation as the browser's own localStorage
    history -- they simply don't appear in this list.

    json_addendum is stored as a TEXT blob in the DB (see
    db.insert_company_scan_result) -- parsed back into a real object here so
    the dashboard can feed it straight into renderNarrativeCardHtml() without
    a second JSON.parse of a JSON string inside JSON."""
    db.init_company_scan(con)
    latest = db.latest_company_scan_by_inn(con)
    out = []
    for inn, row in sorted(latest.items(), key=lambda kv: kv[1].get("scanned_at") or "", reverse=True):
        addendum = row.get("json_addendum")
        card = None
        if addendum:
            try:
                card = json.loads(addendum)
            except json.JSONDecodeError:
                card = None
        out.append({
            "id": row["id"], "inn": inn, "query": row["query"],
            "issuer_name": row.get("issuer_name") or row["query"],
            "scanned_at": row["scanned_at"], "model": row.get("model"),
            "issuer_quality": row.get("issuer_quality"),
            "bond_suitability": row.get("bond_suitability"),
            "confidence": row.get("confidence"),
            "data_coverage_pct": row.get("data_coverage_pct"),
            "raw_markdown": row.get("raw_markdown"),
            "card": card,
            "input_tokens": row.get("input_tokens"), "output_tokens": row.get("output_tokens"),
            "cache_read_tokens": row.get("cache_read_tokens"), "web_searches": row.get("web_searches"),
        })
    return out


def main() -> None:
    watchlist = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    watchlist = {k: v for k, v in watchlist.items() if not k.startswith("_")}

    report = json.loads((ROOT / "data" / "report.json").read_text(encoding="utf-8"))
    factlists = latest_factlists()

    con = db.connect()
    db.init_db(con)
    db.init_cbr(con)
    scenarios = portfolio.get_rate_scenarios(con)  # single computation, shared below
    universe = universe_for_dashboard(con, scenarios)
    market_map = universe_snapshot_bundle(con)
    ofz = ofz_bundle(con, scenarios)
    cbr = cbr_export(con, scenarios)
    fhist = flag_history(con)
    shist = spread_history_by_secid(con)
    fcounts = filing_counts_by_issuer(con)
    aey_by_secid = universe_aey_by_secid(con, universe["snap_date"])
    convexity_by_secid_map = convexity_by_secid(con)
    mis_delta_by_secid = mispricing_delta_by_secid(con)
    events_full_by_issuer = full_events_by_issuer(con)
    expected_returns = expected_returns_by_secid(con)

    issuers = []
    for issuer_id, meta in watchlist.items():
        fl = factlists.get(issuer_id, {})
        thesis_row = db.get_thesis(con, issuer_id)
        thesis = None
        if thesis_row:
            thesis = {
                "thesis": thesis_row["thesis"],
                "assumptions": thesis_row["assumptions"],
                "invalidation": thesis_row["invalidation"],
                "status": thesis_row["status"],
                "updated": thesis_row["updated"],
            }
        rating_row = db.current_rating_row(con, issuer_id)
        rating_needs_verification = bool(rating_row["needs_verification"]) if rating_row else False
        ratings_all = db.all_current_ratings(con, issuer_id)
        rating_detail = None
        if rating_row:
            rated_on = rating_row["rated_on"]
            is_stale = None
            if rated_on:
                try:
                    is_stale = db.rating_is_stale(rated_on)
                except ValueError:
                    is_stale = None
            rating_detail = {
                "agency": rating_row["agency"], "rated_on": rated_on,
                "outlook": rating_row["outlook"], "action": rating_row["action"],
                "is_stale": is_stale,
            }

        bonds = fl.get("bonds", [])
        wall_by_secid = report.get("maturity_wall_by_secid", {})
        for b in bonds:
            b["spread_history"] = shist.get(b["secid"], [])
            # Real per-bond monthly coupon/principal buckets (pipeline.py,
            # same build_wall() the watchlist-wide maturity wall uses, just
            # scoped to this one secid's own cashflows) -- feeds the
            # Портфель tab's weight-scaled combined wall. Empty for floaters
            # (no fixed schedule, same gap the watchlist-wide wall already
            # has) or if `hybond run` hasn't been re-run since this field
            # was added.
            b["maturity_wall"] = wall_by_secid.get(b["secid"], [])
            aey = aey_by_secid.get(b["secid"])
            b["mispricing_bp"] = round(aey["mispricing_bp"], 0) if aey and aey["mispricing_bp"] is not None else None
            b["mispricing_trend"] = mis_delta_by_secid.get(
                b["secid"], {"delta_bp": None, "direction": None})
            b["aey_base_pct"] = round(aey["aey_base"] * 100, 2) if aey and aey["aey_base"] is not None else None
            b["aey_stress_pct"] = round(aey["aey_stress"] * 100, 2) if aey and aey["aey_stress"] is not None else None
            b["aey_bull_pct"] = round(aey["aey_bull"] * 100, 2) if aey and aey["aey_bull"] is not None else None
            b["aey_base_risk_adj_pct"] = (round(aey["aey_base_risk_adj"] * 100, 2)
                                          if aey and aey["aey_base_risk_adj"] is not None else None)
            b["aey_risk_decomposition"] = _aey_risk_decomposition_pct(
                aey["aey_base"] if aey else None, meta.get("rating"))
            b["expected_return"] = expected_returns.get(b["secid"], {})
            # Task B -- verdict component on the per-bond detail drawer:
            # yield_zone/ev_positive straight from universe_snapshot (Task
            # A), same JOIN pattern as the AEY fields just above.
            b["yield_zone"] = aey["yield_zone"] if aey else None
            b["ev_positive"] = (bool(aey["ev_positive"])
                                 if aey and aey["ev_positive"] is not None else None)
            b["convexity"] = convexity_by_secid_map.get(b["secid"])
            # Registered-vs-placed volume (Task 3 follow-up): placement_pct/
            # placement_partial_since are raw facts from the bond table
            # (pipeline.py/db.upsert_bond); is_placement_stalled is the same
            # interpretive-flag-computed-at-export-time pattern rating_detail's
            # is_stale already uses (db.rating_is_stale above), not baked into
            # the factlist itself.
            b["is_placement_stalled"] = db.placement_is_stalled(
                b.get("placement_pct"), b.get("placement_partial_since"))

        issuers.append({
            "issuer_id": issuer_id,
            "name": meta.get("name"),
            "sector": meta.get("sector"),
            "risk_tier": meta.get("risk_tier", "core"),
            "rating": meta.get("rating"),
            "rating_needs_verification": rating_needs_verification,
            "rating_detail": rating_detail,
            "ratings_all": ratings_all,
            "data_conflicts": data_conflicts_for_issuer(con, issuer_id, ratings_all),
            "note": meta.get("note"),
            "edisclosure_id": meta.get("edisclosure_id"),
            "snap_date": fl.get("snap_date"),
            "bonds": bonds,
            "flags_today": fl.get("flags", []),
            "flag_history": fhist.get(issuer_id, []),
            "stat_warming_up": fl.get("stat_warming_up", True),
            "events": fl.get("events_since_last", []),
            "market_notes": fl.get("market_notes_since_last", []),
            "events_full": events_full_by_issuer.get(issuer_id, []),
            "six_pillars": fl.get("six_pillars", {}),
            "six_pillars_delta": fl.get("six_pillars_delta", {}),
            "thesis": thesis,
            "filing_counts": fcounts.get(issuer_id, {"total": 0, "classified": 0}),
        })
    notes = data_notes(con, watchlist)
    known_issuers = known_issuers_meta(con)
    company_scan_history = company_scan_history_export(con)
    queue_count_val = queue_count(con)
    con.close()

    out = {
        "generated_at": report.get("snap_date"),
        "day1_snap_date": report.get("snap_date"),
        "day2_snap_date": issuers[0]["snap_date"] if issuers else None,
        "queue_count": queue_count_val,
        "spread_screen_day1": report.get("spread_screen", []),
        "maturity_wall": report.get("maturity_wall", []),
        "redemptions_by_issuer": report.get("redemptions_by_issuer", {}),
        "issuers": issuers,
        "aey_source_note": _aey_source_note(scenarios),
        "aey_is_fallback": scenarios["is_fallback"],
        "universe": universe,
        "universe_now": market_map["universe_now"],
        "universe_week_ago": market_map["universe_week_ago"],
        "universe_month_ago": market_map["universe_month_ago"],
        "rf_curve": market_map["rf_curve"],
        "fair_value_bp_by_tier": market_map["fair_value_bp_by_tier"],
        # Task A -- PD-adjusted expected-value inflection (compute/
        # inflection.py): per-rating floor/ceiling for the Рынок tab's
        # "Показать предел доходности" overlay, plus the live multiples used
        # so the chart can show its own calibration transparently.
        "yield_inflection_by_rating": market_map["yield_inflection_by_rating"],
        "yield_inflection_ceiling_multiple": market_map["yield_inflection_ceiling_multiple"],
        "yield_inflection_caution_multiple": market_map["yield_inflection_caution_multiple"],
        "yield_inflection_unrated_max_pct": market_map["yield_inflection_unrated_max_pct"],
        "yield_inflection_rated_absolute_max_pct": market_map["yield_inflection_rated_absolute_max_pct"],
        # Task B verdict component, Layer 3 (pro-only): "PD table value
        # used" -- the raw 1yr PD (%) default_probs.lookup() reads for a
        # bond's rating, same table pd_adjusted_aey/mispricing_bp already
        # use, just exposed directly instead of only through derived bp/pp
        # figures.
        "pd_by_rating": {rating: round(probs["yr1"] * 100, 3)
                          for rating, probs in DEFAULT_PROBS.items()},
        "ofz": ofz,
        # Single source of truth for the dashboard's "Топ-кандидаты" toggle
        # (Портфельный рейтинг table) -- sourced directly from promote.py's
        # own defaults and default_probs' own rating scale, not hand-copied
        # into JS, so the live-in-browser filter can never silently drift
        # from what `hybond promote` itself would report. Thresholds are a
        # starting point, not final policy -- see promote.py's docstring.
        "promotion_thresholds": {
            "rating_floor": promote.RATING_FLOOR_DEFAULT,
            "n_before_cap": promote.N_BEFORE_CAP_DEFAULT,
            "per_issuer_cap": promote.PER_ISSUER_CAP_DEFAULT,
            "target_after_cap": promote.TARGET_AFTER_CAP_DEFAULT,
            "min_volume": VDO_MIN_VOLUME,
        },
        "rating_order": RATING_ORDER,
        "placement_thresholds": {
            "stall_weeks": db.PLACEMENT_STALL_WEEKS,
            "stall_threshold_pct": db.PLACEMENT_STALL_THRESHOLD_PCT,
            "full_threshold_pct": db.PLACEMENT_FULL_THRESHOLD_PCT,
        },
        "cbr": cbr,
        "data_notes": notes,
        "known_issuers": known_issuers,
        "company_scan_history": company_scan_history,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(issuers)} issuers)")


if __name__ == "__main__":
    main()
