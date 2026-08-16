"""Tests for portfolio.get_rate_scenarios/bond_aey/pd_adjusted_aey. Uses a
real (tmp-file) SQLite DB via db.connect(), same pattern as test_slice1.py --
no network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import portfolio  # noqa: E402
from hybond.compute import default_probs  # noqa: E402
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


def _snapshot_row(**overrides):
    row = dict(
        secid="X", snap_date="2026-07-30", short_name="X", emitent_inn="INN1",
        emitent_title="Issuer 1", mat_date="2028-01-01", offer_date=None,
        coupon_pct=15.0, list_level=1, qualified=0, last_pct=95.0, yield_moex=20.0,
        yield_to_offer=None, accrued_int=0.0, volume=1000.0, num_trades=50,
        duration_moex=365.0, ytm=0.20, tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=600.0,
        yield_source="moex", rating="ruBB", implied_fair_spread_bp=153.0,
        mispricing_bp=447.0, in_watchlist=1, signal_excluded=0,
        mod_dur=1.0, aey_base=0.20, aey_stress=0.19, aey_bull=0.22,
        aey_base_risk_adj=0.174,
    )
    row.update(overrides)
    return row


def _rank_con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_universe(con)
    return con


def test_bond_aey_price_effect_direction():
    # Falling rates (negative delta) should raise the adjusted yield for a
    # duration-bearing bond: aey = ytm - mod_dur * delta_rate.
    aey = portfolio.bond_aey(0.20, 2.0, -0.02)
    assert abs(aey - (0.20 + 0.04)) < 1e-9


def test_pd_adjusted_aey_known_rating_hand_computed():
    # ruBB: yr1 PD = 0.0460 (Эксперт РА Таблица 7, 01.07.2026). Hand-computed:
    # (1 - 0.0460) * 0.20 - 0.0460 * 0.934 = 0.147836.
    result = portfolio.pd_adjusted_aey(0.20, "ruBB")
    expected = (1 - 0.0460) * 0.20 - 0.0460 * default_probs.LGD
    assert abs(result - expected) < 1e-9
    assert abs(result - 0.147836) < 1e-6


def test_pd_adjusted_aey_unrated_bond_returns_none():
    assert portfolio.pd_adjusted_aey(0.20, None) is None
    assert portfolio.pd_adjusted_aey(0.20, "") is None


def test_pd_adjusted_aey_unknown_rating_string_returns_none():
    # Not unrated, but a rating string that doesn't match any entry in the
    # Эксперт РА table -- lookup() misses, same as no rating at all.
    assert portfolio.pd_adjusted_aey(0.20, "not-a-real-rating") is None


def test_pd_adjusted_aey_none_aey_returns_none():
    assert portfolio.pd_adjusted_aey(None, "ruBB") is None


def test_pd_adjusted_aey_zero_pd_equals_raw_aey(monkeypatch):
    # PD=0 -> (1-0)*aey - 0*(1-recovery) == aey exactly: a riskless bond's
    # expected return should just be its running yield, untouched.
    monkeypatch.setitem(default_probs.DEFAULT_PROBS, "ruTESTPD0",
                         {"yr1": 0.0, "yr2": 0.0, "yr3": 0.0})
    result = portfolio.pd_adjusted_aey(0.20, "ruTESTPD0")
    assert abs(result - 0.20) < 1e-12


def test_pd_adjusted_aey_full_pd_equals_negative_severity(monkeypatch):
    # PD=1 -> (1-1)*aey - 1*(1-recovery) == -(1-recovery): a certain default
    # collapses the expected return to the pure loss-given-default term,
    # independent of whatever the (never-realized) running yield was.
    monkeypatch.setitem(default_probs.DEFAULT_PROBS, "ruTESTPD1",
                         {"yr1": 1.0, "yr2": 1.0, "yr3": 1.0})
    result = portfolio.pd_adjusted_aey(0.20, "ruTESTPD1")
    assert abs(result - (-(1 - default_probs.RECOVERY_RATE))) < 1e-12


# --- aey_risk_decomposition: same PD/recovery math, exposed as two scenarios

def test_aey_risk_decomposition_matches_pd_adjusted_aey():
    # Decomposition's expected_value must always equal pd_adjusted_aey()'s
    # output for the same inputs -- this is a decomposition, not a second
    # model, across a spread of ratings.
    for aey, rating in [(0.20, "ruBB"), (0.50, "ruB-"), (5.0, "ruCCC"), (1.2, "ruC")]:
        blended = portfolio.pd_adjusted_aey(aey, rating)
        decomp = portfolio.aey_risk_decomposition(aey, rating)
        assert abs(decomp["expected_value"] - blended) < 1e-9


def test_aey_risk_decomposition_known_rating_hand_computed():
    # ruBB: yr1 PD = 0.0460. p_survive/p_default must sum to 1, and the
    # blended value must hand-match the same computation as the
    # pd_adjusted_aey test above.
    result = portfolio.aey_risk_decomposition(0.20, "ruBB")
    assert abs(result["p_default"] - 0.0460) < 1e-9
    assert abs(result["p_survive"] - (1 - 0.0460)) < 1e-9
    assert abs(result["return_if_survive"] - 0.20) < 1e-12
    assert abs(result["return_if_default"] - (-default_probs.LGD)) < 1e-12
    assert abs(result["expected_value"] - 0.147836) < 1e-6


def test_aey_risk_decomposition_unrated_bond_returns_none():
    assert portfolio.aey_risk_decomposition(0.20, None) is None
    assert portfolio.aey_risk_decomposition(0.20, "") is None


def test_aey_risk_decomposition_unknown_rating_string_returns_none():
    assert portfolio.aey_risk_decomposition(0.20, "not-a-real-rating") is None


def test_aey_risk_decomposition_none_aey_returns_none():
    assert portfolio.aey_risk_decomposition(None, "ruBB") is None


def test_aey_risk_decomposition_zero_pd_equals_raw_aey(monkeypatch):
    monkeypatch.setitem(default_probs.DEFAULT_PROBS, "ruTESTPD0",
                         {"yr1": 0.0, "yr2": 0.0, "yr3": 0.0})
    result = portfolio.aey_risk_decomposition(0.20, "ruTESTPD0")
    assert result["p_survive"] == 1.0
    assert result["p_default"] == 0.0
    assert abs(result["expected_value"] - 0.20) < 1e-12


def test_aey_risk_decomposition_full_pd_equals_negative_severity(monkeypatch):
    monkeypatch.setitem(default_probs.DEFAULT_PROBS, "ruTESTPD1",
                         {"yr1": 1.0, "yr2": 1.0, "yr3": 1.0})
    result = portfolio.aey_risk_decomposition(0.20, "ruTESTPD1")
    assert result["p_survive"] == 0.0
    assert result["p_default"] == 1.0
    assert abs(result["expected_value"] - (-(1 - default_probs.RECOVERY_RATE))) < 1e-12


# --- apply_issuer_cap: pure list logic, no DB -------------------------------

def test_apply_issuer_cap_keeps_up_to_cap_per_issuer():
    rows = [
        {"emitent_inn": "A", "secid": "A1"},
        {"emitent_inn": "A", "secid": "A2"},
        {"emitent_inn": "A", "secid": "A3"},
        {"emitent_inn": "B", "secid": "B1"},
    ]
    capped = portfolio.apply_issuer_cap(rows, per_issuer_cap=2)
    assert [r["secid"] for r in capped] == ["A1", "A2", "B1"]


def test_apply_issuer_cap_preserves_order_of_survivors():
    # A lower-ranked row from an issuer under the cap must still outrank a
    # higher-ranked row from an issuer that already hit it -- this is what
    # lets rank_candidates() just slice [:limit] after capping.
    rows = [
        {"emitent_inn": "A", "secid": "A1"},
        {"emitent_inn": "A", "secid": "A2"},
        {"emitent_inn": "A", "secid": "A3"},  # dropped: A already has 2
        {"emitent_inn": "B", "secid": "B1"},  # survives despite ranking last
    ]
    capped = portfolio.apply_issuer_cap(rows, per_issuer_cap=2)
    assert capped == [
        {"emitent_inn": "A", "secid": "A1"},
        {"emitent_inn": "A", "secid": "A2"},
        {"emitent_inn": "B", "secid": "B1"},
    ]


def test_apply_issuer_cap_cap_of_one():
    rows = [{"emitent_inn": "A", "secid": "A1"}, {"emitent_inn": "A", "secid": "A2"}]
    capped = portfolio.apply_issuer_cap(rows, per_issuer_cap=1)
    assert [r["secid"] for r in capped] == ["A1"]


# --- rank_candidates: integration through a real (tmp-file) DB -------------

def test_rank_candidates_enforces_cap_and_ytm_bound(tmp_path):
    con = _rank_con(tmp_path)
    # Issuer 1: three series, all otherwise top-ranked -- only 2 should
    # survive the per-issuer cap.
    for i, risk_adj in enumerate([0.50, 0.45, 0.40]):
        db.insert_universe_snapshot(con, **_snapshot_row(
            secid=f"ISS1-{i}", emitent_inn="INN1", aey_base_risk_adj=risk_adj))
    # Issuer 2: one lower-ranked series -- must still appear (not crowded
    # out by issuer 1's third series).
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="ISS2-0", emitent_inn="INN2", emitent_title="Issuer 2",
        aey_base_risk_adj=0.30))
    # A distress-priced bond with an even higher risk_adj value but a YTM
    # over the sanity bound -- must be excluded regardless of rank.
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="DISTRESS", emitent_inn="INN3", emitent_title="Distress Co",
        ytm=3.5, aey_base_risk_adj=0.90))
    con.commit()

    result = portfolio.rank_candidates(con, limit=15, per_issuer_cap=2, ytm_max=0.80)

    secids = [r["secid"] for r in result]
    assert "DISTRESS" not in secids
    assert secids.count("ISS1-0") == 1 and secids.count("ISS1-1") == 1
    assert "ISS1-2" not in secids  # capped out
    assert "ISS2-0" in secids
    # aey_base_risk_adj descending among survivors
    assert secids == ["ISS1-0", "ISS1-1", "ISS2-0"]


def test_rank_candidates_respects_limit_after_capping(tmp_path):
    con = _rank_con(tmp_path)
    for i in range(5):
        db.insert_universe_snapshot(con, **_snapshot_row(
            secid=f"S{i}", emitent_inn=f"INN{i}", aey_base_risk_adj=1.0 - i * 0.1))
    con.commit()
    result = portfolio.rank_candidates(con, limit=3, per_issuer_cap=2)
    assert len(result) == 3
    assert [r["secid"] for r in result] == ["S0", "S1", "S2"]


def test_rank_candidates_no_snapshot_data_returns_empty(tmp_path):
    con = _rank_con(tmp_path)
    assert portfolio.rank_candidates(con) == []


def test_rank_candidates_excludes_bonds_outside_watchlist_by_default(tmp_path):
    con = _rank_con(tmp_path)
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="NOTWL", emitent_inn="INN1", in_watchlist=0, aey_base_risk_adj=0.9))
    con.commit()
    assert portfolio.rank_candidates(con) == []
    assert portfolio.rank_candidates(con, in_watchlist_only=False) != []
