"""OFZ carry + roll-down projection. Pure functions, no I/O.

Methodology reused from a standalone HTML prototype's carry/roll-down
approach (coupon return + duration/convexity-based body reevaluation, at
1yr/2yr/3yr horizons with a graduated yield-shift schedule) -- the
prototype's own bond dataset was fabricated/unverified and is NOT used
anywhere in this codebase; only the formula and horizon mechanism are
reused, applied to real MOEX data (ofz_pipeline.py) and real CBR scenario
deltas (portfolio.get_rate_scenarios).

No PD/default layer here -- correct for sovereign (OFZ) risk, unlike the
corporate AEY layer in portfolio.py, which is deliberately PD-weighted.

Two components, matching the prototype's "coupon + body" split:

  coupon_return(H) = (annual_coupon / dirty_price) * H
      Cash coupon income over the holding period, simple (not reinvested).

  body_reeval(modDur, convexity, deltaY) = -modDur * deltaY + 0.5 * convexity * deltaY**2
      Price change from a yield shift, via the standard duration/convexity
      Taylor approximation. `convexity` may be None (fixed-schedule bonds
      only get a real convexity number from ofz_pipeline.py; floaters/
      linkers fall back to a MOEX-yield proxy with no convexity) -- treated
      as 0 in that case, i.e. duration-only approximation.

Graduated shift: the market does not reprice a multi-year CBR path
instantly. Applying GRADUATED_SHIFT[horizon] to whatever "eventual total"
delta the caller supplies (get_rate_scenarios()'s base/stress/bull, used
flatly elsewhere in this codebase too, e.g. portfolio.bond_aey) models most
of the repricing happening in year 1, tapering by year 3 -- the same
40%/75%/100% schedule as the prototype, not the prototype's specific
hardcoded delta magnitudes (those came from a scenario the prototype
invented; this module takes the delta as a real parameter instead).
"""
from __future__ import annotations

HORIZONS_YEARS = (1, 2, 3)

# Fraction of the "eventual total" yield shift assumed realized by each
# horizon -- front-loaded, tapering by year 3 (see module docstring).
GRADUATED_SHIFT: dict[int, float] = {1: 0.40, 2: 0.75, 3: 1.00}


def coupon_return_pct(annual_coupon_pct: float, dirty_price_pct: float, years: float) -> float:
    """Cash coupon return over `years`, as a percent of dirty price.

    annual_coupon_pct: annual coupon amount as a percent of face (e.g. 12.0
    for a 12%-coupon bond). dirty_price_pct: dirty price as a percent of
    face (MOEX convention). Both must be on the same face-percent basis so
    the ratio is dimensionless before scaling by years."""
    if dirty_price_pct <= 0:
        raise ValueError("dirty_price_pct must be positive")
    return (annual_coupon_pct / dirty_price_pct) * years * 100.0


def body_reeval_pct(mod_dur: float, convexity: float | None, delta_y: float) -> float:
    """Price change (%) from a yield shift `delta_y` (decimal, e.g. -0.01
    for -100bp), via the duration/convexity Taylor approximation.
    `convexity` may be None (proxy-yield bonds with no real convexity
    computed) -- treated as 0, i.e. duration-only."""
    cvx = convexity or 0.0
    return (-mod_dur * delta_y + 0.5 * cvx * delta_y * delta_y) * 100.0


def project_horizon(
    *, annual_coupon_pct: float, dirty_price_pct: float,
    mod_dur: float, convexity: float | None,
    total_delta_y: float, horizon_years: int,
) -> dict:
    """Carry + roll-down projection for one bond at one horizon, under one
    scenario's total (eventual) yield-shift assumption. Returns the two
    components plus their sum, all in percent."""
    if horizon_years not in GRADUATED_SHIFT:
        raise ValueError(f"horizon_years must be one of {sorted(GRADUATED_SHIFT)}")
    shift_frac = GRADUATED_SHIFT[horizon_years]
    delta_y_h = total_delta_y * shift_frac
    coupon = coupon_return_pct(annual_coupon_pct, dirty_price_pct, horizon_years)
    body = body_reeval_pct(mod_dur, convexity, delta_y_h)
    return {
        "horizon_years": horizon_years,
        "shift_frac": shift_frac,
        "delta_y_applied": delta_y_h,
        "coupon_return_pct": round(coupon, 3),
        "body_reeval_pct": round(body, 3),
        "total_return_pct": round(coupon + body, 3),
    }


def project_custom_scenario(
    *, annual_coupon_pct: float, dirty_price_pct: float,
    mod_dur: float, convexity: float | None,
    delta_y: float, horizon_years: float,
) -> dict:
    """Carry + roll-down to a USER-specified target rate/date (the OFZ tab's
    custom-scenario input), not one of the fixed 1/2/3yr CBR base/stress/
    bull horizons project_horizon() handles.

    No GRADUATED_SHIFT here: that schedule models the market absorbing a
    multi-year CBR path gradually. A custom scenario is the opposite kind of
    input -- "assume the rate is at this level BY this date" -- so the full
    delta is applied at the user's own horizon rather than tapered in.
    horizon_years may be fractional and is not restricted to {1, 2, 3}."""
    if horizon_years <= 0:
        raise ValueError("horizon_years must be positive")
    coupon = coupon_return_pct(annual_coupon_pct, dirty_price_pct, horizon_years)
    body = body_reeval_pct(mod_dur, convexity, delta_y)
    return {
        "horizon_years": round(horizon_years, 3),
        "delta_y_applied": delta_y,
        "coupon_return_pct": round(coupon, 3),
        "body_reeval_pct": round(body, 3),
        "total_return_pct": round(coupon + body, 3),
    }


def project_all_horizons(
    *, annual_coupon_pct: float, dirty_price_pct: float,
    mod_dur: float, convexity: float | None, total_delta_y: float,
) -> list[dict]:
    """project_horizon() for every horizon in HORIZONS_YEARS, under one
    scenario's total delta."""
    return [
        project_horizon(
            annual_coupon_pct=annual_coupon_pct, dirty_price_pct=dirty_price_pct,
            mod_dur=mod_dur, convexity=convexity, total_delta_y=total_delta_y,
            horizon_years=h,
        )
        for h in HORIZONS_YEARS
    ]
