"""Tests for portfolio.get_rate_scenarios/bond_aey/pd_adjusted_aey. Uses a
real (tmp-file) SQLite DB via db.connect(), same pattern as test_slice1.py --
no network.
"""
import math
import sys
from pathlib import Path

import pytest

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


# ---- Portfolio stress test v2: poisson_pmf / multi_default_stress_test ----

def test_poisson_pmf_n_zero_is_e_neg_lambda():
    assert portfolio.poisson_pmf(2.0, 0) == pytest.approx(math.exp(-2.0))


def test_poisson_pmf_known_value():
    # lambda=1, n=1: e^-1 * 1^1 / 1! = e^-1
    assert portfolio.poisson_pmf(1.0, 1) == pytest.approx(math.exp(-1.0))
    # lambda=3, n=2: e^-3 * 9 / 2 = 4.5 * e^-3
    assert portfolio.poisson_pmf(3.0, 2) == pytest.approx(4.5 * math.exp(-3.0))


def test_poisson_pmf_lambda_zero_all_mass_at_n_zero():
    assert portfolio.poisson_pmf(0.0, 0) == 1.0
    assert portfolio.poisson_pmf(0.0, 3) == 0.0


def test_poisson_pmf_rejects_negative_inputs():
    with pytest.raises(ValueError):
        portfolio.poisson_pmf(-1.0, 0)
    with pytest.raises(ValueError):
        portfolio.poisson_pmf(1.0, -1)


def test_poisson_pmf_sums_to_one_across_full_support():
    # The discrete display buckets (0,1,2,3,5,8) deliberately don't sum to
    # 1 (they skip 4,6,7,...) -- this checks the PMF formula itself is a
    # valid distribution, summed over enough of its support to converge.
    for lam in (0.1, 1.0, 2.5, 5.0):
        total = sum(portfolio.poisson_pmf(lam, n) for n in range(0, 60))
        assert total == pytest.approx(1.0, abs=1e-9)


def _pos(issuer_id, weight_frac, aey_pct, pd_1y, name=None):
    return {"issuer_id": issuer_id, "name": name or issuer_id,
            "weight_frac": weight_frac, "aey_pct": aey_pct, "pd_1y": pd_1y}


def test_multi_default_known_inputs_two_equal_issuers():
    # Two issuers, equal 50/50 weight, AEY 20% each, PD 5% and 1%.
    # avg_issuer_weight = 1.0 / 2 = 0.5. LGD forced to 1.0 for round numbers.
    positions = [
        _pos("A", 0.5, 20.0, 0.05),
        _pos("B", 0.5, 20.0, 0.01),
    ]
    result = portfolio.multi_default_stress_test(
        positions, scenario_ns=(0, 1, 2), lgd=1.0)

    assert result["n_issuers"] == 2
    assert result["expected_defaults"] == pytest.approx(0.06)
    assert result["avg_issuer_weight"] == pytest.approx(0.5)

    by_n = {s["n_defaults"]: s for s in result["scenarios"]}
    # N=0: no loss, full weighted AEY (both issuers equal AEY -> 20%)
    assert by_n[0]["portfolio_return_pct"] == pytest.approx(20.0)
    assert by_n[0]["n_effective"] == 0
    assert by_n[0]["capped"] is False
    # N=1: worst-PD issuer (A, PD=0.05) defaults first. Survivor B alone
    # earns 20% on its own weight; loss = 1 * 0.5 * 1.0 * 100 = 50pp.
    assert by_n[1]["portfolio_return_pct"] == pytest.approx(20.0 - 50.0)
    assert by_n[1]["n_effective"] == 1
    # N=2: both default -- no survivors, loss = 2 * 0.5 * 1.0 * 100 = 100pp.
    assert by_n[2]["portfolio_return_pct"] == pytest.approx(0.0 - 100.0)
    assert by_n[2]["n_effective"] == 2
    assert by_n[2]["capped"] is False


def test_multi_default_caps_n_above_issuer_count():
    positions = [_pos("A", 1.0, 15.0, 0.02)]
    result = portfolio.multi_default_stress_test(positions, scenario_ns=(0, 5), lgd=1.0)
    by_n = {s["n_defaults"]: s for s in result["scenarios"]}
    assert by_n[5]["n_effective"] == 1  # capped at n_issuers=1
    assert by_n[5]["capped"] is True
    # Same result as N=1 since only 1 issuer exists to default
    by_n1 = portfolio.multi_default_stress_test(
        positions, scenario_ns=(1,), lgd=1.0)["scenarios"][0]
    assert by_n[5]["portfolio_return_pct"] == pytest.approx(by_n1["portfolio_return_pct"])


