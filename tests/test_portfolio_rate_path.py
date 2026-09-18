"""Портфель "3-year" section (2026-09-18, captain's follow-up to the approved
Ставка и ОФЗ scenarios): portfolio.scenario_deltas_by_year() generalises
get_rate_scenarios()'s single-horizon-year lookup to a year list, and
export_dashboard_data.rates_premium_export() uses it to add `aey_path` (the
same snapshot-repricing AEY as `by_scenario`, evaluated at each of the next
few years' own published scenario level) and `rolloff` (how much of the
portfolio's own bond COUNT survives that long) to DATA.rates_premium.

`aey_path` is a repricing sensitivity across years, not a compounding value
path -- there is no cash-flow/roll-down model for corporate bonds (see
`rolloff`, and the scout report at
firstmate/data/portfolio-rate-plan-1789716252/report.md for why).
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond import portfolio  # noqa: E402
from hybond.store import db  # noqa: E402
from export_dashboard_data import rates_premium_export  # noqa: E402

TODAY = date(2026, 9, 17)


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_cbr(con)
    db.init_rates(con)
    db.init_ofz(con)
    db.init_universe(con)
    return con


def _seed_multi_year_publications(con):
    fc = "https://www.cbr.ru/Content/Document/File/194258/forecast_260724.pdf"
    db.replace_rate_expectations(con, "cbr_forecast", "2026-07-24", "2026-07-24", [
        {"scenario": "base", "year": 2027, "lo": 10.5, "hi": 12.5, "url": fc},
        {"scenario": "base", "year": 2028, "lo": 8.0, "hi": 9.0, "url": fc},
        {"scenario": "base", "year": 2029, "lo": 7.5, "hi": 8.5, "url": fc},
    ])
    on = "https://www.cbr.ru/about_br/publ/ondkp/on_2027_2029/"
    db.replace_rate_expectations(con, "ondkp", "on_2027_2029", "2026-08-31", [
        {"scenario": "proinflation", "year": 2027, "lo": 13.0, "hi": 15.0, "url": on},
        {"scenario": "proinflation", "year": 2028, "lo": 11.0, "hi": 12.0, "url": on},
        {"scenario": "proinflation", "year": 2029, "lo": 8.5, "hi": 9.5, "url": on},
        {"scenario": "disinflation", "year": 2027, "lo": 9.0, "hi": 11.0, "url": on},
        {"scenario": "disinflation", "year": 2028, "lo": 7.0, "hi": 8.0, "url": on},
        {"scenario": "disinflation", "year": 2029, "lo": 7.5, "hi": 8.5, "url": on},
        {"scenario": "risk", "year": 2027, "lo": 19.0, "hi": 21.0, "url": on},
        {"scenario": "risk", "year": 2028, "lo": 14.0, "hi": 16.0, "url": on},
        {"scenario": "risk", "year": 2029, "lo": 9.5, "hi": 10.5, "url": on},
    ], status="draft")
    db.upsert_key_rates(con, [("2026-09-17", 14.0)])
    con.commit()


def test_scenario_deltas_by_year_reads_every_requested_year(tmp_path):
    con = _con(tmp_path)
    _seed_multi_year_publications(con)
    out = portfolio.scenario_deltas_by_year(con, [2027, 2028, 2029])
    assert out["base"][2027] == pytest.approx(-0.025)   # 11.5 - 14.0
    assert out["base"][2028] == pytest.approx(-0.055)   # 8.5 - 14.0
    assert out["base"][2029] == pytest.approx(-0.06)    # 8.0 - 14.0
    assert out["stress"][2027] == pytest.approx(0.0)    # Проинфляционный 14.0 - 14.0
    assert out["bull"][2028] == pytest.approx(-0.065)   # Дезинфляционный 7.5 - 14.0
    assert out["risky"][2029] == pytest.approx(-0.04)   # Рисковый 10.0 - 14.0
    con.close()


def test_scenario_deltas_by_year_none_when_a_year_is_not_published(tmp_path):
    con = _con(tmp_path)
    _seed_multi_year_publications(con)
    out = portfolio.scenario_deltas_by_year(con, [2027, 2031])
    assert out["base"][2027] is not None
    assert out["base"][2031] is None                    # nothing published that far out
    assert out["stress"][2031] is None
    con.close()


def test_scenario_deltas_by_year_all_none_without_current_rate(tmp_path):
    con = _con(tmp_path)  # no key_rate_daily, no cbr_meeting -> current_key_rate() is None
    out = portfolio.scenario_deltas_by_year(con, [2027])
    assert out == {"base": {2027: None}, "stress": {2027: None}, "bull": {2027: None}, "risky": {2027: None}}
    con.close()


def _universe_bond(con, secid, ytm, dur_days, mat_date, offer_date=None, rating="ruBBB", coupon_pct=18.0):
    db.insert_universe_snapshot(con, secid=secid, snap_date="2026-09-17", short_name=secid, emitent_inn=secid,
                                emitent_title=secid, mat_date=mat_date, offer_date=offer_date, coupon_pct=coupon_pct,
                                list_level=3, qualified=0, last_pct=100.0, yield_moex=ytm * 100, yield_to_offer=None,
                                accrued_int=None, volume=1e6, num_trades=10, duration_moex=dur_days, ytm=ytm,
                                tenor_yrs=1.8, rf_rate=0.14, g_spread_bp=500.0, yield_source="moex", rating=rating,
                                implied_fair_spread_bp=300.0, mispricing_bp=200.0, in_watchlist=0,
                                signal_excluded=0, mod_dur=dur_days / 365, aey_base=None, aey_stress=None,
                                aey_bull=None, aey_base_risk_adj=None)


def test_rates_premium_export_adds_aey_path_and_rolloff(tmp_path):
    con = _con(tmp_path)
    _seed_multi_year_publications(con)
    # A matures within a year (rolls off soon); B is a 5-year floater (no coupon_pct -> floater,
    # and its maturity sits well beyond the 3-year window).
    _universe_bond(con, "A", 0.24, 1.4 * 365, mat_date="2027-03-01")
    _universe_bond(con, "B", 0.20, 1.2 * 365, mat_date="2032-01-01", coupon_pct=None)
    con.commit()
    scenarios = portfolio.get_rate_scenarios(con, today=TODAY)
    pf = [{"id": 1, "name": "Консервативный", "preset_type": "conservative",
           "allocation": {"bonds": [{"isin": "A", "weight": 0.5}, {"isin": "B", "weight": 0.5}]}}]
    out = rates_premium_export(con, scenarios, pf, [])["portfolios"][0]

    assert out["aey_path_years"] == [2027, 2028, 2029]
    for k in ("base", "stress", "bull", "risky"):
        assert set(out["aey_path"][k].keys()) == {"2027", "2028", "2029"}
        # 2027's delta equals the live horizon-year scenario, so the path's
        # first point must reproduce by_scenario's own AEY exactly.
        assert out["aey_path"][k]["2027"] == pytest.approx(out["by_scenario"][k]["pf_aey_pct"], abs=0.01)

    rolloff = out["rolloff"]
    assert rolloff["n_bonds"] == 2
    assert rolloff["floaters"] == 1          # B has no coupon_pct
    assert rolloff["within_1y"] == 1         # A matures 2027-03-01, within a year of TODAY-ish (2026-09-17 as_of)
    assert rolloff["beyond_3y"] == 1         # B matures 2032


def test_rolloff_uses_the_earlier_of_maturity_or_put(tmp_path):
    con = _con(tmp_path)
    _universe_bond(con, "A", 0.24, 1.0 * 365, mat_date="2033-01-01", offer_date="2028-06-01")
    con.commit()
    from export_dashboard_data import _portfolio_rolloff
    rows = {r["secid"]: r for r in db.universe_snapshot_rows(con, "2026-09-17")}
    out = _portfolio_rolloff([{"isin": "A", "weight": 1.0}], rows, date(2026, 9, 17))
    assert out["within_1_3y"] == 1    # the 2028 put, not the 2033 maturity, drives the bucket
    con.close()
