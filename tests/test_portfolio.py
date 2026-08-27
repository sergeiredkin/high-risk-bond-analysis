"""Tests for portfolio.get_rate_scenarios/bond_aey/pd_adjusted_aey. Uses a
real (tmp-file) SQLite DB via db.connect(), same pattern as test_slice1.py --
no network.
"""
import json
import math
import sys
from datetime import date, timedelta
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


# ============================================================================
# Portfolio presets (Portfolio presets task, 2026-08-27)
# ============================================================================

SECTORS = ["finance", "retail", "energy", "industrial", "transport",
           "construction", "telecom", "agriculture", "leasing", "metals"]


def _preset_con(tmp_path):
    con = db.connect(tmp_path / "presets.sqlite")
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_cbr(con)
    db.init_portfolio(con)
    return con


def _seed_bond(con, idx, rating="ruBBB+", health="green", sector=None,
                ytm=0.20, g_spread_bp=500.0, mod_dur=1.5,
                prior_g_spread_bp=None, mat_date="2028-01-01"):
    """One issuer + one tier=1 bond with a current rating and a latest
    spread_snapshot row (plus an optional ~30d-earlier one, for the
    G-spread-delta tests). Returns the bond's isin."""
    sector = sector or SECTORS[idx % len(SECTORS)]
    issuer_id = f"ISS{idx:04d}"
    secid = f"SECID{idx:04d}"
    isin = f"RU{idx:06d}"
    db.add_issuer_at_tier(con, issuer_id, f"Issuer {idx}", watchlist_tier=1, sector=sector)
    db.set_issuer_health_status(con, issuer_id, health)
    db.upsert_rating(con, issuer_id=issuer_id, agency="expert_ra", rating=rating,
                      outlook=None, action=None, rated_on="2026-01-01")
    db.add_issuer_bond(con, issuer_id=issuer_id, secid=secid, source="manual")
    db.upsert_bond(con, secid=secid, isin=isin, issuer_id=issuer_id,
                    short_name=f"Bond{idx}", mat_date=mat_date, face_value=1000.0,
                    face_unit="SUR", coupon_pct=15.0, list_level=3, qualified=1,
                    updated="2026-08-01")
    if prior_g_spread_bp is not None:
        db.insert_spread_snapshot(con, secid=secid, snap_date="2026-07-27", ytm=ytm,
                                   tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=prior_g_spread_bp,
                                   mod_dur=mod_dur, convexity=None)
    db.insert_spread_snapshot(con, secid=secid, snap_date="2026-08-26", ytm=ytm,
                               tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=g_spread_bp,
                               mod_dur=mod_dur, convexity=None)
    con.commit()
    return isin


def _seed_watchlist(con, n, rating="ruBBB+", health="green", ytm=0.20,
                     g_spread_bp=500.0, start=0):
    """n bonds cycling across SECTORS, all otherwise identical -- the
    "happy path, plenty of diversified candidates" fixture shape."""
    return [_seed_bond(con, start + i, rating=rating, health=health, ytm=ytm,
                        g_spread_bp=g_spread_bp)
            for i in range(n)]


# ---- compute_weights() -----------------------------------------------------

def _bond(isin, aey):
    return portfolio.BondCandidate(
        isin=isin, secid=isin, issuer_id=isin, issuer_name=isin, sector="x",
        rating="ruBBB+", health="green", aey=aey, ytm=0.2, g_spread_bp=500.0,
        g_spread_delta_30d_bp=0.0, mat_date=None, offer_date=None)


def test_compute_weights_equal_sums_to_one():
    bonds = [_bond("A", 0.10), _bond("B", 0.20), _bond("C", 0.30)]
    w = portfolio.compute_weights(bonds, mode="equal")
    assert w == {"A": pytest.approx(1 / 3), "B": pytest.approx(1 / 3), "C": pytest.approx(1 / 3)}
    assert sum(w.values()) == pytest.approx(1.0)


