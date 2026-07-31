"""Offline unit tests for the default-probability lookup module. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.compute import default_probs as dp  # noqa: E402


def test_lookup_ruBB_matches_table():
    probs = dp.lookup("ruBB")
    assert probs == {"yr1": 0.03056, "yr2": 0.06975, "yr3": 0.10359}


def test_lookup_without_ru_prefix():
    assert dp.lookup("BB") == dp.lookup("ruBB")


def test_lookup_lowercase():
    assert dp.lookup("bb") == dp.lookup("ruBB")


def test_lookup_ruAAA_returns_values():
    assert dp.lookup("ruAAA") is not None


def test_lookup_unknown_rating_returns_none():
    assert dp.lookup("ruXXX") is None


def test_implied_fair_spread_ruBB():
    result = dp.implied_fair_spread_bp("ruBB", 1)
    # 0.03056 * 0.65 * 10000 = 198.64
    assert abs(result - 198.6) < 1


def test_mispricing_positive_is_cheap():
    result = dp.mispricing_bp(500, "ruBB", 1)
    # 500 - 198.64 = 301.36
    assert abs(result - 301) < 1
    assert result > 0


def test_mispricing_negative_is_rich():
    # ruBBB fair = 0.01396 * 0.65 * 10000 = 90.74bp. An actual spread of 50bp
    # is tighter than that -- rich, negative mispricing. (The spec's original
    # example used 100bp, but 100 > 90.74 is actually a *positive* -- cheap --
    # result by this same formula; that example had the sign backwards, so
    # this uses a value that genuinely demonstrates the rich/negative case.)
    result = dp.mispricing_bp(50, "ruBBB", 1)
    assert result < 0


def test_implied_fair_spread_unknown_rating_returns_none():
    assert dp.implied_fair_spread_bp("ruXXX", 1) is None


def test_ratings_are_monotone_increasing_risk():
    aa = dp.lookup("ruAA")["yr1"]
    bb = dp.lookup("ruBB")["yr1"]
    b = dp.lookup("ruB")["yr1"]
    assert aa < bb < b


# --- implied_annual_hazard_from_yr3 / survival_probability ------------------

def test_implied_annual_hazard_from_yr3_hand_computed():
    # ruBB yr3 PD = 0.10359. h = 1 - (1-0.10359)**(1/3).
    h = dp.implied_annual_hazard_from_yr3("ruBB")
    expected = 1 - (1 - 0.10359) ** (1 / 3)
    assert abs(h - expected) < 1e-9


def test_implied_annual_hazard_from_yr3_unknown_rating_returns_none():
    assert dp.implied_annual_hazard_from_yr3("ruXXX") is None


def test_survival_probability_exact_at_year_marks():
    # Must match 1 - PD exactly at t=1,2,3 -- these are the real agency
    # numbers, not something the interpolation is allowed to smooth over.
    probs = dp.lookup("ruBB")
    assert abs(dp.survival_probability("ruBB", 1) - (1 - probs["yr1"])) < 1e-12
    assert abs(dp.survival_probability("ruBB", 2) - (1 - probs["yr2"])) < 1e-12
    assert abs(dp.survival_probability("ruBB", 3) - (1 - probs["yr3"])) < 1e-12


def test_survival_probability_at_zero_is_one():
    assert dp.survival_probability("ruBB", 0) == 1.0


def test_survival_probability_monotonically_decreasing():
    # Survival can only fall (or stay flat) as horizon lengthens.
    ts = [0, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 10]
    vals = [dp.survival_probability("ruB-", t) for t in ts]
    assert all(a >= b - 1e-9 for a, b in zip(vals, vals[1:]))


def test_survival_probability_beyond_3yr_uses_extrapolation():
    # Past t=3, survival should keep declining at the constant hazard implied
    # by yr3 -- i.e. S(4) == S(3) * (1 - h_ext).
    s3 = dp.survival_probability("ruBB", 3)
    s4 = dp.survival_probability("ruBB", 4)
    h_ext = dp.implied_annual_hazard_from_yr3("ruBB")
    assert abs(s4 - s3 * (1 - h_ext)) < 1e-9


def test_survival_probability_negative_horizon_raises():
    import pytest
    with pytest.raises(ValueError):
        dp.survival_probability("ruBB", -1)


def test_survival_probability_unknown_rating_returns_none():
    assert dp.survival_probability("ruXXX", 1) is None
