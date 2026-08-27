"""Portfolio scenario layer: per-bond Adjusted Expected Yield (AEY) under
three CBR rate-path scenarios (base/stress/bull), and the live rate-scenario
loader that drives it.

Rate deltas are computed fresh from the most recent cbr_meeting's own
rate_after/rate_2026_mid/rate_2027_mid/rate_2029_mid columns, never from
analysis_json's static aey_delta_rate_* fields -- those were seeded from a
user-authored draft and have since drifted from the corrected forecast
ranges (see export_dashboard_data.py's cbr_export docstring for the same
caveat about analysis_json's other fields).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from .cbr_tracker import _midpoint
from .compute import default_probs
from .store import db

logger = logging.getLogger(__name__)

# Commodity-linked bonds (Watchlist Filters UI task, 2026-08-27): coupon/
# redemption indexed to a metal price rather than a fixed rate, so this
# module's YTM/AEY math (and every preset's rating/YTM/AEY filters) simply
# doesn't apply to them -- excluded from every preset build regardless of
# preset_type. No `commodity_linked` column exists anywhere in the schema
# (checked: bond, spread_snapshot, universe_snapshot), so this is a literal
# id list, matched against secid OR bond.short_name, rather than a flag
# lookup. Add to this set if more commodity-linked issues are found.
COMMODITY_LINKED_BOND_IDS = frozenset({"SELSILV01", "SELGOLD002", "SELGOLD003"})


def _is_commodity_linked(secid: str, short_name: str | None) -> bool:
    """True if `secid` or `short_name` matches a known commodity-linked
    bond identifier (see COMMODITY_LINKED_BOND_IDS)."""
    return secid in COMMODITY_LINKED_BOND_IDS or (short_name or "") in COMMODITY_LINKED_BOND_IDS


FALLBACK_SCENARIOS = {
    "base": -0.010, "stress": 0.015, "bull": -0.020,
    "source_date": "fallback", "cbr_2026": "?", "cbr_2027": "?", "cbr_2029": "?",
    "current_rate": None, "is_fallback": True,
}


def _range_high(s: str | None) -> float | None:
    """Upper bound of a "14,5-14,6"-style range string -- same en-dash/comma
    normalisation as cbr_tracker._midpoint, but the high end rather than the
    average. Used for the stress scenario, which prices the worst outcome
    still inside the CBR's own published range, not the range's centre."""
    if not s:
        return None
    normalised = s.replace(",", ".").replace("–", "-").replace("—", "-")
    parts = normalised.split("-")
    try:
        return max(float(parts[0]), float(parts[-1]))
    except (ValueError, IndexError):
        return None


def get_rate_scenarios(con) -> dict:
    """Load current rate-path deltas (decimal, annual) for the AEY overlay
    from the most recent cbr_meeting row with a completed analysis
    (analysis_json IS NOT NULL).

    base   = 2027 forecast midpoint - current key rate (central case)
    bull   = 2029 forecast midpoint - current key rate (cutting cycle runs
             its full course to the CBR's own neutral-rate estimate)
    stress = 2026 forecast HIGH - current key rate (worst case still inside
             the CBR's own published range, not a made-up shock)

    Falls back to conservative static defaults, visibly flagged via
    is_fallback=True, when no meeting has both a completed analysis and all
    three forecast columns populated -- callers must surface that flag
    rather than silently treating a fallback delta as a real one.
    """
    row = con.execute(
        "SELECT meeting_date, rate_after, rate_2026_mid, rate_2027_mid, rate_2029_mid "
        "FROM cbr_meeting WHERE analysis_json IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1"
    ).fetchone()

    if row:
        current = row["rate_after"]
        high_2026 = _range_high(row["rate_2026_mid"])
        mid_2027 = _midpoint(row["rate_2027_mid"])
        mid_2029 = _midpoint(row["rate_2029_mid"])
        if current is not None and None not in (high_2026, mid_2027, mid_2029):
            return {
                "base": (mid_2027 - current) / 100.0,
                "stress": (high_2026 - current) / 100.0,
                "bull": (mid_2029 - current) / 100.0,
                "source_date": row["meeting_date"],
                "cbr_2026": row["rate_2026_mid"],
                "cbr_2027": row["rate_2027_mid"],
                "cbr_2029": row["rate_2029_mid"],
                "current_rate": current,
                "is_fallback": False,
            }

    return dict(FALLBACK_SCENARIOS)


def bond_aey(ytm: float | None, mod_dur_yrs: float | None, delta_rate: float) -> float | None:
    """Adjusted Expected Yield: running YTM plus the one-time price effect of
    `delta_rate` (decimal, annual) acting on this bond's modified duration --
    the standard first-order bond-math approximation dP/P = -ModDur * dy,
    expressed as a yield-equivalent addition to YTM. None when either input
    needed for the calc is missing, rather than silently treating unknown
    duration as zero duration."""
    if ytm is None or mod_dur_yrs is None:
        return None
    return ytm - mod_dur_yrs * delta_rate


def pd_adjusted_aey(aey: float | None, rating: str | None, horizon_yr: int = 1) -> float | None:
    """Expected-return version of AEY: (1 - PD) * aey - PD * LGD,
    using the same Эксперт РА default-probability table and LGD/recovery
    assumption as the mispricing signal (default_probs.py). None when there's
    no aey or no rating to look up a PD for -- an unrated bond stays
    unranked here rather than being silently treated as riskless.

    This turns a raw distress YTM into what it actually represents: a
    probability-weighted mix of "full yield" and "near-total loss on
    default", not a return to chase. It punishes low ratings hard (ruCCC's
    ~13.5% 1yr PD already halves a merely-high AEY), but does NOT by itself
    tame an already-absurd nominal YTM (hundreds/thousands of percent) --
    those numbers reflect a bond priced for restructuring regardless of the
    formal "default" event, so the linear (1-PD)*aey term still comes out
    huge. Callers ranking a candidate list should pair this with a YTM
    sanity cap (see export_dashboard_data.py / the CLI report), not rely on
    PD-weighting alone to exclude distress-priced bonds.
    """
    if aey is None or not rating:
        return None
    probs = default_probs.lookup(rating)
    if probs is None:
        return None
    key = default_probs._HORIZON_KEYS.get(horizon_yr, "yr1")
    pd = probs[key]
    return (1 - pd) * aey - pd * default_probs.LGD


def aey_risk_decomposition(aey: float | None, rating: str | None,
                            horizon_yr: int = 1) -> dict | None:
    """Same PD/recovery-rate lookup as pd_adjusted_aey(), but returns the two
    scenarios it blends instead of only the blended expected value -- so a
    caller (or a future UI) can show "survive at full yield" vs "default at
    near-total loss" as two numbers, not one that hides which one is doing
    the work. expected_value here must always equal pd_adjusted_aey()'s
    return for the same inputs -- this is a decomposition of that number,
    not an independent model (see test_portfolio.py for the consistency
    check). None under the same conditions pd_adjusted_aey() returns None."""
    if aey is None or not rating:
        return None
    probs = default_probs.lookup(rating)
    if probs is None:
        return None
    key = default_probs._HORIZON_KEYS.get(horizon_yr, "yr1")
    pd = probs[key]
    p_survive = 1 - pd
    return_if_survive = aey
    return_if_default = -default_probs.LGD
    expected_value = p_survive * return_if_survive + pd * return_if_default
    return {
        "p_survive": p_survive, "return_if_survive": return_if_survive,
        "p_default": pd, "return_if_default": return_if_default,
        "expected_value": expected_value,
    }


def apply_issuer_cap(rows: list[dict], per_issuer_cap: int = 2) -> list[dict]:
    """Enforce a max-N-series-per-issuer cap on an already-ranked list
    (best first) -- ranking a candidate list by return alone doesn't
    penalize single-name concentration risk. First observed on the initial
    top-15-by-aey_base_risk_adj run: 6 of 15 rows were all Самолёт series,
    crowding out every other issuer in what's supposed to read as a
    diversified candidate list, not one issuer's own curve.

    Rows must already be sorted by the ranking key -- this only filters,
    preserving relative order, so a later/lower-ranked row from an issuer
    that hasn't hit the cap still outranks an earlier/higher-ranked row from
    an issuer that has."""
    counts: dict[str, int] = {}
    capped = []
    for row in rows:
        inn = row.get("emitent_inn")
        n = counts.get(inn, 0)
        if n >= per_issuer_cap:
            continue
        counts[inn] = n + 1
        capped.append(row)
    return capped


def rank_candidates(con, limit: int = 15, per_issuer_cap: int = 2,
                     ytm_max: float = 0.80, in_watchlist_only: bool = True,
                     snap_date: str | None = None) -> list[dict]:
    """Portfolio candidate list: aey_base_risk_adj descending, with the YTM
    sanity bound from the mispricing-scaling investigation (PD-weighting
    alone doesn't tame nominal YTMs in the hundreds/thousands of percent --
    those reflect distress pricing, not a return to rank on) and the
    per-issuer cap above. `snap_date` defaults to the latest available.
    """
    snap_date = snap_date or db.latest_universe_snap_date(con)
    if not snap_date:
        return []
    query = ("SELECT * FROM universe_snapshot WHERE snap_date=? "
             "AND aey_base_risk_adj IS NOT NULL AND ytm < ?")
    params: list = [snap_date, ytm_max]
    if in_watchlist_only:
        query += " AND in_watchlist=1"
    query += " ORDER BY aey_base_risk_adj DESC"
    rows = [dict(r) for r in con.execute(query, params)]
    return apply_issuer_cap(rows, per_issuer_cap)[:limit]


def poisson_pmf(lam: float, n: int) -> float:
    """P(N = n) for a Poisson(lam) distribution -- used to price the
    probability of exactly n issuer defaults in the multi-default stress
    test below, given lam = E[defaults] = sum of the portfolio's per-issuer
    1yr PDs (the standard Poisson-binomial approximation for a sum of
    independent low-probability Bernoulli events)."""
    if lam < 0:
        raise ValueError(f"lam must be >= 0, got {lam}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    return math.exp(-lam) * lam ** n / math.factorial(n)


def _aggregate_positions_by_issuer(positions: list[dict]) -> list[dict]:
    """Collapse per-bond positions to per-issuer aggregates: summed weight,
    weight-contribution to portfolio AEY, and the weighted-average 1yr PD
    across that issuer's own positions (normally identical across an
    issuer's bonds since PD is rating-derived, but weight-averaged rather
    than just taking the first value in case they ever differ). Positions
    with pd_1y=None (no rating -> no PD) are excluded from the PD-driven
    ordering entirely -- an unrated issuer can't be placed in a "worst PD
    first" ranking, and silently treating it as PD=0 would be the wrong kind
    of wrong (understating risk, not just missing data)."""
    agg: dict[str, dict] = {}
    for p in positions:
        iid = p["issuer_id"]
        a = agg.setdefault(iid, {
            "issuer_id": iid, "name": p.get("name", iid),
            "weight": 0.0, "aey_contrib": 0.0, "pd_weight": 0.0, "pd_sum": 0.0,
        })
        w = p["weight_frac"]
        a["weight"] += w
        a["aey_contrib"] += w * p["aey_pct"]
        if p.get("pd_1y") is not None:
            a["pd_sum"] += w * p["pd_1y"]
            a["pd_weight"] += w
    issuers = []
    for a in agg.values():
        pd = (a["pd_sum"] / a["pd_weight"]) if a["pd_weight"] > 0 else None
        issuers.append({"issuer_id": a["issuer_id"], "name": a["name"],
                        "weight": a["weight"], "aey_contrib": a["aey_contrib"],
                        "pd_1y": pd})
    return issuers


def multi_default_stress_test(
    positions: list[dict],
    scenario_ns: tuple[int, ...] = (0, 1, 2, 3, 5, 8),
    lgd: float | None = None,
    ofz_rate_pct: float | None = None,
    cbr_rate_pct: float | None = None,
) -> dict:
    """Multi-default portfolio stress test (Portfolio stress test v2 task,
    2026-08-22): extends the single-issuer default stress test to N
    simultaneous defaults, worst-PD-first (pessimistic ordering).

    `positions` is a list of {issuer_id, name, weight_frac, aey_pct, pd_1y}
    dicts, one per held bond (weight_frac in [0, 1], aey_pct/pd_1y in
    percent-point / percent units respectively) -- already loaded from the
    DB-backed dashboard export upstream, this function does no I/O itself so
    it stays trivially testable against known inputs.

    For each scenario N: the N issuers with the highest 1yr PD are assumed
    to default (worst-case ordering, not a random draw). Portfolio return
    is the weighted-average AEY of the SURVIVING issuers' own positions
    (renormalised to their own weight, i.e. "what the rest of the portfolio
    still earns on its own merits") minus a capital-loss term of
    N * avg_issuer_weight * LGD, expressed in AEY percentage points --
    avg_issuer_weight = total portfolio weight / number of distinct issuers
    held, an explicit simplification (actual defaulted issuers' weights are
    not summed directly) matching this task's own formula spec, not a
    refinement of the existing single-issuer stress test's exact-weight
    calculation.

    E[defaults] = sum of every held issuer's own 1yr PD (Poisson lambda,
    unclamped by portfolio size -- this is the raw expectation, not a
    per-scenario figure). Scenario N is capped at the number of distinct
    issuers actually held (`n_effective`) since more defaults than issuers
    held is not a state this portfolio can reach; `capped=True` flags rows
    where the requested N exceeded that.

    break_even_n is the largest N in [0, n_issuers] at which the portfolio
    still returns at least ofz_rate_pct -- None if ofz_rate_pct wasn't
    supplied, -1 if the portfolio is already below the OFZ rate at N=0
    (never breaks even, not "breaks even at a negative number of
    defaults")."""
    lgd = default_probs.LGD if lgd is None else lgd
    all_issuers = _aggregate_positions_by_issuer(positions)
    issuers = [i for i in all_issuers if i["pd_1y"] is not None]
    excluded_unrated = len(all_issuers) - len(issuers)
    n_issuers = len(issuers)
    total_weight = sum(i["weight"] for i in issuers)
    expected_defaults = sum(i["pd_1y"] for i in issuers)
    avg_issuer_weight = (total_weight / n_issuers) if n_issuers else 0.0
    ranked = sorted(issuers, key=lambda i: i["pd_1y"], reverse=True)

    def _scenario_return(n: int) -> tuple[float, int]:
        n_eff = min(n, n_issuers)
        survivors = ranked[n_eff:]
        surv_w = sum(s["weight"] for s in survivors)
        surv_aey = (sum(s["aey_contrib"] for s in survivors) / surv_w) if surv_w > 0 else 0.0
        loss_pp = n_eff * avg_issuer_weight * lgd * 100
        return surv_aey - loss_pp, n_eff

    scenarios = []
    for n in scenario_ns:
        ret, n_eff = _scenario_return(n)
        scenarios.append({
            "n_defaults": n, "n_effective": n_eff, "capped": n > n_issuers,
            "portfolio_return_pct": ret,
            "vs_ofz_pp": (ret - ofz_rate_pct) if ofz_rate_pct is not None else None,
            "vs_cbr_pp": (ret - cbr_rate_pct) if cbr_rate_pct is not None else None,
            "probability": poisson_pmf(expected_defaults, n),
        })

    break_even_n = None
    if ofz_rate_pct is not None:
        ret0, _ = _scenario_return(0)
        if ret0 < ofz_rate_pct:
            break_even_n = -1
        else:
            break_even_n = 0
            for n in range(1, n_issuers + 1):
                ret, _ = _scenario_return(n)
                if ret >= ofz_rate_pct:
                    break_even_n = n
                else:
                    break

    return {
        "expected_defaults": expected_defaults,
        "n_issuers": n_issuers,
        "excluded_unrated": excluded_unrated,
        "avg_issuer_weight": avg_issuer_weight,
        "lgd": lgd,
        "scenarios": scenarios,
        "break_even_n": break_even_n,
        "break_even_probability": (
            poisson_pmf(expected_defaults, break_even_n)
            if break_even_n is not None and break_even_n >= 0 else None),
    }


# ============================================================================
# Portfolio presets (Portfolio presets task, 2026-08-27)
# ============================================================================
# Same rating scale as scripts/export_dashboard_data.py's RATING_TREND_VALUE
# (higher = better, ruD=1 included) -- duplicated rather than imported since
# scripts/ sits above src/hybond/ in the dependency direction (scripts import
# from hybond, never the reverse); kept in sync by inspection, not shared
# code. See test_portfolio.py for a values-match assertion against that dict.
RATING_RANK: dict[str, int] = {
    "ruAAA": 20, "ruAA+": 19, "ruAA": 18, "ruAA-": 17, "ruA+": 16, "ruA": 15, "ruA-": 14,
    "ruBBB+": 13, "ruBBB": 12, "ruBBB-": 11, "ruBB+": 10, "ruBB": 9, "ruBB-": 8,
    "ruB+": 7, "ruB": 6, "ruB-": 5, "ruCCC": 4, "ruCC": 3, "ruC": 2, "ruD": 1,
}

# NOTE on 'conservative'.min_rating: the task spec that defined this table
# gives the value 'ruBBB-' but its own inline comment says "ruBBB+ and
# above" -- those are NOT the same threshold (ruBBB- ranks two notches below
# ruBBB+). Implemented against the literal field value (rank >= ruBBB-'s 11),
# since that's the actual data driving the filter; flagging the comment
# discrepancy here rather than silently picking whichever reading seemed
# stricter. Confirm with whoever wrote the spec if 'ruBBB+' was intended.
PRESETS: dict[str, dict] = {
    "conservative": {
        "bond_count": (20, 25),
        "min_rating": "ruBBB-",
        "max_ytm": 0.30,
        "max_aey": 0.35,
        "health_allowed": ["GREEN", "YELLOW"],
        "max_per_issuer": 0.05,
        "sector_cap": 0.12,
    },
    "balanced": {
        "bond_count": (25, 30),
        "min_rating": "ruBB-",
        "max_ytm": 0.35,
        "max_aey": None,
        "health_allowed": ["GREEN", "YELLOW", "ORANGE"],
        "max_per_issuer": 0.04,
        "sector_cap": 0.15,
    },
    "aggressive": {
        "bond_count": (28, 35),
        "min_rating": "ruB-",
        "max_ytm": 0.40,
        "max_aey": None,
        "health_allowed": ["GREEN", "YELLOW", "ORANGE", "RED"],
        "max_per_issuer": 0.03,
        "sector_cap": 0.20,
    },
}

# health_allowed above is spelled UPPERCASE per the task spec, but this
# codebase's actual data (issuer.health_status, db.HEALTH_STATUSES,
# set_issuer_health_status) is lowercase throughout -- every comparison
# below lowercases both sides rather than silently requiring callers to
# match a case the rest of hybond never uses.

RED_MIN_RATING = "ruBB-"
RED_MAX_PD = 0.03            # empirical 1yr PD ceiling, Эксперт РА Table 7
RED_MAX_GSPREAD_SPIKE_BP = 200.0   # 30d G-spread widening ceiling
REFI_WINDOW_DAYS = 182             # ~6 months, refinancing soft-warning window
EPS_AEY = 1e-4                     # floor for 1/aey weighting, see compute_weights


@dataclass
class BondCandidate:
    """One bond's worth of data as seen by the preset builder. Plain
    dataclass (not a dict) so compute_weights() can use `b.isin`/`b.aey`
    attribute access, matching the task's own illustrative signature."""
    isin: str
    secid: str
    issuer_id: str
    issuer_name: str
    sector: str | None
    rating: str | None
    health: str  # lowercase: 'green' | 'yellow' | 'orange' | 'red'
    aey: float | None          # pd_adjusted_aey(), decimal (0.184 = 18.4%)
    ytm: float | None          # decimal
    g_spread_bp: float | None
    g_spread_delta_30d_bp: float | None
    mat_date: str | None
    offer_date: str | None
    flags: dict = field(default_factory=dict)


def compute_weights(bonds: list[BondCandidate], mode: str = "equal") -> dict[str, float]:
    """Position sizing across an already-selected bond list. Three modes:

    'equal'    -- 1/N each.
    'aey_high' -- weight proportional to aey (larger weight to higher AEY).
    'aey_low'  -- weight proportional to 1/aey (larger weight to lower AEY --
                  "conservative inside the preset": tilts toward the
                  already-safer names within whatever risk band the preset
                  itself selected for).

    Raises ValueError on an empty list or an unknown mode -- there is no
    sensible weight to return in either case.

    A bond with aey <= 0 (a distress-priced or otherwise pathological entry)
    is floored to EPS_AEY for the purposes of this weighting math only --
    filter thresholds (max_aey etc.) elsewhere in this module still see the
    real, unclamped aey. Without the floor, aey_low's 1/aey would blow up
    (aey=0) or flip sign (aey<0), producing a nonsensical weight rather than
    "these bonds are being treated as maximally undesirable to overweight,
    without crashing".
    """
    if not bonds:
        raise ValueError("compute_weights requires at least one bond")
    if mode == "equal":
        w = 1.0 / len(bonds)
        return {b.isin: w for b in bonds}
    if mode not in ("aey_high", "aey_low"):
        raise ValueError(f"mode must be one of 'equal'/'aey_high'/'aey_low', got {mode!r}")
    floored = [max(b.aey, EPS_AEY) if b.aey is not None else EPS_AEY for b in bonds]
    if mode == "aey_high":
        total = sum(floored)
        return {b.isin: v / total for b, v in zip(bonds, floored)}
    inv = [1.0 / v for v in floored]
    total = sum(inv)
    return {b.isin: iv / total for b, iv in zip(bonds, inv)}


def red_issuer_eligibility(rating: str | None,
                            g_spread_delta_30d_bp: float | None) -> tuple[bool, str | None]:
    """Whether a RED-health issuer may still be included (aggressive preset
    only -- health_allowed there is the only one listing 'RED'). This is an
    ADDITIONAL gate on top of the preset's own rating/YTM/AEY filters, not a
    replacement for them.

    Rule: rating >= ruBB- AND empirical 1yr PD (Table 7) <= 3% AND 30d
    G-spread delta < +200bp (not in a widening spike).

    Both a missing PD lookup and a missing G-spread-delta fail conservatively
    (excluded) EXCEPT the one fallback the task spec calls out explicitly:
    a recognized rating (has a RATING_RANK entry) with no PD table entry
    (e.g. 'ruD', which DEFAULT_PROBS_1YR carries no figure for) falls back to
    the rating floor alone. An unrecognized/missing rating always fails --
    there's no floor to even check.

    Returns (eligible, reason_string_for_flags_json). reason is None when
    not eligible.
    """
    rank = RATING_RANK.get(rating)
    min_rank = RATING_RANK[RED_MIN_RATING]
    if rank is None or rank < min_rank:
        return False, None
    probs = default_probs.lookup(rating)
    pd_1y = probs["yr1"] if probs else None
    if pd_1y is None:
        return True, f"{rating} / PD н/д / G-spread н/д"
    if pd_1y > RED_MAX_PD:
        return False, None
    if g_spread_delta_30d_bp is None or g_spread_delta_30d_bp >= RED_MAX_GSPREAD_SPIKE_BP:
        return False, None
    gspread_desc = ("G-spread stable" if g_spread_delta_30d_bp < 50
                     else f"G-spread +{g_spread_delta_30d_bp:.0f}bp")
    return True, f"{rating} / PD {pd_1y * 100:.1f}% / {gspread_desc}"


def refinancing_flag(mat_date: str | None, offer_date: str | None,
                      as_of: date | None = None,
                      window_days: int | None = None) -> tuple[bool, str | None]:
    """Soft warning only (task spec: included regardless, never a hard
    exclusion) -- true when the EARLIER of maturity/put(offer) date falls
    within `window_days` (default REFI_WINDOW_DAYS, ~6 months -- the
    Builder's own at-build-time warning) of `as_of`. Uses real wall-clock
    today() by default, not a DB snap_date: "within N days from today" is a
    genuine calendar-time question, not a data-freshness one (contrast with
    day2_snap_date elsewhere in this codebase, which deliberately never
    uses wall-clock time). monitor_portfolios() calls this with the
    shorter, more urgent MONITOR_REFI_WINDOW_DAYS instead."""
    as_of = as_of or date.today()
    window_days = window_days if window_days is not None else REFI_WINDOW_DAYS
    candidates = []
    for d in (mat_date, offer_date):
        if not d:
            continue
        try:
            candidates.append(date.fromisoformat(d))
        except ValueError:
            continue
    if not candidates:
        return False, None
    earliest = min(candidates)
    if as_of <= earliest <= as_of + timedelta(days=window_days):
        return True, earliest.isoformat()
    return False, None


def _latest_spread_row(con, secid: str):
    return con.execute(
        "SELECT snap_date, ytm, g_spread_bp, mod_dur FROM spread_snapshot "
        "WHERE secid=? ORDER BY snap_date DESC LIMIT 1", (secid,)).fetchone()


def _g_spread_delta_30d(con, secid: str, latest_date: str,
                         latest_g_spread: float | None) -> float | None:
    """Current G-spread minus the most recent snapshot available at or
    before 30 days ago -- None if either side of that comparison is
    missing. "Most recent available on/before the target date" rather than
    an exact 30-day match, since the daily pipeline doesn't guarantee a row
    exists for every calendar date."""
    if latest_g_spread is None:
        return None
    target = (date.fromisoformat(latest_date) - timedelta(days=30)).isoformat()
    row = con.execute(
        "SELECT g_spread_bp FROM spread_snapshot WHERE secid=? AND snap_date<=? "
        "ORDER BY snap_date DESC LIMIT 1", (secid, target)).fetchone()
    if row is None or row["g_spread_bp"] is None:
        return None
    return latest_g_spread - row["g_spread_bp"]


def _gather_candidates(con) -> list[BondCandidate]:
    """Every currently-listed bond of every tier=1 (active watchlist)
    issuer, with the fields the preset filters/sizing need already resolved
    -- rating filtering happens in build_preset(), not here, since an
    unrated/too-low-rated bond is still a legitimate "candidate" for this
    function's purposes (build_preset() decides what to do with it).

    Commodity-linked bonds (see COMMODITY_LINKED_BOND_IDS) are the one
    exception: excluded here, upstream of every preset's own filters, so
    build_preset() can never select one under any preset_type -- their
    coupon/redemption is indexed to a metal price rather than a fixed rate,
    which breaks the fixed-income YTM/AEY math this whole module assumes
    (confirmed live, Watchlist Filters UI task duration-distribution check:
    Селигдар's SELSILV01/SELGOLD002/SELGOLD003 had the three longest
    durations in the book alongside YTM figures of 2-8% next to ordinary
    bonds' 25-37%, an artifact of their non-standard structure, not a real
    yield signal)."""
    rate_scenarios = get_rate_scenarios(con)
    delta_rate = rate_scenarios["base"]
    candidates: list[BondCandidate] = []
    for issuer in db.list_issuers_by_tier(con, 1):
        issuer_id = issuer["issuer_id"]
        issuer_row = db.get_issuer(con, issuer_id)
        # list_issuers_by_tier() only filters on watchlist_tier, not active
        # -- a defensive check, scoped to this function rather than that
        # shared helper (other callers may legitimately want inactive
        # tier=1 rows too). No-op against the real DB today (all 34 tier=1
        # issuers have active=1), but an issuer demoted to inactive while
        # still tagged tier=1 must never be preset-eligible.
        if issuer_row is not None and not issuer_row.get("active", 1):
            continue
        health = (issuer_row.get("health_status") if issuer_row else None) or "green"
        rating = db.current_rating(con, issuer_id)
        for secid in db.list_bonds_for_issuer(con, issuer_id):
            bond_row = con.execute(
                "SELECT isin, mat_date, short_name FROM bond WHERE secid=?", (secid,)).fetchone()
            if bond_row is None or not bond_row["isin"]:
                continue
            if _is_commodity_linked(secid, bond_row["short_name"]):
                logger.info(
                    "build_preset: excluding commodity-linked bond %s (%s), issuer=%s",
                    secid, bond_row["short_name"], issuer_id)
                continue
            # universe_snapshot (offer/put date) is a nice-to-have, not a
            # hard dependency of this function -- a caller who never ran
            # `hybond universe` (so init_universe() was never called) still
            # gets a working build_preset(), just without offer-date
            # refinancing-risk detection (mat_date alone still works).
            try:
                offer_row = con.execute(
                    "SELECT offer_date FROM universe_snapshot WHERE secid=? "
                    "ORDER BY snap_date DESC LIMIT 1", (secid,)).fetchone()
                offer_date = offer_row["offer_date"] if offer_row else None
            except sqlite3.OperationalError:
                offer_date = None
            spread_row = _latest_spread_row(con, secid)
            ytm = spread_row["ytm"] if spread_row else None
            mod_dur = spread_row["mod_dur"] if spread_row else None
            g_spread_bp = spread_row["g_spread_bp"] if spread_row else None
            g_spread_delta = (
                _g_spread_delta_30d(con, secid, spread_row["snap_date"], g_spread_bp)
                if spread_row else None)
            aey_base = bond_aey(ytm, mod_dur, delta_rate)
            aey = pd_adjusted_aey(aey_base, rating)
            candidates.append(BondCandidate(
                isin=bond_row["isin"], secid=secid, issuer_id=issuer_id,
                issuer_name=issuer_row.get("name", issuer_id) if issuer_row else issuer_id,
                sector=issuer_row.get("sector") if issuer_row else None,
                rating=rating, health=str(health).lower(), aey=aey, ytm=ytm,
                g_spread_bp=g_spread_bp, g_spread_delta_30d_bp=g_spread_delta,
                mat_date=bond_row["mat_date"], offer_date=offer_date,
            ))
    return candidates


def build_preset(preset_type: str, sizing_mode: str = "equal",
                  user_filters: dict | None = None, db=None,
                  dry_run: bool = False) -> dict:
    """Build a portfolio preset from tier=1 watchlist bonds.

    Args:
        preset_type: 'conservative' | 'balanced' | 'aggressive'
        sizing_mode: 'equal' | 'aey_high' | 'aey_low'
        user_filters: dict overriding preset defaults
                      (sector_cap, max_per_issuer, health_allowed)
        db: DB connection (sqlite3.Connection, e.g. from store.db.connect())
        dry_run: if True, never raises -- instead returns
            {"success": bool, "bond_count": int, "reason": str|None}, where
            "reason" is the ValueError message on failure (unknown
            preset_type/sizing_mode, no db connection, or fewer than
            bond_count[0] bonds surviving every filter) and None on
            success. Nothing about a normal (non-dry-run) call writes to
            the DB either way -- build_preset() has always been a pure
            computation, save_portfolio() is the only writer in this
            module -- dry_run exists purely as a cheap satisfiability
            check the Builder UI can call on every filter change (Portfolio
            Presets Builder pre-build validation, 2026-08-27 fix: replaces
            an earlier client-side formula check -- eligible_bond_count *
            max_per_issuer >= min_count -- that produced false negatives by
            conflating bond count with issuer count).

    Returns:
        dict with bonds, weights, flags, summary metrics:
        {"preset_type", "sizing_mode", "filters", "bonds": [BondCandidate,...],
         "weights": {isin: weight}, "summary": {avg_aey, expected_loss,
         net_return, n_issuers, n_sectors, n_red_included}}
        (or the dry_run shape above, if dry_run=True)

    Raises:
        ValueError: unknown preset_type/sizing_mode, no db connection, or
        fewer than bond_count[0] bonds pass every filter (including the
        issuer/sector cap enforcement pass). Never raised when dry_run=True.
    """
    if dry_run:
        try:
            built = build_preset(preset_type, sizing_mode=sizing_mode,
                                  user_filters=user_filters, db=db)
            return {"success": True, "bond_count": built["summary"]["n_bonds"], "reason": None}
        except ValueError as e:
            return {"success": False, "bond_count": 0, "reason": str(e)}

    if preset_type not in PRESETS:
        raise ValueError(f"preset_type must be one of {sorted(PRESETS)}, got {preset_type!r}")
    if db is None:
        raise ValueError("db connection (db=...) is required")
    con = db

    preset = dict(PRESETS[preset_type])
    if user_filters:
        for key in ("sector_cap", "max_per_issuer", "health_allowed"):
            if user_filters.get(key) is not None:
                preset[key] = user_filters[key]

    min_rank = RATING_RANK.get(preset["min_rating"])
    health_allowed = {str(h).lower() for h in preset["health_allowed"]}
    min_count, max_count = preset["bond_count"]

    all_candidates = _gather_candidates(con)
    eligible: list[BondCandidate] = []
    for c in all_candidates:
        rank = RATING_RANK.get(c.rating)
        if rank is None or rank < min_rank:
            continue
        if c.ytm is None or c.ytm > preset["max_ytm"]:
            continue
        if c.aey is None:
            continue
        if preset["max_aey"] is not None and c.aey > preset["max_aey"]:
            continue
        if c.health not in health_allowed:
            continue
        if c.health == "red":
            red_ok, reason = red_issuer_eligibility(c.rating, c.g_spread_delta_30d_bp)
            if not red_ok:
                continue
            c.flags["red_included"] = True
            c.flags["red_reason"] = reason
        refi_ok, refi_date = refinancing_flag(c.mat_date, c.offer_date)
        if refi_ok:
            c.flags["refi_flag"] = True
            c.flags["refi_date"] = refi_date
        eligible.append(c)

    if len(eligible) < min_count:
        raise ValueError(
            f"only {len(eligible)} bonds passed {preset_type} filters, "
            f"need at least {min_count}")

    eligible.sort(key=lambda c: c.aey, reverse=True)
    target_n = min(max_count, len(eligible))

    # Slot-based approximation of the weight caps (max_per_issuer/
    # sector_cap are fractions of total portfolio weight, but weight isn't
    # known until sizing runs, which itself depends on which bonds were
    # selected -- a circular dependency). Converts each fraction to a bond
    # COUNT cap using the target size, assuming near-equal weight during
    # selection; the actual weight-based caps are re-verified (and
    # enforced by trimming) after real sizing runs, below, so this only
    # needs to be a reasonable starting point, not exact.
    max_per_issuer_slots = max(1, math.floor(preset["max_per_issuer"] * target_n))
    max_per_sector_slots = max(1, math.floor(preset["sector_cap"] * target_n))

    def _select(issuer_cap: int, sector_cap: int) -> list[BondCandidate]:
        sel: list[BondCandidate] = []
        issuer_counts: dict[str, int] = {}
        sector_counts: dict[str, int] = {}
        for c in eligible:
            if len(sel) >= target_n:
                break
            if issuer_counts.get(c.issuer_id, 0) >= issuer_cap:
                continue
            if c.sector and sector_counts.get(c.sector, 0) >= sector_cap:
                continue
            sel.append(c)
            issuer_counts[c.issuer_id] = issuer_counts.get(c.issuer_id, 0) + 1
            if c.sector:
                sector_counts[c.sector] = sector_counts.get(c.sector, 0) + 1
        return sel

    selected = _select(max_per_issuer_slots, max_per_sector_slots)
    # Slot caps too tight to reach even the floor (small/concentrated
    # watchlist): relax BOTH caps by the same +1 step and retry the whole
    # selection from scratch, rather than a one-shot "ignore every cap"
    # fallback. That one-shot fallback was tried first and had a real bug --
    # dumping in whatever's left over regardless of cap could push a single
    # issuer/sector's weight far past its cap in one step, landing exactly
    # at min_count with no bonds left to trim (the enforcement loop below
    # only trims when len(selected) > min_count) -- an unrecoverable
    # ValueError on a scenario that a more gradual relaxation resolves
    # cleanly. Bounded by len(eligible): once both caps reach that, every
    # candidate is admissible and selection can only fail to reach min_count
    # if len(eligible) itself is under min_count, already ruled out above.
    while len(selected) < min_count and (
            max_per_issuer_slots < len(eligible) or max_per_sector_slots < len(eligible)):
        max_per_issuer_slots += 1
        max_per_sector_slots += 1
        selected = _select(max_per_issuer_slots, max_per_sector_slots)

    if len(selected) < min_count:
        raise ValueError(
            f"only {len(selected)} bonds available for {preset_type} "
            f"after issuer/sector caps, need at least {min_count}")

    weights = compute_weights(selected, sizing_mode)

    # Real, weight-based cap enforcement -- sizing_mode (esp. aey_high/
    # aey_low) can concentrate weight beyond what the slot-count
    # approximation above anticipated. Drop the lowest-aey offending bond
    # and re-size, repeating until compliant or until dropping further
    # would breach bond_count's floor.
    for _ in range(len(selected)):
        by_issuer: dict[str, float] = {}
        by_sector: dict[str, float] = {}
        for c in selected:
            by_issuer[c.issuer_id] = by_issuer.get(c.issuer_id, 0.0) + weights[c.isin]
            if c.sector:
                by_sector[c.sector] = by_sector.get(c.sector, 0.0) + weights[c.isin]
        offending_issuer = next((i for i, w in by_issuer.items() if w > preset["max_per_issuer"] + 1e-9), None)
        offending_sector = next((s for s, w in by_sector.items() if w > preset["sector_cap"] + 1e-9), None)
        if offending_issuer is None and offending_sector is None:
            break
        if len(selected) <= min_count:
            raise ValueError(
                f"cannot satisfy {preset_type}'s issuer/sector caps without "
                f"dropping below the {min_count}-bond minimum")
        if offending_issuer is not None:
            group = [c for c in selected if c.issuer_id == offending_issuer]
        else:
            group = [c for c in selected if c.sector == offending_sector]
        worst = min(group, key=lambda c: c.aey)
        selected = [c for c in selected if c.isin != worst.isin]
        weights = compute_weights(selected, sizing_mode)

    n_issuers = len({c.issuer_id for c in selected})
    n_sectors = len({c.sector for c in selected if c.sector})
    n_red_included = sum(1 for c in selected if c.flags.get("red_included"))
    avg_aey = sum(weights[c.isin] * c.aey for c in selected)
    expected_loss = 0.0
    for c in selected:
        probs = default_probs.lookup(c.rating)
        pd_1y = probs["yr1"] if probs else 0.0
        expected_loss += weights[c.isin] * pd_1y * default_probs.LGD
    net_return = avg_aey - expected_loss

    return {
        "preset_type": preset_type,
        "sizing_mode": sizing_mode,
        "filters": preset,
        "bonds": selected,
        "weights": weights,
        "summary": {
            "avg_aey": avg_aey,
            "expected_loss": expected_loss,
            "net_return": net_return,
            "n_bonds": len(selected),
            "n_issuers": n_issuers,
            "n_sectors": n_sectors,
            "n_red_included": n_red_included,
        },
    }


def save_portfolio(con, name: str, built: dict, snapshot_date: str | None = None) -> int:
    """Persist a build_preset() result: writes `portfolio` (status='active')
    + one `portfolio_bond` row per bond (flags_json includes whatever
    red_included/refi_flag entries build_preset() set, plus a G-spread
    monitoring baseline recorded here at save time) + a first
    `portfolio_snapshot` row. Returns the new portfolio_id.

    The daily-pipeline comparison of this baseline against later G-spread
    readings (gspread_delta_30d/gspread_alert) is Phase B (Monitor screen)
    -- out of scope here, this only records the baseline itself.
    """
    db.init_portfolio(con)
    snapshot_date = snapshot_date or date.today().isoformat()
    portfolio_id = db.create_portfolio(
        con, name=name, preset_type=built["preset_type"],
        sizing_mode=built["sizing_mode"],
        filters_json=json.dumps(built["filters"]), status="active")
    for c in built["bonds"]:
        flags = dict(c.flags)
        flags["gspread_baseline_bp"] = c.g_spread_bp
        flags["gspread_baseline_date"] = snapshot_date
        # health_at_save (Portfolio Monitoring task, 2026-08-27):
        # monitor_portfolios()'s health-degradation check needs to know
        # what health status this bond's issuer had AT SAVE TIME, same as
        # the dashboard's client-side Monitor stores in its own localStorage
        # record -- BondCandidate.health isn't otherwise persisted anywhere.
        flags["health_at_save"] = c.health
        db.add_portfolio_bond(
            con, portfolio_id=portfolio_id, isin=c.isin,
            weight=built["weights"][c.isin], flags_json=json.dumps(flags))
    summary = built["summary"]
    db.add_portfolio_snapshot(
        con, portfolio_id=portfolio_id, snapshot_date=snapshot_date,
        aey=summary["avg_aey"], expected_loss=summary["expected_loss"],
        metrics_json=json.dumps(summary))
    con.commit()
    return portfolio_id


# --- Phase B: daily portfolio monitoring -----------------------------------

FLAG_TYPE_GSPREAD = "portfolio_gspread_alert"
FLAG_TYPE_HEALTH = "portfolio_health_change"
FLAG_TYPE_REFI = "portfolio_refi_warning"
FLAG_TYPE_RED = "portfolio_red_change"

# Same threshold/window the dashboard's client-side Monitor screen (Пресеты
# subtab, Phase B) uses -- mirrored, not shared, same convention as every
# other Python/JS pair in this module (see gatherCandidates()'s header
# comment in dashboard/template.html).
MONITOR_GSPREAD_ALERT_BP = 150.0
MONITOR_REFI_WINDOW_DAYS = 45
HEALTH_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}