def test_compute_weights_aey_high_favors_higher_aey():
    bonds = [_bond("A", 0.10), _bond("B", 0.30)]
    w = portfolio.compute_weights(bonds, mode="aey_high")
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["B"] > w["A"]
    assert w["B"] == pytest.approx(0.75)  # 0.30 / (0.10+0.30)
    assert w["A"] == pytest.approx(0.25)


def test_compute_weights_aey_low_favors_lower_aey():
    bonds = [_bond("A", 0.10), _bond("B", 0.30)]
    w = portfolio.compute_weights(bonds, mode="aey_low")
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["A"] > w["B"]
    # inverse-proportional: (1/0.10)/((1/0.10)+(1/0.30)) = 0.75
    assert w["A"] == pytest.approx(0.75)
    assert w["B"] == pytest.approx(0.25)


def test_compute_weights_empty_list_raises():
    with pytest.raises(ValueError):
        portfolio.compute_weights([], mode="equal")


def test_compute_weights_unknown_mode_raises():
    with pytest.raises(ValueError):
        portfolio.compute_weights([_bond("A", 0.1)], mode="bogus")


def test_compute_weights_non_positive_aey_does_not_crash():
    # aey_low's 1/aey would blow up (aey=0) or flip sign (aey<0) without the
    # EPS_AEY floor -- must still return a valid, non-negative weight dict.
    bonds = [_bond("A", 0.0), _bond("B", -0.05), _bond("C", 0.20)]
    w = portfolio.compute_weights(bonds, mode="aey_low")
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(v >= 0 for v in w.values())


# ---- red_issuer_eligibility() ----------------------------------------------

def test_red_eligible_good_rating_low_pd_stable_spread():
    ok, reason = portfolio.red_issuer_eligibility("ruBB+", g_spread_delta_30d_bp=10.0)
    assert ok is True
    assert "ruBB+" in reason and "PD" in reason and "G-spread" in reason


def test_red_excluded_rating_below_floor():
    ok, reason = portfolio.red_issuer_eligibility("ruB+", g_spread_delta_30d_bp=10.0)
    assert ok is False
    assert reason is None


def test_red_excluded_unrated():
    ok, _reason = portfolio.red_issuer_eligibility(None, g_spread_delta_30d_bp=10.0)
    assert ok is False


def test_red_excluded_pd_too_high():
    # ruBB- itself has yr1 PD 5.98% > the 3% ceiling -- passes the rating
    # floor exactly but must still fail on PD.
    ok, _reason = portfolio.red_issuer_eligibility("ruBB-", g_spread_delta_30d_bp=10.0)
    assert ok is False


def test_red_excluded_gspread_spike():
    # ruBB+ (PD 2.95%) clears the PD gate but a >=200bp 30d widening must
    # still exclude it.
    ok, _reason = portfolio.red_issuer_eligibility("ruBB+", g_spread_delta_30d_bp=250.0)
    assert ok is False


def test_red_excluded_gspread_delta_missing():
    ok, _reason = portfolio.red_issuer_eligibility("ruBB+", g_spread_delta_30d_bp=None)
    assert ok is False


# ---- refinancing_flag() ----------------------------------------------------

def test_refi_flag_within_window():
    as_of = date(2026, 8, 26)
    flag, refi_date = portfolio.refinancing_flag("2026-12-15", None, as_of=as_of)
    assert flag is True
    assert refi_date == "2026-12-15"


def test_refi_flag_outside_window():
    as_of = date(2026, 8, 26)
    flag, refi_date = portfolio.refinancing_flag("2028-01-01", None, as_of=as_of)
    assert flag is False
    assert refi_date is None


def test_refi_flag_no_dates():
    flag, refi_date = portfolio.refinancing_flag(None, None)
    assert flag is False
    assert refi_date is None


def test_refi_flag_uses_earliest_of_maturity_and_offer():
    as_of = date(2026, 8, 26)
    # offer (put) date is the sooner of the two -- must drive the flag even
    # though maturity itself is years away.
    flag, refi_date = portfolio.refinancing_flag("2030-01-01", "2026-10-01", as_of=as_of)
    assert flag is True
    assert refi_date == "2026-10-01"


# ---- build_preset() ---------------------------------------------------------