def test_multi_default_excludes_unrated_issuers_from_pd_ordering():
    positions = [
        _pos("A", 0.5, 20.0, 0.05),
        _pos("B", 0.5, 20.0, None),  # no rating -> no PD
    ]
    result = portfolio.multi_default_stress_test(positions, lgd=1.0)
    assert result["n_issuers"] == 1  # B excluded
    assert result["excluded_unrated"] == 1
    assert result["expected_defaults"] == pytest.approx(0.05)  # only A's PD


def test_multi_default_vs_ofz_and_cbr_deltas():
    positions = [_pos("A", 1.0, 25.0, 0.03)]
    result = portfolio.multi_default_stress_test(
        positions, scenario_ns=(0,), lgd=1.0, ofz_rate_pct=14.0, cbr_rate_pct=17.0)
    row = result["scenarios"][0]
    assert row["portfolio_return_pct"] == pytest.approx(25.0)
    assert row["vs_ofz_pp"] == pytest.approx(25.0 - 14.0)
    assert row["vs_cbr_pp"] == pytest.approx(25.0 - 17.0)


def test_multi_default_vs_ofz_none_when_ofz_rate_not_supplied():
    positions = [_pos("A", 1.0, 25.0, 0.03)]
    result = portfolio.multi_default_stress_test(positions, scenario_ns=(0,), lgd=1.0)
    assert result["scenarios"][0]["vs_ofz_pp"] is None
    assert result["break_even_n"] is None


def test_multi_default_break_even_verified_against_manual_calc():
    # Three issuers, equal weight (1/3 each), equal AEY 30%, distinct PDs
    # so ranking is deterministic. LGD=1.0 for round numbers.
    # avg_issuer_weight = 1.0 -> loss per default = 1 * (1/3) * 1.0 * 100 = 33.333...pp
    positions = [
        _pos("A", 1 / 3, 30.0, 0.10),  # worst PD, defaults first
        _pos("B", 1 / 3, 30.0, 0.05),
        _pos("C", 1 / 3, 30.0, 0.01),  # best PD, defaults last
    ]
    # Manual truth table (survivors always earn 30% on their own weight):
    #   N=0: return = 30.0                     (loss 0)
    #   N=1: return = 30.0 - 33.333.. = -3.33   (survivors B,C still 30%)
    #   N=2: return = 30.0 - 66.667.. = -36.67
    #   N=3: return =  0.0 - 100.0   = -100.0
    ofz = 0.0  # break-even against a 0% OFZ rate: only N=0 clears it
    result = portfolio.multi_default_stress_test(
        positions, scenario_ns=(0, 1, 2, 3), lgd=1.0, ofz_rate_pct=ofz)
    by_n = {s["n_defaults"]: s["portfolio_return_pct"] for s in result["scenarios"]}
    assert by_n[0] == pytest.approx(30.0)
    assert by_n[1] == pytest.approx(30.0 - 100.0 / 3)
    assert by_n[2] == pytest.approx(30.0 - 200.0 / 3)
    assert by_n[3] == pytest.approx(-100.0)
    assert result["break_even_n"] == 0
    assert result["break_even_probability"] == pytest.approx(
        portfolio.poisson_pmf(result["expected_defaults"], 0))

    # Raise the OFZ bar above what even N=0 clears -> never breaks even.
    result2 = portfolio.multi_default_stress_test(
        positions, scenario_ns=(0, 1, 2, 3), lgd=1.0, ofz_rate_pct=50.0)
    assert result2["break_even_n"] == -1
    assert result2["break_even_probability"] is None


def test_multi_default_break_even_at_higher_n():
    # Ten equal-weight issuers, low uniform PD, small LGD so more than one
    # default is still survivable -- break-even should land above 0.
    positions = [_pos(f"I{i}", 0.1, 30.0, 0.02) for i in range(10)]
    result = portfolio.multi_default_stress_test(
        positions, scenario_ns=(0, 1, 2, 3, 5, 8), lgd=default_probs.LGD,
        ofz_rate_pct=14.0)
    assert result["break_even_n"] is not None
    assert result["break_even_n"] >= 1
    # Verify against a manual scan of all N in [0, n_issuers]
    manual_be = -1
    ret0 = None
    for n in range(0, 11):
        s = portfolio.multi_default_stress_test(
            positions, scenario_ns=(n,), lgd=default_probs.LGD)["scenarios"][0]
        if n == 0:
            ret0 = s["portfolio_return_pct"]
        if s["portfolio_return_pct"] >= 14.0:
            manual_be = n
        else:
            break
    assert ret0 >= 14.0  # sanity: this fixture is meant to start above OFZ
    assert result["break_even_n"] == manual_be
