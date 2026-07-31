"""Tests for compute.expected_return.project_expected_return -- the
survival-weighted multi-horizon IRR projection. No network; hand-built
coupon/amortization schedules only."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.compute import bondmath, default_probs  # noqa: E402
from hybond.compute.expected_return import project_expected_return  # noqa: E402

SETTLEMENT = date(2026, 1, 1)
FACE = 1000.0
# 3-year annual bullet bond: coupon 100/yr at t=1,2,3; redemption 1000 at t=3.
COUPONS = [(date(2027, 1, 1), 100.0), (date(2028, 1, 1), 100.0), (date(2029, 1, 1), 100.0)]
AMORTS = [(date(2029, 1, 1), 1000.0)]


def _zero_pd_rating(monkeypatch, name="ruTESTPD0"):
    monkeypatch.setitem(default_probs.DEFAULT_PROBS, name,
                         {"yr1": 0.0, "yr2": 0.0, "yr3": 0.0})
    return name


def test_zero_pd_to_maturity_matches_plain_ytm(monkeypatch):
    # With PD=0 everywhere, survival is always 1 -- the expected-cashflow IRR
    # to maturity must exactly equal the bond's plain contractual YTM.
    rating = _zero_pd_rating(monkeypatch)
    pairs = [((d - SETTLEMENT).days / 365.0, amt) for d, amt in COUPONS]
    pairs.append(((date(2029, 1, 1) - SETTLEMENT).days / 365.0, 1000.0))
    price = bondmath.price_from_ytm(0.15, pairs)
    result = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, price, rating)
    expected_ytm = bondmath.ytm_from_price(price, pairs)
    assert abs(result["irr_pct"] - expected_ytm * 100) < 0.05
    assert abs(result["horizon_yr"] - 3.0) < 0.01
    assert result["horizon_capped_by_maturity"] is False
    assert result["uses_extrapolated_pd"] is False


def test_zero_pd_truncated_horizon_matches_exit_at_same_price(monkeypatch):
    # Truncating at 2yr with PD=0: exit flow = dirty_price itself (survival
    # 1 at t=2), so IRR must match solving price = 100/(1+r) + (100+price)/(1+r)^2.
    rating = _zero_pd_rating(monkeypatch)
    price = 950.0
    result = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, price,
                                      rating, horizon_yr=2)
    expected_pairs = [(1.0, 100.0), (2.0, 100.0 + price)]
    expected_irr = bondmath.ytm_from_price(price, expected_pairs)
    assert abs(result["irr_pct"] - expected_irr * 100) < 0.05
    assert result["horizon_yr"] == 2.0
    assert result["horizon_capped_by_maturity"] is False


def test_horizon_longer_than_bond_life_is_capped(monkeypatch):
    rating = _zero_pd_rating(monkeypatch)
    price = 950.0
    at_maturity = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, price, rating)
    capped = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, price,
                                      rating, horizon_yr=10)
    assert capped["horizon_yr"] == at_maturity["horizon_yr"]
    assert abs(capped["horizon_yr"] - 3.0) < 0.01
    assert capped["horizon_capped_by_maturity"] is True
    assert abs(capped["irr_pct"] - at_maturity["irr_pct"]) < 1e-9


def test_full_pd_collapses_expected_return_to_recovery_only(monkeypatch):
    # PD=1 in year 1 -> the bond defaults with certainty before any coupon
    # is paid; the entire expected cash flow is the recovery on outstanding
    # face at t=1 (RECOVERY_RATE * face), nothing else survives to be paid.
    monkeypatch.setitem(default_probs.DEFAULT_PROBS, "ruTESTPD1",
                         {"yr1": 1.0, "yr2": 1.0, "yr3": 1.0})
    price = 500.0
    result = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, price, "ruTESTPD1")
    expected_recovery = default_probs.RECOVERY_RATE * FACE
    assert abs(result["undiscounted_total_cashflow"] - expected_recovery) < 1e-6


def test_uses_extrapolated_pd_flag_beyond_3yr(monkeypatch):
    rating = _zero_pd_rating(monkeypatch)
    long_coupons = [(date(2026 + y, 1, 1), 100.0) for y in range(1, 6)]
    long_amorts = [(date(2031, 1, 1), 1000.0)]
    price = 900.0
    result = project_expected_return(long_coupons, long_amorts, SETTLEMENT, FACE,
                                      price, rating)
    assert abs(result["horizon_yr"] - 5.0) < 0.01
    assert result["uses_extrapolated_pd"] is True


def test_within_3yr_horizon_does_not_use_extrapolation(monkeypatch):
    rating = _zero_pd_rating(monkeypatch)
    price = 950.0
    result = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, price,
                                      rating, horizon_yr=2)
    assert result["uses_extrapolated_pd"] is False


def test_unrated_bond_returns_none():
    assert project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, 950.0, None) is None
    assert project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE, 950.0, "not-a-rating") is None


def test_no_cashflows_returns_none():
    assert project_expected_return([], [], SETTLEMENT, FACE, 950.0, "ruBB") is None


def test_real_rating_produces_lower_irr_than_zero_pd_case(monkeypatch):
    # Sanity check against a real (non-monkeypatched) rating: a genuine PD
    # should always drag expected IRR below the PD=0 case at the same price.
    zero_rating = _zero_pd_rating(monkeypatch)
    price = 950.0
    zero_pd_result = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE,
                                              price, zero_rating)
    real_result = project_expected_return(COUPONS, AMORTS, SETTLEMENT, FACE,
                                           price, "ruB-")
    assert real_result["irr_pct"] < zero_pd_result["irr_pct"]
