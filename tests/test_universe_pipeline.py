"""Tests for the universe screening predicate. Pure logic, no network, no DB.

Regression guard for the OFZ-exclusion bug found while building this: the
originally-specified criteria ("ISIN starts with SU", "board is TQOB") don't
match MOEX's real data (SU-prefix lives on SECID; OFZ sits on both TQOB and
SPOB) -- these tests pin the corrected behavior.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.universe_pipeline import (  # noqa: E402
    COMMODITY_LINKED, SHORT_TENOR_THIN, _passes_screen)

TODAY = date(2026, 7, 28)


def _bond(**overrides):
    m = {"FACEUNIT": "SUR", "MATDATE": "2028-01-01", "BOARDID": "TQCB"}
    m.update(overrides)
    return m


def _quote(**overrides):
    q = {"LAST": 95.0}
    q.update(overrides)
    return q


def test_accepts_a_normal_corporate_bond():
    assert _passes_screen("RU000A10B4A4", _bond(), _quote(), TODAY)


def test_excludes_ofz_by_secid_prefix():
    # OFZ SECIDs start with SU; their ISIN never does -- SECID is the field
    # that actually carries this signal.
    assert not _passes_screen("SU26207RMFS9", _bond(BOARDID="SPOB"), _quote(), TODAY)


def test_excludes_ofz_on_tqob_board():
    assert not _passes_screen("RU000TEST001", _bond(BOARDID="TQOB"), _quote(), TODAY)


def test_excludes_ofz_on_spob_board():
    # The originally-specified filter only excluded TQOB -- SPOB slips
    # through unless both are checked.
    assert not _passes_screen("RU000TEST002", _bond(BOARDID="SPOB"), _quote(), TODAY)


def test_excludes_non_rub_denominated():
    assert not _passes_screen("RU000TEST003", _bond(FACEUNIT="USD"), _quote(), TODAY)


def test_excludes_bond_maturing_within_30_days():
    assert not _passes_screen(
        "RU000TEST004", _bond(MATDATE="2026-08-10"), _quote(), TODAY)


def test_excludes_bond_with_no_maturity_date():
    assert not _passes_screen("RU000TEST005", _bond(MATDATE=None), _quote(), TODAY)


def test_excludes_bond_with_no_price():
    assert not _passes_screen("RU000TEST006", _bond(), _quote(LAST=None), TODAY)


# --- signal-exclusion tests -------------------------------------------------
# Mirrors the branch in universe_pipeline.run_universe() that nulls the
# mispricing fields when either heuristic fires -- these bonds still keep
# their raw ytm/g_spread_bp, only the signal built on top is excluded.

def _apply_signal_exclusion(bond: dict, implied_fair: float | None, misprice: float | None):
    """Same decision as run_universe()'s post-rating branch, isolated for
    testing without a DB/network round-trip."""
    signal_excluded = False
    if COMMODITY_LINKED(bond):
        signal_excluded = True
    elif SHORT_TENOR_THIN(bond):
        signal_excluded = True
    if signal_excluded:
        implied_fair = None
        misprice = None
    return signal_excluded, implied_fair, misprice


def test_commodity_linked_bond_excludes_mispricing_signal():
    # Селигдар-style gold/silver-linked note: low coupon, MOEX yield well
    # under the risk-free rate -- structural, not a real mispricing signal.
    bond = {"coupon_pct": 5.5, "ytm": 0.06, "tenor_yrs": 3.4, "num_trades": 47}
    excluded, implied_fair, misprice = _apply_signal_exclusion(bond, 91.0, -650.0)
    assert excluded is True
    assert misprice is None


def test_near_maturity_thin_print_excludes_mispricing_signal():
    bond = {"coupon_pct": 21.0, "ytm": 0.0857, "tenor_yrs": 0.3, "num_trades": 3}
    excluded, implied_fair, misprice = _apply_signal_exclusion(bond, 53.0, -553.0)
    assert excluded is True
    assert misprice is None


def test_normal_bond_keeps_mispricing_signal():
    bond = {"coupon_pct": 18.0, "ytm": 0.22, "tenor_yrs": 2.0, "num_trades": 50}
    excluded, implied_fair, misprice = _apply_signal_exclusion(bond, 199.0, 850.0)
    assert excluded is False
    assert misprice == 850.0
