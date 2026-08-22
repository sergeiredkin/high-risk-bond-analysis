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

import math

from .cbr_tracker import _midpoint
from .compute import default_probs
from .store import db

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
