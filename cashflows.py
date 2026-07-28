"""Build a bond's remaining cash-flow schedule from MOEX coupon + amortization
data. Pure, no I/O.

MOEX ISS exposes coupons via /iss/securities/<SECID>/bondization (columns
coupondate, value, ...) and amortizations similarly. We normalise both into a
single list of dated cash flows, then hand them to bondmath as (t, amount).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CashFlow:
    when: date
    amount: float  # currency units on face (e.g. RUB per 1000 face)
    kind: str      # "coupon" | "amortization" | "redemption"


def build_cashflows(
    coupons: list[tuple[date, float]],
    amortizations: list[tuple[date, float]],
    settlement: date,
    face_at_settlement: float,
) -> list[CashFlow]:
    """Merge coupon and principal flows occurring strictly after settlement.

    `amortizations` are partial principal repayments (date, amount). If empty,
    a single redemption of `face_at_settlement` at the last coupon date is
    assumed by the caller-provided maturity flow (pass it in amortizations as
    the final entry). We keep this explicit rather than guessing.
    """
    flows: list[CashFlow] = []
    for d, v in coupons:
        if d > settlement and v is not None:
            flows.append(CashFlow(d, float(v), "coupon"))
    for d, v in amortizations:
        if d > settlement and v is not None:
            flows.append(CashFlow(d, float(v), "amortization"))
    flows.sort(key=lambda f: f.when)
    return flows


def to_pairs(flows: list[CashFlow], settlement: date) -> list[tuple[float, float]]:
    """Collapse to (year_fraction, amount) pairs for bondmath, ACT/365F."""
    return [((f.when - settlement).days / 365.0, f.amount) for f in flows]


def next_coupon(flows: list[CashFlow]) -> CashFlow | None:
    coupons = [f for f in flows if f.kind == "coupon"]
    return coupons[0] if coupons else None


def final_maturity(flows: list[CashFlow]) -> date | None:
    return flows[-1].when if flows else None
