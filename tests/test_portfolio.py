"""Tests for portfolio.get_rate_scenarios/bond_aey. Uses a real (tmp-file)
SQLite DB via db.connect(), same pattern as test_slice1.py -- no network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import portfolio  # noqa: E402
from hybond.store import db  # noqa: E402


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_cbr(con)
    return con


def test_fallback_when_no_meeting_rows(tmp_path):
    con = _con(tmp_path)
    scenarios = portfolio.get_rate_scenarios(con)
    assert scenarios["is_fallback"] is True
    assert scenarios["source_date"] == "fallback"
    assert scenarios["base"] == -0.010


def test_fallback_when_analysis_json_null(tmp_path):
    # A meeting row exists but hasn't had its analysis computed yet --
    # must still fall back, not crash on missing forecast columns.
    con = _con(tmp_path)
    db.upsert_cbr_meeting(con, meeting_date="2026-07-24", rate_after=14.0,
                           rate_2026_mid="14,5-14,6", rate_2027_mid="10,5-12,5",
                           rate_2029_mid="7,5-8,5")
    scenarios = portfolio.get_rate_scenarios(con)
    assert scenarios["is_fallback"] is True


def test_live_scenarios_from_completed_meeting(tmp_path):
    con = _con(tmp_path)
    db.upsert_cbr_meeting(
        con, meeting_date="2026-07-24", rate_after=14.0,
        rate_2026_mid="14,5-14,6", rate_2027_mid="10,5-12,5",
        rate_2029_mid="7,5-8,5", analysis_json="{}")
    scenarios = portfolio.get_rate_scenarios(con)
    assert scenarios["is_fallback"] is False
    assert scenarios["source_date"] == "2026-07-24"
    # base = 2027 mid (11.5) - current (14.0) = -2.5 -> -0.025
    assert abs(scenarios["base"] - (-0.025)) < 1e-9
    # stress = 2026 HIGH (14.6) - current (14.0) = 0.6 -> 0.006
    assert abs(scenarios["stress"] - 0.006) < 1e-9
    # bull = 2029 mid (8.0) - current (14.0) = -6.0 -> -0.06
    assert abs(scenarios["bull"] - (-0.06)) < 1e-9


def test_picks_most_recent_completed_meeting(tmp_path):
    con = _con(tmp_path)
    db.upsert_cbr_meeting(
        con, meeting_date="2026-04-24", rate_after=15.0,
        rate_2026_mid="14,0-14,5", rate_2027_mid="8,0-10,0",
        rate_2029_mid="7,5-8,5", analysis_json="{}")
    db.upsert_cbr_meeting(
        con, meeting_date="2026-07-24", rate_after=14.0,
        rate_2026_mid="14,5-14,6", rate_2027_mid="10,5-12,5",
        rate_2029_mid="7,5-8,5", analysis_json="{}")
    scenarios = portfolio.get_rate_scenarios(con)
    assert scenarios["source_date"] == "2026-07-24"


def test_bond_aey_none_when_duration_missing():
    assert portfolio.bond_aey(0.20, None, -0.02) is None


def test_bond_aey_none_when_ytm_missing():
    assert portfolio.bond_aey(None, 1.5, -0.02) is None


def test_bond_aey_price_effect_direction():
    # Falling rates (negative delta) should raise the adjusted yield for a
    # duration-bearing bond: aey = ytm - mod_dur * delta_rate.
    aey = portfolio.bond_aey(0.20, 2.0, -0.02)
    assert abs(aey - (0.20 + 0.04)) < 1e-9
