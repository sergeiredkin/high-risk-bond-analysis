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
