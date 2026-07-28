"""Maturity wall / refinancing calendar. Pure aggregation over the bond table
and cash-flow schedules. No financials, no LLM. This is Slice 1's first
publishable artifact.

Given a set of bonds (each with issuer, face outstanding, maturity, and the
next 12–24 months of coupon+amort flows), produce:
  - per-month principal + coupon outflow across the watchlist
  - per-issuer upcoming redemption dates and amounts
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date


@dataclass
class WallBucket:
    month: str                       # "2026-08"
    principal: float = 0.0
    coupon: float = 0.0
    issuers: set[str] = field(default_factory=set)

    @property
    def total(self) -> float:
        return self.principal + self.coupon


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def build_wall(
    flows_by_issuer: dict[str, list[tuple[date, float, str]]],
    horizon_months: int = 24,
    today: date | None = None,
) -> list[WallBucket]:
    """flows_by_issuer: issuer_id -> list of (date, amount, kind) where kind is
    'coupon' | 'amortization' | 'redemption'. Amounts should already be scaled
    to the outstanding notional the caller cares about (per-bond or aggregated).
    """
    today = today or date.today()
    buckets: dict[str, WallBucket] = {}
    for issuer, flows in flows_by_issuer.items():
        for d, amt, kind in flows:
            if d <= today:
                continue
            months_ahead = (d.year - today.year) * 12 + (d.month - today.month)
            if months_ahead > horizon_months:
                continue
            k = month_key(d)
            b = buckets.setdefault(k, WallBucket(month=k))
            if kind == "coupon":
                b.coupon += amt
            else:
                b.principal += amt
            b.issuers.add(issuer)
    return [buckets[k] for k in sorted(buckets)]


def redemptions_by_issuer(
    flows_by_issuer: dict[str, list[tuple[date, float, str]]],
    today: date | None = None,
) -> dict[str, list[tuple[date, float]]]:
    """Just the principal repayments ahead, per issuer, sorted by date — the
    core of a refinancing-pressure view."""
    today = today or date.today()
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for issuer, flows in flows_by_issuer.items():
        for d, amt, kind in flows:
            if d > today and kind in ("amortization", "redemption"):
                out[issuer].append((d, amt))
        out[issuer].sort()
    return dict(out)
