"""Pure bond-math functions. No I/O, no network, no LLM.

Every function is a pure function of its inputs and is unit-tested against
textbook cases. This is the layer that must never be delegated to a model.

Conventions:
- Rates and yields are decimals (0.14 == 14%).
- Cash flows are (t_years, amount) pairs measured from the settlement date.
- Prices are in currency units on the same face as the cash flows (typically
  face = 1000 RUB for MOEX bonds). MOEX quotes clean price as % of face; convert
  before calling (price_rub = clean_pct/100 * face).
"""
from __future__ import annotations

from datetime import date
from typing import Sequence


def year_fractions(settlement: date, dates: Sequence[date]) -> list[float]:
    """ACT/365F year fractions from settlement to each cash-flow date.

    ACT/365F is a pragmatic default for screening; it is not the exact MOEX
    day-count for every instrument, but is consistent and adequate for
    relative G-spread comparison. Document this choice; revisit if a bond's
    convention materially differs.
    """
    return [(d - settlement).days / 365.0 for d in dates]


def price_from_ytm(ytm: float, cashflows: Sequence[tuple[float, float]]) -> float:
    """Dirty price implied by a flat annualised yield."""
    return sum(cf / (1.0 + ytm) ** t for t, cf in cashflows)


def ytm_from_price(
    price: float,
    cashflows: Sequence[tuple[float, float]],
    lo: float = -0.5,
    hi: float = 5.0,
    iterations: int = 200,
) -> float:
    """Solve for yield-to-maturity by bisection.

    Bisection over a wide bracket is deliberately chosen over Newton: it cannot
    diverge, handles deep-discount ВДО prices, and 200 iterations gives machine
    precision. `price` must be the dirty price (clean + accrued) in the same
    units as the cash flows.
    """
    if not cashflows:
        raise ValueError("no cashflows")
    if price <= 0:
        raise ValueError("price must be positive")
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        p = price_from_ytm(mid, cashflows)
        if p > price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def macaulay_duration(
    ytm: float, cashflows: Sequence[tuple[float, float]]
) -> float:
    p = price_from_ytm(ytm, cashflows)
    if p == 0:
        raise ValueError("zero price")
    return sum(t * cf / (1.0 + ytm) ** t for t, cf in cashflows) / p


def modified_duration(
    ytm: float, cashflows: Sequence[tuple[float, float]]
) -> float:
    return macaulay_duration(ytm, cashflows) / (1.0 + ytm)


def convexity(ytm: float, cashflows: Sequence[tuple[float, float]]) -> float:
    p = price_from_ytm(ytm, cashflows)
    if p == 0:
        raise ValueError("zero price")
    return (
        sum(t * (t + 1) * cf / (1.0 + ytm) ** (t + 2) for t, cf in cashflows) / p
    )


def interp_curve(tenor_years: float, curve: Sequence[tuple[float, float]]) -> float:
    """Linear interpolation of a zero curve given as sorted (tenor, rate) points.

    The CBR G-curve is published as spot rates at fixed tenors. Flat
    extrapolation beyond the endpoints (clamp) is intentional: we do not want a
    screening tool inventing yields past the observable curve.
    """
    pts = sorted(curve)
    if not pts:
        raise ValueError("empty curve")
    if tenor_years <= pts[0][0]:
        return pts[0][1]
    if tenor_years >= pts[-1][0]:
        return pts[-1][1]
    for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
        if t0 <= tenor_years <= t1:
            w = (tenor_years - t0) / (t1 - t0)
            return r0 + w * (r1 - r0)
    return pts[-1][1]  # unreachable, defensive


def g_spread(bond_ytm: float, tenor_years: float, curve: Sequence[tuple[float, float]]) -> float:
    """G-spread in decimal: bond YTM minus the interpolated risk-free at the
    bond's tenor. Multiply by 1e4 for basis points. This is the workhorse
    relative-value metric for Russian HY."""
    return bond_ytm - interp_curve(tenor_years, curve)