def test_build_preset_requires_db():
    with pytest.raises(ValueError):
        portfolio.build_preset("conservative")


def test_build_preset_unknown_type_raises(tmp_path):
    con = _preset_con(tmp_path)
    with pytest.raises(ValueError):
        portfolio.build_preset("yolo", db=con)


def test_build_preset_dry_run_success(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    result = portfolio.build_preset("conservative", sizing_mode="equal", db=con, dry_run=True)
    assert result == {"success": True, "bond_count": 25, "reason": None}


def test_build_preset_dry_run_failure_never_raises(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 5, rating="ruBBB+", health="green", ytm=0.20)  # need 20
    result = portfolio.build_preset("conservative", sizing_mode="equal", db=con, dry_run=True)
    assert result["success"] is False
    assert result["bond_count"] == 0
    assert "conservative" in result["reason"]


def test_build_preset_dry_run_unknown_preset_type_returns_failure(tmp_path):
    con = _preset_con(tmp_path)
    result = portfolio.build_preset("yolo", db=con, dry_run=True)
    assert result["success"] is False
    assert result["reason"] is not None


def test_build_preset_dry_run_writes_nothing(tmp_path):
    """dry_run is a pure computation, same as a normal build_preset() call
    -- save_portfolio() is the only writer in this module."""
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    portfolio.build_preset("conservative", db=con, dry_run=True)
    assert db.list_portfolios(con) == []


def test_build_preset_conservative_happy_path(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    result = portfolio.build_preset("conservative", sizing_mode="equal", db=con)
    n_min, n_max = portfolio.PRESETS["conservative"]["bond_count"]
    assert n_min <= result["summary"]["n_bonds"] <= n_max
    assert sum(result["weights"].values()) == pytest.approx(1.0)
    for c in result["bonds"]:
        assert c.health in ("green", "yellow")
        assert c.ytm <= 0.30
        assert c.aey <= 0.35


def test_build_preset_balanced_happy_path(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 30, rating="ruBB", health="yellow", ytm=0.25)
    result = portfolio.build_preset("balanced", sizing_mode="equal", db=con)
    n_min, n_max = portfolio.PRESETS["balanced"]["bond_count"]
    assert n_min <= result["summary"]["n_bonds"] <= n_max
    for c in result["bonds"]:
        assert c.health in ("green", "yellow", "orange")
        assert c.ytm <= 0.35


def test_build_preset_aggressive_happy_path(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 35, rating="ruB+", health="orange", ytm=0.35)
    result = portfolio.build_preset("aggressive", sizing_mode="equal", db=con)
    n_min, n_max = portfolio.PRESETS["aggressive"]["bond_count"]
    assert n_min <= result["summary"]["n_bonds"] <= n_max
    for c in result["bonds"]:
        assert c.ytm <= 0.40


def test_build_preset_fewer_than_min_raises(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 5, rating="ruBBB+", health="green", ytm=0.20)  # need 20
    with pytest.raises(ValueError):
        portfolio.build_preset("conservative", db=con)


def test_build_preset_red_issuer_included_when_eligible(tmp_path):
    con = _preset_con(tmp_path)
    # 40, not the 28-bond floor: aggressive's max_per_issuer=0.03 combined
    # with one-bond-per-issuer equal weighting needs N>=34 (1/34<3%) to be
    # satisfiable at all -- 28 alone (equal weight 3.57%) is mathematically
    # incompatible with its own 3% cap under equal sizing, a real tension in
    # the preset's own numbers, not a fixture bug. See build_preset()'s cap
    # enforcement, which correctly raises rather than silently violating the
    # cap when a caller's candidate pool can't support it.
    _seed_watchlist(con, 40, rating="ruB+", health="orange", ytm=0.35)
    # One RED issuer clearing every red_issuer_eligibility gate: rating
    # ruBB+ (>= floor, PD 2.95% < 3%), G-spread flat (no spike). ytm=0.38
    # (higher than the base pool's 0.35) so its AEY ranks near the top and
    # it isn't crowded out of the top-35-by-AEY cut by sheer pool size --
    # this test is about the eligibility gate, not AEY ranking.
    _seed_bond(con, 900, rating="ruBB+", health="red", ytm=0.38,
               g_spread_bp=510.0, prior_g_spread_bp=500.0, sector="finance")
    result = portfolio.build_preset("aggressive", db=con)
    red_bonds = [c for c in result["bonds"] if c.health == "red"]
    assert len(red_bonds) == 1
    assert red_bonds[0].flags.get("red_included") is True
    assert "ruBB+" in red_bonds[0].flags["red_reason"]
    assert result["summary"]["n_red_included"] == 1


def test_build_preset_red_issuer_excluded_when_gspread_spikes(tmp_path):
    con = _preset_con(tmp_path)
    # 40, not the 28-bond floor: aggressive's max_per_issuer=0.03 combined
    # with one-bond-per-issuer equal weighting needs N>=34 (1/34<3%) to be
    # satisfiable at all -- 28 alone (equal weight 3.57%) is mathematically
    # incompatible with its own 3% cap under equal sizing, a real tension in
    # the preset's own numbers, not a fixture bug. See build_preset()'s cap
    # enforcement, which correctly raises rather than silently violating the
    # cap when a caller's candidate pool can't support it.
    _seed_watchlist(con, 40, rating="ruB+", health="orange", ytm=0.35)
    # Same eligible rating/PD as the included case above, but a >=200bp 30d
    # widening must still keep it out.
    _seed_bond(con, 900, rating="ruBB+", health="red", ytm=0.30,
               g_spread_bp=800.0, prior_g_spread_bp=500.0, sector="finance")
    result = portfolio.build_preset("aggressive", db=con)
    red_bonds = [c for c in result["bonds"] if c.health == "red"]
    assert len(red_bonds) == 0


def test_build_preset_red_not_allowed_outside_aggressive(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    _seed_bond(con, 900, rating="ruBB+", health="red", ytm=0.20,
               g_spread_bp=510.0, prior_g_spread_bp=500.0, sector="finance")
    result = portfolio.build_preset("conservative", db=con)
    assert all(c.health != "red" for c in result["bonds"])


def test_build_preset_sizing_modes_all_valid(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    for mode in ("equal", "aey_high", "aey_low"):
        result = portfolio.build_preset("conservative", sizing_mode=mode, db=con)
        assert sum(result["weights"].values()) == pytest.approx(1.0)
        assert result["sizing_mode"] == mode


def test_build_preset_sector_cap_enforced(tmp_path):
    con = _preset_con(tmp_path)
    # 20 bonds all in ONE sector (would be 100% concentrated) plus enough
    # other-sector bonds to still reach the 20-bond floor after the cap
    # trims the dominant sector down to <=12%.
    for i in range(20):
        _seed_bond(con, i, rating="ruBBB+", health="green", ytm=0.20, sector="finance")
    for i in range(20, 40):
        _seed_bond(con, i, rating="ruBBB+", health="green", ytm=0.20,
                   sector=SECTORS[i % len(SECTORS)])
    result = portfolio.build_preset("conservative", sizing_mode="equal", db=con)
    by_sector: dict[str, float] = {}
    for c in result["bonds"]:
        by_sector[c.sector] = by_sector.get(c.sector, 0.0) + result["weights"][c.isin]
    max_cap = portfolio.PRESETS["conservative"]["sector_cap"]
    for sector, w in by_sector.items():
        assert w <= max_cap + 1e-9, f"{sector} at {w:.3f} exceeds cap {max_cap}"


def test_build_preset_max_per_issuer_enforced(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    result = portfolio.build_preset("conservative", sizing_mode="equal", db=con)
    by_issuer: dict[str, float] = {}
    for c in result["bonds"]:
        by_issuer[c.issuer_id] = by_issuer.get(c.issuer_id, 0.0) + result["weights"][c.isin]
    max_cap = portfolio.PRESETS["conservative"]["max_per_issuer"]
    for w in by_issuer.values():
        assert w <= max_cap + 1e-9


def test_build_preset_user_filters_override_sector_cap(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    result = portfolio.build_preset(
        "conservative", db=con, user_filters={"sector_cap": 0.30})
    assert result["filters"]["sector_cap"] == 0.30


# ---- save_portfolio() + DB lifecycle ---------------------------------------

def test_save_portfolio_round_trip(tmp_path):
    con = _preset_con(tmp_path)
    _seed_watchlist(con, 25, rating="ruBBB+", health="green", ytm=0.20)
    built = portfolio.build_preset("conservative", db=con)
    portfolio_id = portfolio.save_portfolio(con, "Тест", built)
    row = db.get_portfolio(con, portfolio_id)
    assert row["status"] == "active"
    assert row["preset_type"] == "conservative"
    bonds = db.list_portfolio_bonds(con, portfolio_id)
    assert len(bonds) == built["summary"]["n_bonds"]
    for b in bonds:
        flags = json.loads(b["flags_json"])
        assert "gspread_baseline_bp" in flags
    snaps = con.execute(
        "SELECT * FROM portfolio_snapshot WHERE portfolio_id=?", (portfolio_id,)).fetchall()
    assert len(snaps) == 1


def test_init_portfolio_idempotent(tmp_path):
    con = db.connect(tmp_path / "idem.sqlite")
    db.init_db(con)
    db.init_portfolio(con)
    db.init_portfolio(con)  # must not raise
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio'").fetchone()
    assert row is not None


def test_portfolio_cascade_delete(tmp_path):
    con = db.connect(tmp_path / "cascade.sqlite")
    db.init_db(con)
    db.init_portfolio(con)
    pid = db.create_portfolio(con, name="X", preset_type="custom", status="active")
    db.add_portfolio_bond(con, portfolio_id=pid, isin="RU000001", weight=1.0)
    db.add_portfolio_snapshot(con, portfolio_id=pid, snapshot_date="2026-08-26",
                               aey=0.18, expected_loss=0.01)
    con.commit()
    con.execute("DELETE FROM portfolio WHERE id=?", (pid,))
    con.commit()
    assert db.list_portfolio_bonds(con, pid) == []
    snaps = con.execute(
        "SELECT * FROM portfolio_snapshot WHERE portfolio_id=?", (pid,)).fetchall()
    assert snaps == []


def test_portfolio_status_lifecycle(tmp_path):
    con = db.connect(tmp_path / "lifecycle.sqlite")
    db.init_db(con)
    db.init_portfolio(con)
    pid = db.create_portfolio(con, name="X", preset_type="custom")  # default draft
    assert db.get_portfolio(con, pid)["status"] == "draft"
    db.set_portfolio_status(con, pid, "active")
    assert db.get_portfolio(con, pid)["status"] == "active"
    db.set_portfolio_status(con, pid, "archived")
    assert db.get_portfolio(con, pid)["status"] == "archived"
    with pytest.raises(ValueError):
        db.set_portfolio_status(con, pid, "bogus")
    with pytest.raises(ValueError):
        db.set_portfolio_status(con, 999999, "active")


def test_create_portfolio_rejects_invalid_preset_type(tmp_path):
    con = db.connect(tmp_path / "invalid.sqlite")
    db.init_db(con)
    db.init_portfolio(con)
    with pytest.raises(ValueError):
        db.create_portfolio(con, name="X", preset_type="not_a_real_preset")


def test_rating_rank_matches_dashboard_export_scale():
    # scripts/ isn't importable as a package from src/hybond's tests without
    # path surgery, and portfolio.RATING_RANK is a deliberate, documented
    # duplicate of export_dashboard_data.RATING_TREND_VALUE (see that
    # constant's own comment) -- load the script module directly here so a
    # future edit to either table that drifts the other is caught.
    import importlib.util
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "export_dashboard_data.py"
    spec = importlib.util.spec_from_file_location("_export_dashboard_data_ratingcheck", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    assert portfolio.RATING_RANK == mod.RATING_TREND_VALUE


# ---- monitor_portfolios() (Portfolio Monitoring task, Phase B) ------------

def _seed_active_portfolio(con, isin, weight=1.0, gspread_baseline_bp=500.0,
                            health_at_save="green", red_included=False,
                            name="Test Portfolio"):
    """A minimal one-bond active portfolio, built directly (not via
    build_preset()) so each monitor_portfolios() test can control exactly
    the baseline/health-at-save state it needs, independent of the real
    selection algorithm."""
    portfolio_id = db.create_portfolio(con, name=name, preset_type="conservative",
                                        sizing_mode="equal", status="active")
    flags = {"gspread_baseline_bp": gspread_baseline_bp,
              "gspread_baseline_date": "2026-08-01", "health_at_save": health_at_save}
    if red_included:
        flags["red_included"] = True
        flags["red_reason"] = "test fixture"
    db.add_portfolio_bond(con, portfolio_id=portfolio_id, isin=isin, weight=weight,
                           flags_json=json.dumps(flags))
    con.commit()
    return portfolio_id


def _flags_for_portfolio(con, portfolio_id):
    rows = con.execute(
        "SELECT * FROM flag_log WHERE detector='portfolio_monitor' "
        "AND json_extract(detail, '$.portfolio_id')=?", (portfolio_id,)).fetchall()
    return [dict(r) for r in rows]


def test_flag_log_severity_column_retrofit_is_idempotent(tmp_path):
    """init_db() must add flag_log.severity via a conditional ALTER TABLE
    (PRAGMA table_info check) rather than assuming a fresh CREATE TABLE, and
    running it twice (fresh install re-init, or an existing DB from before
    this task) must not raise."""
    con = db.connect(tmp_path / "retrofit.sqlite")
    db.init_db(con)
    db.init_db(con)  # idempotent re-init must not raise
    cols = {r["name"] for r in con.execute("PRAGMA table_info(flag_log)")}
    assert "severity" in cols
    db.insert_flag(con, snap_date="2026-08-27", secid=None, issuer_id=None,
                    detector="portfolio_monitor", signal="portfolio_gspread_alert",
                    metric=None, detail="{}", driving=0, severity="high")
    row = con.execute(
        "SELECT severity FROM flag_log WHERE signal='portfolio_gspread_alert'").fetchone()
    assert row["severity"] == "high"


def test_monitor_portfolios_gspread_alert_over_threshold(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, g_spread_bp=700.0)  # baseline 500 -> delta +200bp
    pid = _seed_active_portfolio(con, isin, gspread_baseline_bp=500.0)

    alerts = portfolio.monitor_portfolios(con)
    assert len(alerts[pid]) == 1
    assert alerts[pid][0]["flag_type"] == portfolio.FLAG_TYPE_GSPREAD
    assert alerts[pid][0]["severity"] == "high"

    rows = _flags_for_portfolio(con, pid)
    assert len(rows) == 1
    assert rows[0]["signal"] == "portfolio_gspread_alert"
    assert rows[0]["severity"] == "high"
    detail = json.loads(rows[0]["detail"])
    assert detail["delta_bp"] == pytest.approx(200.0)


def test_monitor_portfolios_gspread_no_alert_under_threshold(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, g_spread_bp=600.0)  # delta +100bp, under 150bp
    pid = _seed_active_portfolio(con, isin, gspread_baseline_bp=500.0)

    alerts = portfolio.monitor_portfolios(con)
    assert alerts[pid] == []
    assert _flags_for_portfolio(con, pid) == []


def test_monitor_portfolios_health_degradation(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, health="orange")
    pid = _seed_active_portfolio(con, isin, health_at_save="green")

    alerts = portfolio.monitor_portfolios(con)
    types = {a["flag_type"] for a in alerts[pid]}
    assert portfolio.FLAG_TYPE_HEALTH in types
    health_alert = next(a for a in alerts[pid] if a["flag_type"] == portfolio.FLAG_TYPE_HEALTH)
    assert health_alert["detail"]["health_before"] == "green"
    assert health_alert["detail"]["health_after"] == "orange"


def test_monitor_portfolios_health_improvement_no_alert(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, health="green")
    pid = _seed_active_portfolio(con, isin, health_at_save="orange")

    alerts = portfolio.monitor_portfolios(con)
    assert alerts[pid] == []


def test_monitor_portfolios_refinancing_window(tmp_path):
    con = _preset_con(tmp_path)
    soon = (date.today() + timedelta(days=20)).isoformat()
    isin = _seed_bond(con, 1, mat_date=soon)
    pid = _seed_active_portfolio(con, isin)

    alerts = portfolio.monitor_portfolios(con)
    types = {a["flag_type"] for a in alerts[pid]}
    assert portfolio.FLAG_TYPE_REFI in types
    refi_alert = next(a for a in alerts[pid] if a["flag_type"] == portfolio.FLAG_TYPE_REFI)
    assert refi_alert["severity"] == "low"


def test_monitor_portfolios_refinancing_outside_window_no_alert(tmp_path):
    con = _preset_con(tmp_path)
    far = (date.today() + timedelta(days=365)).isoformat()
    isin = _seed_bond(con, 1, mat_date=far)
    pid = _seed_active_portfolio(con, isin)

    alerts = portfolio.monitor_portfolios(con)
    assert alerts[pid] == []


def test_monitor_portfolios_red_status_change(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, health="red")
    pid = _seed_active_portfolio(con, isin, health_at_save="orange")

    alerts = portfolio.monitor_portfolios(con)
    types = [a["flag_type"] for a in alerts[pid]]
    # Both fire for this transition -- see monitor_portfolios()'s own
    # docstring on why FLAG_TYPE_HEALTH and FLAG_TYPE_RED are independent.
    assert portfolio.FLAG_TYPE_RED in types
    assert portfolio.FLAG_TYPE_HEALTH in types
    red_alert = next(a for a in alerts[pid] if a["flag_type"] == portfolio.FLAG_TYPE_RED)
    assert red_alert["severity"] == "high"


def test_monitor_portfolios_already_red_no_red_change_alert(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, health="red")
    pid = _seed_active_portfolio(con, isin, health_at_save="red")

    alerts = portfolio.monitor_portfolios(con)
    types = [a["flag_type"] for a in alerts[pid]]
    assert portfolio.FLAG_TYPE_RED not in types


def test_monitor_portfolios_clean_state_no_alerts(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, g_spread_bp=500.0, health="green",
                       mat_date="2030-01-01")
    pid = _seed_active_portfolio(con, isin, gspread_baseline_bp=500.0, health_at_save="green")

    alerts = portfolio.monitor_portfolios(con)
    assert alerts[pid] == []


def test_monitor_portfolios_ignores_draft_and_archived_portfolios(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1, g_spread_bp=900.0)
    draft_id = db.create_portfolio(con, name="Draft", preset_type="conservative",
                                    sizing_mode="equal", status="draft")
    db.add_portfolio_bond(con, portfolio_id=draft_id, isin=isin, weight=1.0,
                           flags_json=json.dumps({"gspread_baseline_bp": 100.0,
                                                     "health_at_save": "green"}))
    con.commit()

    alerts = portfolio.monitor_portfolios(con)
    assert draft_id not in alerts


def test_monitor_portfolios_writes_portfolio_snapshot(tmp_path):
    con = _preset_con(tmp_path)
    isin = _seed_bond(con, 1)
    pid = _seed_active_portfolio(con, isin)

    portfolio.monitor_portfolios(con)
    snaps = con.execute(
        "SELECT * FROM portfolio_snapshot WHERE portfolio_id=?", (pid,)).fetchall()
    assert len(snaps) == 1
    metrics = json.loads(snaps[0]["metrics_json"])
    assert "avg_aey" in metrics and "expected_loss" in metrics


def test_monitor_portfolios_skips_bond_with_missing_isin(tmp_path):
    """A bond removed from the `bond` table entirely (not just delisted from
    the watchlist) since save must not crash the whole monitoring run."""
    con = _preset_con(tmp_path)
    pid = _seed_active_portfolio(con, "RU_GHOST_ISIN")

    alerts = portfolio.monitor_portfolios(con)
    assert alerts[pid] == []