def monitor_portfolios(con: sqlite3.Connection) -> dict[int, list[dict]]:
    """Daily pipeline step. For each active portfolio in DB:
    - Compare current G-spread vs baseline per bond
    - Detect health status changes
    - Check refinancing windows (<=45 days)
    - Write alerts to flag_log table
    - Take a new portfolio_snapshot with current metrics

    Alert conditions (per bond, all independent -- a bond can trigger more
    than one in the same run): G-spread delta over the saved baseline
    exceeding MONITOR_GSPREAD_ALERT_BP (+150bp) -> FLAG_TYPE_GSPREAD;
    current health status ranked worse than at save time (HEALTH_RANK) ->
    FLAG_TYPE_HEALTH; maturity/put date within MONITOR_REFI_WINDOW_DAYS
    (45 days) -> FLAG_TYPE_REFI; health specifically becoming 'red' from a
    non-red state at save time -> FLAG_TYPE_RED (fires alongside
    FLAG_TYPE_HEALTH for that same transition, not instead of it -- the two
    are separate signals in flag_log by design, matching the task spec's
    four distinct flag_type values).

    A bond whose isin no longer resolves to a `bond` row (e.g. delisted
    since save) is skipped for that portfolio rather than raising -- this
    is a scheduled pipeline step, one stale bond must not block every other
    portfolio/bond from being monitored.

    Args:
        con: DB connection

    Returns:
        dict mapping portfolio_id -> list of alert dicts generated for it
        this run (each: issuer_id, flag_type, severity, detail dict).
    """
    db.init_portfolio(con)
    today = date.today().isoformat()
    delta_rate = get_rate_scenarios(con)["base"]
    alerts_by_portfolio: dict[int, list[dict]] = {}

    for p in db.list_portfolios(con, status="active"):
        portfolio_id = p["id"]
        alerts: list[dict] = []
        total_aey = 0.0
        total_expected_loss = 0.0
        for pb in db.list_portfolio_bonds(con, portfolio_id):
            isin = pb["isin"]
            weight = pb["weight"] or 0.0
            flags = json.loads(pb["flags_json"] or "{}")
            bond_row = con.execute(
                "SELECT secid, issuer_id, mat_date FROM bond WHERE isin=?",
                (isin,)).fetchone()
            if bond_row is None:
                continue
            secid, issuer_id, mat_date = (
                bond_row["secid"], bond_row["issuer_id"], bond_row["mat_date"])
            issuer_row = db.get_issuer(con, issuer_id)
            current_health = ((issuer_row.get("health_status") if issuer_row else None)
                               or "green").lower()
            saved_health = (flags.get("health_at_save") or "green").lower()
            rating = db.current_rating(con, issuer_id)

            spread_row = _latest_spread_row(con, secid)
            current_g_spread = spread_row["g_spread_bp"] if spread_row else None
            baseline_bp = flags.get("gspread_baseline_bp")
            gspread_delta = (
                current_g_spread - baseline_bp
                if current_g_spread is not None and baseline_bp is not None else None)

            aey = None
            if spread_row is not None:
                aey_base = bond_aey(spread_row["ytm"], spread_row["mod_dur"], delta_rate)
                aey = pd_adjusted_aey(aey_base, rating)
            if aey is not None:
                total_aey += weight * aey
            probs = default_probs.lookup(rating) if rating else None
            pd_1y = probs["yr1"] if probs else 0.0
            total_expected_loss += weight * pd_1y * default_probs.LGD

            if gspread_delta is not None and gspread_delta > MONITOR_GSPREAD_ALERT_BP:
                alerts.append({
                    "issuer_id": issuer_id, "flag_type": FLAG_TYPE_GSPREAD, "severity": "high",
                    "detail": {"portfolio_id": portfolio_id, "isin": isin, "secid": secid,
                               "baseline_bp": baseline_bp, "current_bp": current_g_spread,
                               "delta_bp": gspread_delta}})

            if HEALTH_RANK.get(current_health, 0) > HEALTH_RANK.get(saved_health, 0):
                alerts.append({
                    "issuer_id": issuer_id, "flag_type": FLAG_TYPE_HEALTH, "severity": "medium",
                    "detail": {"portfolio_id": portfolio_id, "isin": isin,
                               "health_before": saved_health, "health_after": current_health}})

            refi_flag, refi_date = refinancing_flag(
                mat_date, None, window_days=MONITOR_REFI_WINDOW_DAYS)
            if refi_flag:
                alerts.append({
                    "issuer_id": issuer_id, "flag_type": FLAG_TYPE_REFI, "severity": "low",
                    "detail": {"portfolio_id": portfolio_id, "isin": isin,
                               "refi_date": refi_date, "window_days": MONITOR_REFI_WINDOW_DAYS}})

            if saved_health != "red" and current_health == "red":
                alerts.append({
                    "issuer_id": issuer_id, "flag_type": FLAG_TYPE_RED, "severity": "high",
                    "detail": {"portfolio_id": portfolio_id, "isin": isin,
                               "health_before": saved_health}})

        for a in alerts:
            db.insert_flag(
                con, snap_date=today, secid=None, issuer_id=a["issuer_id"],
                detector="portfolio_monitor", signal=a["flag_type"],
                metric=None, detail=json.dumps(a["detail"]), driving=0,
                severity=a["severity"])

        db.add_portfolio_snapshot(
            con, portfolio_id=portfolio_id, snapshot_date=today,
            aey=total_aey, expected_loss=total_expected_loss,
            metrics_json=json.dumps({
                "avg_aey": total_aey, "expected_loss": total_expected_loss,
                "net_return": total_aey - total_expected_loss,
                "n_alerts": len(alerts)}))

        alerts_by_portfolio[portfolio_id] = alerts

    con.commit()
    return alerts_by_portfolio
