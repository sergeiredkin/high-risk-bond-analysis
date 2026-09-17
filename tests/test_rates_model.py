"""Model and export changes behind the Ставка и ОФЗ page (plan rev 2, approved
2026-09-17):

* rate scenarios from the CBR's own publications (captain's Q1: stress =
  «Проинфляционный», bull = «Дезинфляционный», «Рисковый» in its own column);
* OFZ horizon returns priced off each issue's cash flows (plan findings O1-O4);
* the premium to OFZ computed with one AEY formula on both legs (finding P1);
* the additive export keys the page reads (DATA.expectations, zcyc,
  rates_premium, ofz.bonds[].returns/benchmark, aey_risky_pct).
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond import ofz_pipeline, portfolio  # noqa: E402
from hybond.compute import bondmath, ofz_carry  # noqa: E402
from hybond.store import db  # noqa: E402
from export_dashboard_data import (  # noqa: E402
    _bonds_for_snap_date, expectations_export, ofz_bundle, rates_premium_export, zcyc_export)

TODAY = date(2026, 9, 17)


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_cbr(con)
    db.init_rates(con)
    db.init_ofz(con)
    db.init_universe(con)
    return con


def _seed_publications(con, with_ondkp=True):
    db.upsert_cbr_meeting(con, meeting_date="2026-07-24", rate_after=14.0, rate_change_bp=-25,
                          rate_2026_mid="14,5–14,6", rate_2027_mid="10,5–12,5", rate_2029_mid="7,5–8,5",
                          analysis_json="{}")
    fc = "https://www.cbr.ru/Content/Document/File/194258/forecast_260724.pdf"
    db.replace_rate_expectations(con, "cbr_forecast", "2026-07-24", "2026-07-24", [
        {"scenario": "base", "year": 2026, "lo": 14.5, "hi": 14.6, "url": fc},
        {"scenario": "base", "year": 2027, "lo": 10.5, "hi": 12.5, "url": fc},
        {"scenario": "base", "year": 2029, "lo": 7.5, "hi": 8.5, "url": fc},
        {"scenario": "base", "period": "rest_of_year", "year": 2026, "lo": 13.7, "hi": 14.0,
         "ytd_avg": 15.0, "rest_from": "2026-07-27", "url": fc},
    ])
    if with_ondkp:
        on = "https://www.cbr.ru/about_br/publ/ondkp/on_2027_2029/"
        db.replace_rate_expectations(con, "ondkp", "on_2027_2029", "2026-08-31", [
            {"scenario": "base", "year": 2027, "lo": 10.5, "hi": 12.5, "url": on},
            {"scenario": "disinflation", "year": 2027, "lo": 9.0, "hi": 11.0, "url": on},
            {"scenario": "proinflation", "year": 2027, "lo": 13.0, "hi": 15.0, "url": on},
            {"scenario": "risk", "year": 2027, "lo": 19.0, "hi": 21.0, "url": on},
        ], status="draft")
    db.replace_rate_expectations(con, "survey", "2026-09", "2026-09-01", [
        {"scenario": "median", "year": 2027, "lo": 12.43, "hi": 12.43},
        {"scenario": "q1", "year": 2027, "lo": 11.9, "hi": 11.9},
        {"scenario": "median", "year": 2023, "lo": 7.0, "hi": 7.0},      # finished year, trimmed on export
    ])
    db.replace_rate_expectations(con, "survey", "2021-05", "2021-05-01", [
        {"scenario": "median", "year": 2021, "lo": 5.2, "hi": 5.2}])
    db.upsert_key_rates(con, [("2026-07-27", 14.0), ("2026-09-17", 14.0)])
    con.commit()


# ---- scenarios -------------------------------------------------------------------

def test_scenarios_come_from_cbr_publications(tmp_path):
    con = _con(tmp_path)
    _seed_publications(con)
    s = portfolio.get_rate_scenarios(con, today=TODAY)
    assert s["scenario_source"] == "cbr_publications" and s["is_fallback"] is False
    assert s["horizon_year"] == 2027
    assert s["base"] == pytest.approx(-0.025)       # 11.5 − 14.0
    assert s["stress"] == pytest.approx(0.0)        # Проинфляционный 14.0 − 14.0 (not the old +60 artefact)
    assert s["bull"] == pytest.approx(-0.04)        # Дезинфляционный 10.0 − 14.0
    assert s["risky"] == pytest.approx(0.06)        # Рисковый 20.0 − 14.0
    assert s["analysts"] == pytest.approx(-0.0157)  # survey median 12.43 − 14.0
    assert s["levels"]["stress"]["status"] == "draft"
    assert s["cbr_2027"] == "10,5–12,5"             # legacy provenance keys survive
    con.close()


def test_scenarios_fall_back_to_the_meeting_row_without_ondkp(tmp_path):
    con = _con(tmp_path)
    _seed_publications(con, with_ondkp=False)
    s = portfolio.get_rate_scenarios(con, today=TODAY)
    assert s["scenario_source"] == "cbr_meeting"
    assert s["risky"] is None
    assert s["stress"] == pytest.approx(0.006)      # the old definition, clearly labelled
    con.close()


def test_scenario_horizon_moves_with_the_calendar_year(tmp_path):
    con = _con(tmp_path)
    _seed_publications(con)
    # In 2027 the horizon is 2028, which nothing seeded covers: no silent reuse of 2027.
    assert portfolio.get_rate_scenarios(con, today=date(2027, 1, 15))["scenario_source"] == "cbr_meeting"
    con.close()


def test_current_key_rate_prefers_the_newer_source(tmp_path):
    con = _con(tmp_path)
    db.upsert_cbr_meeting(con, meeting_date="2026-09-11", rate_after=14.0)
    db.upsert_key_rates(con, [("2026-09-10", 14.0)])
    assert portfolio.current_key_rate(con) == ("2026-09-11", 14.0)
    db.upsert_key_rates(con, [("2026-10-26", 13.5)])
    assert portfolio.current_key_rate(con) == ("2026-10-26", 13.5)
    con.close()


# ---- OFZ horizon returns ---------------------------------------------------------------

def test_horizon_return_constant_yield_zero_coupon_earns_the_yield():
    flows = [(3.0, 1000.0)]
    price = bondmath.price_from_ytm(0.15, flows)
    r = ofz_carry.horizon_return(cashflows=flows, dirty_price=price, ytm=0.15, delta_y=0.0, horizon_years=1)
    assert r["total_return_pct"] == pytest.approx(15.0, abs=0.01)
    assert r["cash_received_pct"] == 0 and r["repricing_pct"] == 0
    assert r["pull_to_par_pct"] == pytest.approx(15.0, abs=0.01)


def test_horizon_return_includes_pull_to_par_the_old_carry_model_missed():
    """Regression for finding O1: ОФЗ 26232 (6% coupon, price 92.31, YTM
    15.9%) showed a 7.5% one-year return under the old coupon/price model."""
    flows = [(0.05, 30.0), (0.55, 30.0), (1.05, 1030.0)]
    price = 923.1
    ytm = bondmath.ytm_from_price(price, flows)
    new = ofz_carry.horizon_return(cashflows=flows, dirty_price=price, ytm=ytm, delta_y=-0.01, horizon_years=1)
    old = ofz_carry.project_horizon(annual_coupon_pct=6.0, dirty_price_pct=92.31, mod_dur=0.9,
                                    convexity=None, total_delta_y=-0.025, horizon_years=1)
    assert old["total_return_pct"] < 9
    assert new["total_return_pct"] > 15
    parts = new["cash_received_pct"] + new["pull_to_par_pct"] + new["repricing_pct"]
    assert parts == pytest.approx(new["total_return_pct"], abs=0.01)


def test_horizon_return_is_none_once_the_issue_has_matured():
    flows = [(0.5, 30.0), (1.05, 1030.0)]
    assert ofz_carry.horizon_return(cashflows=flows, dirty_price=950, ytm=0.15, delta_y=0, horizon_years=2) is None
    with pytest.raises(ValueError):
        ofz_carry.horizon_return(cashflows=flows, dirty_price=0, ytm=0.15, delta_y=0, horizon_years=1)


def _ofz(con, secid, **kw):
    row = dict(secid=secid, snap_date="2026-09-17", short_name=f"ОФЗ {secid[2:7]}", isin=secid,
               board_id="TQOB", instrument_type="ПД", mat_date="2027-10-06", coupon_pct=6.0, face_value=1000.0,
               last_pct=92.31, accrued_int_pct=None, dirty_price_pct=92.31, ytm=0.1641, mod_dur=0.864,
               convexity=1.5, tenor_yrs=1.05, volume=1e6, num_trades=100, yield_source="computed",
               cashflows_json=json.dumps([[0.05, 30.0], [0.55, 30.0], [1.05, 1030.0]]))
    row.update(kw)
    db.upsert_ofz_snapshot(con, **row)


def test_linkers_and_floaters_are_never_benchmarks(tmp_path):
    con = _con(tmp_path)
    _ofz(con, "SU26232RMFS7")
    _ofz(con, "SU52002RMFS1", instrument_type="ИН", yield_source="moex", ytm=0.071, cashflows_json=None)
    _ofz(con, "SU29021RMFS1", instrument_type="ПК", yield_source="moex", ytm=None, mod_dur=None,
         cashflows_json=None)
    con.commit()
    rows = {r["secid"]: r for r in db.ofz_snapshot_rows(con, "2026-09-17")}
    assert ofz_pipeline.is_benchmark(rows["SU26232RMFS7"])
    assert not ofz_pipeline.is_benchmark(rows["SU52002RMFS1"])
    assert not ofz_pipeline.is_benchmark(rows["SU29021RMFS1"])
    returns = ofz_pipeline.horizon_returns_for_snapshot(con, "2026-09-17", {"base": -0.025, "risky": 0.06})
    assert set(returns) == {"SU26232RMFS7"}
    base = returns["SU26232RMFS7"]["base"]
    assert base[0]["horizon_years"] == 1 and "total_return_pct" in base[0]
    assert base[1] == {"horizon_years": 2, "matured": True}        # finding O2: no "3-year" return past maturity
    assert base[2] == {"horizon_years": 3, "matured": True}
    assert set(returns["SU26232RMFS7"]) == {"base", "risky"}         # only scenarios with a delta
    con.close()


def test_ofz_bundle_adds_returns_without_touching_legacy_fields(tmp_path):
    con = _con(tmp_path)
    _ofz(con, "SU26232RMFS7", tenor_yrs=1.05)
    con.commit()
    bundle = ofz_bundle(con, {"base": -0.025, "stress": 0.0, "bull": -0.04, "risky": 0.06, "analysts": -0.0157,
                              "is_fallback": False, "source_date": "2026-07-24"})
    b = bundle["bonds"][0]
    assert b["benchmark"] is True and b["returns"]["risky"][0]["horizon_years"] == 1
    assert "carry_rolldown" in b                                      # legacy key still present
    assert bundle["scenario_deltas_bp"] == {"base": -250, "stress": 0, "bull": -400, "risky": 600, "analysts": -157}
    assert bundle["rate_scenarios"]["base"] == -0.025                 # legacy shape unchanged
    con.close()


# ---- universe: aey_risky ------------------------------------------------------------

def test_universe_snapshot_carries_aey_risky(tmp_path):
    con = _con(tmp_path)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(universe_snapshot)")}
    assert "aey_risky" in cols
    db.insert_universe_snapshot(con, secid="RU000A1", snap_date="2026-09-17", short_name="X", emitent_inn="1",
                                emitent_title="X", mat_date="2028-01-01", offer_date=None, coupon_pct=20.0,
                                list_level=3, qualified=0, last_pct=100.0, yield_moex=22.0, yield_to_offer=None,
                                accrued_int=None, volume=1e6, num_trades=10, duration_moex=365.0, ytm=0.22,
                                tenor_yrs=1.3, rf_rate=0.14, g_spread_bp=800.0, yield_source="moex",
                                rating="ruBB", implied_fair_spread_bp=500.0, mispricing_bp=300.0, in_watchlist=0,
                                signal_excluded=0, mod_dur=1.0, aey_base=0.245, aey_stress=0.22, aey_bull=0.26,
                                aey_base_risk_adj=0.20, aey_risky=0.16)
    con.commit()
    bonds = _bonds_for_snap_date(con, "2026-09-17", {})
    assert bonds[0]["aey_risky_pct"] == 16.0
    con.close()


# ---- premium: one formula on both legs ------------------------------------------------------

def _universe_bond(con, secid, ytm, dur_days, rating="ruBBB", excluded=0):
    db.insert_universe_snapshot(con, secid=secid, snap_date="2026-09-17", short_name=secid, emitent_inn=secid,
                                emitent_title=secid, mat_date="2028-06-01", offer_date=None, coupon_pct=18.0,
                                list_level=3, qualified=0, last_pct=100.0, yield_moex=ytm * 100, yield_to_offer=None,
                                accrued_int=None, volume=1e6, num_trades=10, duration_moex=dur_days, ytm=ytm,
                                tenor_yrs=1.8, rf_rate=0.14, g_spread_bp=500.0, yield_source="moex", rating=rating,
                                implied_fair_spread_bp=300.0, mispricing_bp=200.0, in_watchlist=0,
                                signal_excluded=excluded, mod_dur=dur_days / 365, aey_base=None, aey_stress=None,
                                aey_bull=None, aey_base_risk_adj=None)


def test_rates_premium_uses_the_same_aey_formula_on_both_legs(tmp_path):
    con = _con(tmp_path)
    _universe_bond(con, "A", 0.24, 1.4 * 365)
    _universe_bond(con, "B", 0.20, 1.2 * 365)
    _universe_bond(con, "C", 3.0, 0.3 * 365, excluded=1)       # distress-priced: never in the average
    con.commit()
    scen = {"base": -0.025, "stress": 0.0, "bull": -0.04, "risky": 0.06, "analysts": None}
    pf = [{"id": 1, "name": "Консервативный", "preset_type": "conservative",
           "allocation": {"bonds": [{"isin": "A", "weight": 0.5}, {"isin": "B", "weight": 0.4},
                                    {"isin": "C", "weight": 0.1}]}}]
    ofz = [{"secid": "SU26236", "short_name": "ОФЗ 26236", "ytm_pct": 16.16, "mod_dur": 1.357, "tenor_yrs": 1.67,
            "mat_date": "2028-05-17", "benchmark": True},
           {"secid": "SU52002", "short_name": "ОФЗ 52002", "ytm_pct": 7.1, "mod_dur": 1.3, "tenor_yrs": 1.7,
            "mat_date": "2028-02-02", "benchmark": False}]
    out = rates_premium_export(con, scen, pf, ofz)["portfolios"][0]
    assert out["ofz"]["secid"] == "SU26236"                    # the linker is never the benchmark
    assert out["covered"] == 2 and "analysts" not in out["by_scenario"]
    for key in ("base", "stress", "bull", "risky"):
        d = scen[key]
        exp_pf = sum(w * portfolio.pd_adjusted_aey(portfolio.bond_aey(y, dur, d), "ruBBB", con=con)
                     for y, dur, w in ((0.24, 1.4, 0.5), (0.20, 1.2, 0.4))) / 0.9 * 100
        exp_ofz = portfolio.bond_aey(0.1616, 1.357, d) * 100
        got = out["by_scenario"][key]
        assert got["pf_aey_pct"] == pytest.approx(exp_pf, abs=0.01)
        assert got["ofz_aey_pct"] == pytest.approx(exp_ofz, abs=0.01)
        assert got["premium_pp"] == pytest.approx(exp_pf - exp_ofz, abs=0.02)
    con.close()


def test_rates_premium_skips_an_ofz_too_far_in_duration(tmp_path):
    con = _con(tmp_path)
    _universe_bond(con, "A", 0.24, 0.5 * 365)
    con.commit()
    ofz = [{"secid": "SU26238", "short_name": "ОФЗ 26238", "ytm_pct": 15.5, "mod_dur": 9.5, "tenor_yrs": 14.8,
            "mat_date": "2041-05-15", "benchmark": True}]
    pf = [{"id": 1, "name": "P", "preset_type": "balanced", "allocation": {"bonds": [{"isin": "A", "weight": 1.0}]}}]
    out = rates_premium_export(con, {"base": -0.025}, pf, ofz)["portfolios"][0]
    assert out["ofz"] is None
    assert out["by_scenario"]["base"]["premium_pp"] is None
    assert out["by_scenario"]["base"]["pf_aey_pct"] is not None
    con.close()


# ---- DATA.expectations / DATA.zcyc ----------------------------------------------------------

def test_expectations_export_shape(tmp_path, monkeypatch):
    con = _con(tmp_path)
    _seed_publications(con)
    db.upsert_cbr_document(con, meeting_date="2026-09-11", doc_type="press_release",
                           url="https://www.cbr.ru/press/pr/?file=11092026_133000key.htm", published="2026-09-11",
                           title="Банк России принял решение сохранить ключевую ставку на уровне 14,00% годовых",
                           text="Решение. Банк России будет принимать дальнейшие решения по ключевой ставке "
                                "в зависимости от динамики инфляции.")
    db.upsert_cbr_document(con, meeting_date="2023-12-15", doc_type="press_release", url="u", title="old")
    db.mark_expectation_source(con, "survey", page_url="https://www.cbr.ru/statistics/ddkp/mo_br/",
                               last_published="2026-09-02")
    con.commit()

    class _Today(date):
        @classmethod
        def today(cls):
            return TODAY
    import export_dashboard_data
    monkeypatch.setattr(export_dashboard_data, "date", _Today)
    scen = portfolio.get_rate_scenarios(con, today=TODAY)
    e = expectations_export(con, scen)

    assert [s["label"] for s in e["scenarios"]] == ["Базовый", "Дезинфляционный", "Проинфляционный", "Рисковый",
                                                     "Аналитики"]
    risky = next(s for s in e["scenarios"] if s["key"] == "risky")
    assert risky["range"] == [19.0, 21.0] and risky["delta_bp"] == 600 and risky["status"] == "draft"
    assert risky["url"] == "https://www.cbr.ru/about_br/publ/ondkp/on_2027_2029/"
    f = e["forecasts"][-1]
    assert f["years"]["2027"] == [10.5, 12.5] and f["rest"]["lo"] == 13.7
    assert e["ondkp"][0]["scenarios"]["proinflation"]["2027"] == [13.0, 15.0]
    assert [s["period"] for s in e["surveys"]] == ["2026-09"]           # 2021 survey has no year >= 2025
    assert "2023" not in e["surveys"][0]["years"]
    assert e["surveys"][0]["years"]["2027"] == {"median": 12.43, "q1": 11.9}
    assert [d["date"] for d in e["decisions"]] == ["2026-09-11"]         # pre-2024 documents are not exported
    assert e["decisions"][0]["rate"] == 14.0 and e["decisions"][0]["change_bp"] == 0
    assert e["decisions"][0]["signal"].startswith("Банк России будет принимать")
    sources = {s["key"]: s for s in e["sources"]}
    assert sources["survey"]["last_published"] == "2026-09-02"
    assert all(s["page_url"].startswith("https://www.cbr.ru/") for s in sources.values())
    assert e["key_rate_steps"] == [["2026-07-27", 14.0]]
    con.close()


def test_zcyc_export_in_percent(tmp_path):
    con = _con(tmp_path)
    assert zcyc_export(con) == {"snap_date": None, "points": []}
    db.replace_zcyc_snapshot(con, "2026-09-16", [(1.0, 0.1360)])
    db.replace_zcyc_snapshot(con, "2026-09-17", [(0.25, 0.1269), (10.0, 0.1646)])
    con.commit()
    assert zcyc_export(con) == {"snap_date": "2026-09-17", "points": [[0.25, 12.69], [10.0, 16.46]]}
    con.close()


def test_an_issue_far_off_the_zero_curve_is_not_a_benchmark(tmp_path):
    """ОФЗ 46012 on 2026-09-17: MOEX lists remaining coupons of 1.16 and
    13.02 ₽ against a 105% price, so its cash-flow YTM came out at 1.6%, 1 385
    б.п. under the CBR zero curve. It must not become anyone's benchmark."""
    con = _con(tmp_path)
    _ofz(con, "SU26236RMFS8", ytm=0.1616, tenor_yrs=1.67)
    _ofz(con, "SU46012RMFS9", instrument_type="АД", ytm=0.0158, tenor_yrs=2.97)
    con.commit()
    curve = [(1.0, 0.137), (2.0, 0.1478), (3.0, 0.1545)]
    rows = {r["secid"]: r for r in db.ofz_snapshot_rows(con, "2026-09-17")}
    assert ofz_pipeline.is_benchmark(rows["SU26236RMFS8"], curve)
    assert not ofz_pipeline.is_benchmark(rows["SU46012RMFS9"], curve)
    assert ofz_pipeline.curve_gap_bp(rows["SU46012RMFS9"], curve) == pytest.approx(-1385, abs=3)
    assert ofz_pipeline.is_benchmark(rows["SU46012RMFS9"])          # without a curve the guard cannot apply
    returns = ofz_pipeline.horizon_returns_for_snapshot(con, "2026-09-17", {"base": -0.025}, curve)
    assert "SU46012RMFS9" not in returns
    con.close()


def test_reading_scenarios_writes_no_schema(tmp_path):
    """get_rate_scenarios() runs inside other pipelines' transactions; an
    executescript() there would COMMIT their pending writes early."""
    con = db.connect(tmp_path / "t.sqlite")
    db.init_cbr(con)
    portfolio.get_rate_scenarios(con, today=TODAY)
    from hybond import cbr_tracker
    cbr_tracker.get_schedule(con)
    assert not db.has_table(con, "rate_expectation")
    assert not db.has_table(con, "cbr_schedule")
    con.close()
