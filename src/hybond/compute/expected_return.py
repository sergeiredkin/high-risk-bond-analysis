"""Multi-horizon expected-return projection: combines a bond's REAL dated
cash-flow schedule (cashflows.py, built from MOEX bondization data -- never
estimated) with the PD/recovery model (default_probs.py) to produce a
survival-weighted expected return, comparable in scale to the existing AEY
figures (portfolio.pd_adjusted_aey / aey_risk_decomposition).

This extends that single-period model to multiple periods: instead of one
blended (1-PD)*aey - PD*(1-recovery) figure computed for a flat 1-year
horizon, this walks the bond's actual coupon/amortization/redemption dates
and weights each by survival probability to that date (default_probs.
survival_probability), adding an expected-recovery cash flow for the
probability of defaulting within each calendar year. The result is IRR-solved
against the bond's current dirty price using the same bisection solver
bondmath.py already uses for YTM -- an expected-return IRR is exactly a YTM
computed on probability-weighted cash flows instead of contractual ones.

Scope: watchlist bonds only. Building this needs the bond's real cash-flow
schedule (sources.fetch_bondization), which universe_pipeline.py deliberately
does not fetch at full-universe scale (documented cost tradeoff) -- so this
module is only usable where that schedule is already being pulled (Slice 1,
hybond run watchlist.json).

IMPORTANT -- horizons beyond 3 years lean on default_probs.survival_
probability()'s constant-hazard extrapolation past its real (agency-sourced)
3-year range. Any output here with uses_extrapolated_pd=True must be labeled
as an approximation wherever it's shown, not presented as agency data.
"""
from __future__ import annotations

from datetime import date

from . import bondmath, cashflows
from .default_probs import (RECOVERY_RATE, YEAR_BOUNDARY_TOL, lookup,
                             survival_probability)


def project_expected_return(
    coupons: list[tuple[date, float]],
    amortizations: list[tuple[date, float]],
    settlement: date,
    face: float,
    dirty_price: float,
    rating: str | None,
    horizon_yr: float | None = None,
) -> dict | None:
    """Survival-weighted expected-return projection for one bond.

    horizon_yr: None means "to maturity" (uses the bond's own remaining
    life). A numeric horizon shorter than the bond's remaining life adds a
    mark-to-market exit assumption (survival-weighted sale at today's dirty
    price, flat -- no future re-rating is modelled, since projecting a future
    price would be inventing a number this codebase has consistently avoided
    inventing). A numeric horizon longer than or equal to the bond's
    remaining life is silently capped at that remaining life -- there is no
    bond left to hold past its own maturity/redemption.

    Returns None if the rating is unrecognised or the bond has no usable
    cash-flow schedule (e.g. a floater with unset future coupons -- same
    exclusion pipeline.py already applies before calling this)."""
    if not rating or lookup(rating) is None:
        return None
    flows = cashflows.build_cashflows(coupons, amortizations, settlement, face)
    if not flows:
        return None

    final_mat = cashflows.final_maturity(flows)
    full_tenor = (final_mat - settlement).days / 365.0
    if full_tenor <= 0:
        return None

    needs_exit_flow = horizon_yr is not None and horizon_yr < full_tenor
    effective_horizon = min(horizon_yr, full_tenor) if horizon_yr is not None else full_tenor

    def outstanding_face_at(t: float) -> float:
        paid = sum(f.amount for f in flows
                   if f.kind == "amortization" and (f.when - settlement).days / 365.0 <= t + 1e-9)
        return max(face - paid, 0.0)

    pairs: list[tuple[float, float]] = []

    # 1. Real scheduled coupon/amortization/redemption flows within the
    # horizon, each weighted by survival probability to its own date.
    for f in flows:
        t = (f.when - settlement).days / 365.0
        if t > effective_horizon + 1e-9:
            continue
        s = survival_probability(rating, t)
        pairs.append((t, f.amount * s))

    # 2. Expected default-recovery cash flow, one per calendar year within
    # the horizon: incremental default probability in that year * recovery
    # rate * outstanding face at the start of that year, placed at the
    # year's end as a simplifying approximation of default timing.
    year = 0
    while year < effective_horizon - 1e-9:
        year_start = float(year)
        year_end = min(float(year + 1), effective_horizon)
        s_start = survival_probability(rating, year_start)
        s_end = survival_probability(rating, year_end)
        incr_default_p = max(s_start - s_end, 0.0)
        if incr_default_p > 0:
            of = outstanding_face_at(year_start)
            pairs.append((year_end, incr_default_p * RECOVERY_RATE * of))
        year += 1

    # 3. Mark-to-market exit flow if the horizon truncates before maturity.
    if needs_exit_flow:
        s_h = survival_probability(rating, effective_horizon)
        pairs.append((effective_horizon, s_h * dirty_price))

    if not pairs:
        return None

    try:
        irr = bondmath.ytm_from_price(dirty_price, pairs)
    except (ValueError, ZeroDivisionError):
        return None

    undiscounted_total = sum(amt for _, amt in pairs)
    return {
        "horizon_yr": round(effective_horizon, 3),
        "horizon_capped_by_maturity": (horizon_yr is not None
                                        and horizon_yr > full_tenor),
        "uses_extrapolated_pd": effective_horizon > 3.0 + YEAR_BOUNDARY_TOL,
        "irr_pct": round(irr * 100, 2),
        "undiscounted_npv": round(undiscounted_total - dirty_price, 2),
        "undiscounted_total_cashflow": round(undiscounted_total, 2),
    }
