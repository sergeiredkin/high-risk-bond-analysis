"""Offline unit tests for the default-probability lookup module. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.compute import default_probs as dp  # noqa: E402


def test_lookup_ruBB_matches_table():
    # 2026-08-16 PD rebase: yr1 is the real Эксперт РА Таблица 7 (01.07.2026)
    # figure; yr2/yr3 are hazard-derived from it (see default_probs.py's
    # module docstring "Structural note") rather than independent agency
    # data -- so this test derives its expectation the same way the module
    # does, instead of hardcoding a second copy of the hazard formula.
    probs = dp.lookup("ruBB")
    assert probs["yr1"] == dp.DEFAULT_PROBS_1YR["ruBB"] == 0.0460
    expected = dp._hazard_derived_multi_year(0.0460)
    assert abs(probs["yr2"] - expected["yr2"]) < 1e-12
    assert abs(probs["yr3"] - expected["yr3"]) < 1e-12


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
    # 0.0460 * 0.934 * 10000 = 429.64
    assert abs(result - 429.64) < 1


def test_mispricing_positive_is_cheap():
    result = dp.mispricing_bp(500, "ruBB", 1)
    # 500 - 429.64 = 70.36
    assert abs(result - 70.36) < 1
    assert result > 0


def test_mispricing_negative_is_rich():
    # ruBBB fair = 0.0198 * 0.934 * 10000 = 184.93bp. An actual spread of
    # 100bp is tighter than that -- rich, negative mispricing.
    result = dp.mispricing_bp(100, "ruBBB", 1)
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
    yr3 = dp.lookup("ruBB")["yr3"]
    h = dp.implied_annual_hazard_from_yr3("ruBB")
    expected = 1 - (1 - yr3) ** (1 / 3)
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


# --- PD table recalibration + LGD fix (2026-08-16) --------------------------

def test_lgd_derived_from_recovery_rate():
    assert dp.RECOVERY_RATE == 0.066
    assert dp.LGD == 1 - dp.RECOVERY_RATE
    assert abs(dp.LGD - 0.934) < 1e-9


def test_official_1yr_pd_table_spot_checks():
    # Эксперт РА Таблица 7, 01.07.2026 -- a few tiers spanning the scale.
    assert dp.DEFAULT_PROBS_1YR["ruAAA"] == 0.0
    assert dp.DEFAULT_PROBS_1YR["ruAA-"] == 0.0  # zero observed defaults
    assert dp.DEFAULT_PROBS_1YR["ruBB-"] == 0.0598
    assert dp.DEFAULT_PROBS_1YR["ruC"] == 0.5
    assert dp.lookup("ruBB-")["yr1"] == 0.0598


def test_official_table_non_monotonic_b_tier_preserved_not_smoothed():
    # The official 01.07.2026 disclosure has ruB+ (better letter grade)
    # showing a HIGHER 1yr default rate than ruB (worse letter grade) --
    # small-sample noise the old theoretical table deliberately smoothed
    # away. This task explicitly directs using the raw official figures
    # anyway, so the module must reproduce that non-monotonicity verbatim,
    # not silently fix it.
    assert dp.DEFAULT_PROBS_1YR["ruB+"] > dp.DEFAULT_PROBS_1YR["ruB"]


def test_zero_pd_tier_has_zero_yr2_yr3():
    probs = dp.lookup("ruAA-")
    assert probs == {"yr1": 0.0, "yr2": 0.0, "yr3": 0.0}


def test_lookup_falls_back_to_base_tier_for_scale_suffix():
    # No such suffixed rating exists in the live DB today (grepped 2026-08-16
    # -- see lookup()'s docstring), but the fallback itself must work: a
    # rating string with a "." suffix not present verbatim in DEFAULT_PROBS
    # should resolve to its base tier, not silently miss.
    assert dp.lookup("ruB+.mfi") == dp.lookup("ruB+")
    assert dp.lookup("ruBB-.retail") == dp.lookup("ruBB-")


def test_lookup_unknown_base_tier_with_suffix_still_none():
    assert dp.lookup("ruXXX.mfi") is None


def test_implied_fair_spread_bp_uses_lgd_not_old_recovery_rate():
    # Locks in the actual numeric shift Fix 2 is meant to produce: LGD went
    # from 0.65 to 0.934, a ~44% increase in the loss-given-default term.
    fair = dp.implied_fair_spread_bp("ruBB-", 1)
    old_style_fair = dp.DEFAULT_PROBS_1YR["ruBB-"] * 0.65 * 10000
    assert fair > old_style_fair
