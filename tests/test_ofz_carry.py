"""Unit tests for compute/ofz_carry.py -- textbook/hand-checked cases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.compute import ofz_carry  # noqa: E402


def test_coupon_return_pct_one_year():
    # 12% annual coupon, dirty price 85% of face, 1 year -> 12/85*100 = 14.117...%
    v = ofz_carry.coupon_return_pct(12.0, 85.0, 1)
    assert abs(v - 14.1176) < 1e-3


def test_coupon_return_pct_scales_with_years():
    v1 = ofz_carry.coupon_return_pct(10.0, 100.0, 1)
    v3 = ofz_carry.coupon_return_pct(10.0, 100.0, 3)
    assert abs(v3 - 3 * v1) < 1e-9


def test_coupon_return_pct_rejects_nonpositive_price():
    import pytest
    with pytest.raises(ValueError):
        ofz_carry.coupon_return_pct(10.0, 0.0, 1)


def test_body_reeval_duration_only():
    # -12yr modDur, -1% yield shift, no convexity -> +12%
    v = ofz_carry.body_reeval_pct(12.0, None, -0.01)
    assert abs(v - 12.0) < 1e-9


def test_body_reeval_with_convexity_adds_upside_on_rally():
    dur_only = ofz_carry.body_reeval_pct(12.0, 0.0, -0.02)
    with_cvx = ofz_carry.body_reeval_pct(12.0, 2.0, -0.02)
    # convexity term is 0.5 * 2.0 * (-0.02)^2 * 100 = 0.04 percentage points
    assert with_cvx > dur_only
    assert abs((with_cvx - dur_only) - 0.04) < 1e-9


def test_project_horizon_matches_prototype_shift_schedule():
    # 1yr horizon must use 40% of the total delta, 2yr 75%, 3yr 100%.
    r1 = ofz_carry.project_horizon(
        annual_coupon_pct=10.0, dirty_price_pct=100.0,
        mod_dur=10.0, convexity=None, total_delta_y=-0.025, horizon_years=1,
    )
    r2 = ofz_carry.project_horizon(
        annual_coupon_pct=10.0, dirty_price_pct=100.0,
        mod_dur=10.0, convexity=None, total_delta_y=-0.025, horizon_years=2,
    )
    r3 = ofz_carry.project_horizon(
        annual_coupon_pct=10.0, dirty_price_pct=100.0,
        mod_dur=10.0, convexity=None, total_delta_y=-0.025, horizon_years=3,
    )
    assert abs(r1["delta_y_applied"] - (-0.025 * 0.40)) < 1e-9
    assert abs(r2["delta_y_applied"] - (-0.025 * 0.75)) < 1e-9
    assert abs(r3["delta_y_applied"] - (-0.025 * 1.00)) < 1e-9
    # total_return is exactly coupon_return + body_reeval
    for r in (r1, r2, r3):
        assert abs(r["total_return_pct"] - (r["coupon_return_pct"] + r["body_reeval_pct"])) < 1e-6


def test_project_horizon_rejects_bad_horizon():
    import pytest
    with pytest.raises(ValueError):
        ofz_carry.project_horizon(
            annual_coupon_pct=10.0, dirty_price_pct=100.0,
            mod_dur=10.0, convexity=None, total_delta_y=-0.02, horizon_years=4,
        )


def test_project_custom_scenario_applies_full_delta_no_graduation():
    # unlike project_horizon, the full delta_y is applied regardless of
    # horizon_years -- no GRADUATED_SHIFT lookup, and horizon isn't
    # restricted to {1, 2, 3}.
    r = ofz_carry.project_custom_scenario(
        annual_coupon_pct=10.0, dirty_price_pct=100.0,
        mod_dur=10.0, convexity=None, delta_y=-0.015, horizon_years=1.4,
    )
    assert r["delta_y_applied"] == -0.015
    assert r["horizon_years"] == 1.4
    # body = -10 * -0.015 * 100 = 15.0; coupon = 10/100 * 1.4 * 100 = 14.0
    assert abs(r["body_reeval_pct"] - 15.0) < 1e-9
    assert abs(r["coupon_return_pct"] - 14.0) < 1e-9
    assert abs(r["total_return_pct"] - 29.0) < 1e-9


def test_project_custom_scenario_rejects_nonpositive_horizon():
    import pytest
    with pytest.raises(ValueError):
        ofz_carry.project_custom_scenario(
            annual_coupon_pct=10.0, dirty_price_pct=100.0,
            mod_dur=10.0, convexity=None, delta_y=-0.01, horizon_years=0,
        )


def test_project_all_horizons_returns_three():
    rows = ofz_carry.project_all_horizons(
        annual_coupon_pct=8.0, dirty_price_pct=95.0,
        mod_dur=7.0, convexity=0.6, total_delta_y=-0.015,
    )
    assert [r["horizon_years"] for r in rows] == [1, 2, 3]
    # longer horizon -> larger cumulative coupon return (coupon dominates
    # for a modest delta/duration combination here)
    assert rows[2]["coupon_return_pct"] > rows[0]["coupon_return_pct"]
