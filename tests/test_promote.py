"""Tests for promote.promote_candidates() -- the Phase 2 rating-floor ->
liquidity-floor -> AEY-ranking -> issuer-cap promotion screen. Uses a real
(tmp-file) SQLite DB via db.connect(), same pattern as test_portfolio.py's
rank_candidates tests -- no network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import promote  # noqa: E402
from hybond.store import db  # noqa: E402


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_universe(con)
    return con


def _row(**overrides):
    row = dict(
        secid="X", snap_date="2026-07-31", short_name="X", emitent_inn="INN1",
        emitent_title="Issuer 1", mat_date="2028-01-01", offer_date=None,
        coupon_pct=15.0, list_level=1, qualified=0, last_pct=95.0, yield_moex=20.0,
        yield_to_offer=None, accrued_int=0.0, volume=1000.0, num_trades=50,
        duration_moex=365.0, ytm=0.20, tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=600.0,
        yield_source="moex", rating="ruBB", implied_fair_spread_bp=153.0,
        mispricing_bp=447.0, in_watchlist=1, signal_excluded=0,
        mod_dur=1.0, aey_base=0.20, aey_stress=0.19, aey_bull=0.22,
        aey_base_risk_adj=0.174, vdo_scope=1,
    )
    row.update(overrides)
    return row


def test_no_snapshot_data_returns_empty(tmp_path):
    con = _con(tmp_path)
    report = promote.promote_candidates(con)
    assert report["snap_date"] is None
    assert report["candidates"] == []
    assert report["unrated_near_miss"] == []


def test_ccc_rating_is_cut_by_rating_floor(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="A", rating="ruCCC"))
    db.insert_universe_snapshot(con, **_row(secid="B", rating="ruBB", emitent_inn="INN2"))
    con.commit()
    report = promote.promote_candidates(con)
    assert report["cut_by_rating_floor"] == 1
    assert [c["secid"] for c in report["candidates"]] == ["B"]


def test_ratings_at_and_below_b_minus_floor(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="ATFLOOR", rating="ruB-", emitent_inn="I1"))
    db.insert_universe_snapshot(con, **_row(secid="BELOW", rating="ruCCC", emitent_inn="I2"))
    db.insert_universe_snapshot(con, **_row(secid="ABOVE", rating="ruBBB", emitent_inn="I3"))
    con.commit()
    report = promote.promote_candidates(con)
    secids = {c["secid"] for c in report["candidates"]}
    assert "ATFLOOR" in secids
    assert "ABOVE" in secids
    assert "BELOW" not in secids
    assert report["cut_by_rating_floor"] == 1


def test_unrated_bond_excluded_but_tracked_as_near_miss(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(
        secid="UNRATED", rating=None, aey_base_risk_adj=None,
        emitent_inn="I1", g_spread_bp=900.0))
    con.commit()
    report = promote.promote_candidates(con)
    assert report["candidates"] == []
    assert len(report["unrated_near_miss"]) == 1
    assert report["unrated_near_miss"][0]["secid"] == "UNRATED"
    # unrated bonds don't count against the rating-floor cut -- they're
    # tracked separately, not silently dropped as a rating-floor failure
    assert report["cut_by_rating_floor"] == 0


def test_liquidity_floor_cuts_thin_volume(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="THIN", volume=100.0, emitent_inn="I1"))
    db.insert_universe_snapshot(con, **_row(secid="LIQUID", volume=5000.0, emitent_inn="I2"))
    con.commit()
    report = promote.promote_candidates(con, min_volume=300)
    assert report["cut_by_liquidity_floor"] == 1
    assert [c["secid"] for c in report["candidates"]] == ["LIQUID"]


def test_missing_aey_is_cut_and_counted(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="NOAEY", aey_base_risk_adj=None, emitent_inn="I1"))
    db.insert_universe_snapshot(con, **_row(secid="HASAEY", emitent_inn="I2"))
    con.commit()
    report = promote.promote_candidates(con)
    assert report["cut_by_missing_aey"] == 1
    assert [c["secid"] for c in report["candidates"]] == ["HASAEY"]


def test_ranks_survivors_by_aey_risk_adj_descending(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="LOW", aey_base_risk_adj=0.10, emitent_inn="I1"))
    db.insert_universe_snapshot(con, **_row(secid="HIGH", aey_base_risk_adj=0.30, emitent_inn="I2"))
    db.insert_universe_snapshot(con, **_row(secid="MID", aey_base_risk_adj=0.20, emitent_inn="I3"))
    con.commit()
    report = promote.promote_candidates(con)
    assert [c["secid"] for c in report["candidates"]] == ["HIGH", "MID", "LOW"]


def test_issuer_concentration_cap_applied_after_ranking(tmp_path):
    con = _con(tmp_path)
    # 3 bonds from the same issuer, all otherwise qualifying -- cap=2 should
    # keep only the top 2 by aey_base_risk_adj.
    db.insert_universe_snapshot(con, **_row(
        secid="A1", emitent_inn="SAME", aey_base_risk_adj=0.30))
    db.insert_universe_snapshot(con, **_row(
        secid="A2", emitent_inn="SAME", aey_base_risk_adj=0.25))
    db.insert_universe_snapshot(con, **_row(
        secid="A3", emitent_inn="SAME", aey_base_risk_adj=0.20))
    con.commit()
    report = promote.promote_candidates(con, per_issuer_cap=2)
    secids = [c["secid"] for c in report["candidates"]]
    assert secids == ["A1", "A2"]
    assert report["cut_by_concentration_cap"] == 1


def test_target_after_cap_limits_final_list_size(tmp_path):
    con = _con(tmp_path)
    for i in range(5):
        db.insert_universe_snapshot(con, **_row(
            secid=f"S{i}", emitent_inn=f"I{i}", aey_base_risk_adj=0.10 + i * 0.01))
    con.commit()
    report = promote.promote_candidates(con, target_after_cap=3)
    assert len(report["candidates"]) == 3


def test_non_vdo_scope_rows_are_excluded_entirely(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="OUT", vdo_scope=0, emitent_inn="I1"))
    db.insert_universe_snapshot(con, **_row(secid="IN", vdo_scope=1, emitent_inn="I2"))
    con.commit()
    report = promote.promote_candidates(con)
    assert report["scope_total"] == 1
    assert [c["secid"] for c in report["candidates"]] == ["IN"]


def test_unrated_near_miss_sorted_by_g_spread_descending(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(
        secid="LOWSPREAD", rating=None, aey_base_risk_adj=None,
        emitent_inn="I1", g_spread_bp=600.0))
    db.insert_universe_snapshot(con, **_row(
        secid="HIGHSPREAD", rating=None, aey_base_risk_adj=None,
        emitent_inn="I2", g_spread_bp=1500.0))
    con.commit()
    report = promote.promote_candidates(con)
    assert [r["secid"] for r in report["unrated_near_miss"]] == ["HIGHSPREAD", "LOWSPREAD"]


def test_custom_rating_floor_is_stricter(tmp_path):
    con = _con(tmp_path)
    db.insert_universe_snapshot(con, **_row(secid="BB", rating="ruBB", emitent_inn="I1"))
    db.insert_universe_snapshot(con, **_row(secid="BBB", rating="ruBBB", emitent_inn="I2"))
    con.commit()
    report = promote.promote_candidates(con, rating_floor="ruBBB")
    secids = {c["secid"] for c in report["candidates"]}
    assert secids == {"BBB"}
    assert report["cut_by_rating_floor"] == 1
