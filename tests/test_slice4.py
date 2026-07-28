"""Slice 4 tests. Run: PYTHONPATH=tests python -m pytest tests/test_slice4.py -q

Financials come from ГИР БО as {code: value} keyed by Минфин 66н codes, so the
pillar layer is testable offline against a realistic payload — no OCR, no
network. Pillar arithmetic is checked against hand-computed values.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.ingest import nalog  # noqa: E402
from hybond.compute import pillars as P  # noqa: E402
from hybond.store import db  # noqa: E402
from hybond.factlist.assemble import build_factlist  # noqa: E402


# A details() payload shaped exactly like the confirmed bo.nalog/parser-api
# schema: a HY-ish issuer with real debt and thin coverage.
DETAILS = {
    "organization": {"short_name": "ООО ТестВДО", "full_name": "ООО ТестВДО",
                     "inn": "7701234567"},
    "reports": [{
        "period": "2025",
        "actual_bfo_date": "2026-03-30",
        "required_audit": 1,
        "organization_info": {"has_ks": False},
        "balance": [
            {"code": 1200, "name": "Оборотные активы", "current": 1500000, "previous": 1400000},
            {"code": 1250, "name": "Денежные средства", "current": 120000, "previous": 100000},
            {"code": 1240, "name": "Фин. вложения обор.", "current": 30000, "previous": 20000},
            {"code": 1410, "name": "Долгосрочные заёмные", "current": 800000, "previous": 700000},
            {"code": 1500, "name": "Краткосрочные обяз.", "current": 1000000, "previous": 950000},
            {"code": 1510, "name": "Краткосрочные заёмные", "current": 400000, "previous": 300000},
            {"code": 1300, "name": "Капитал и резервы", "current": 500000, "previous": 480000},
            {"code": 1600, "name": "Баланс (актив)", "current": 2300000, "previous": 2130000},
        ],
        "financial_result": [
            {"code": 2110, "name": "Выручка", "current": 3000000, "previous": 2800000},
            {"code": 2200, "name": "Прибыль от продаж", "current": 300000, "previous": 280000},
            {"code": 2330, "name": "Проценты к уплате", "current": 150000, "previous": 120000},
            {"code": 2400, "name": "Чистая прибыль", "current": 90000, "previous": 85000},
        ],
        "funds_movement": [
            {"code": 4100, "name": "Сальдо от текущих операций", "current": 210000, "previous": 180000},
        ],
    }],
}


class FakeBackend:
    def search_id(self, inn):
        return "999", {"id": 999, "inn": inn}
    def details(self, org_id):
        return DETAILS


# --- normalisation ---------------------------------------------------------

def test_normalise_extracts_code_maps():
    rec = nalog.fetch_financials("7701234567", FakeBackend(), snapshot=False)
    assert rec is not None
    assert rec.period == "2025"
    assert rec.has_consolidated is False
    assert rec.balance[1410] == 800000
    assert rec.financial_result[2110] == 3000000
    assert rec.funds_movement[4100] == 210000
    assert rec.balance_prev[1410] == 700000   # previous-period value


def test_normalise_none_when_no_reports():
    class Empty:
        def search_id(self, inn): return "1", {}
        def details(self, org_id): return {"organization": {}, "reports": []}
    assert nalog.fetch_financials("x", Empty(), snapshot=False) is None


# --- pillar arithmetic (hand-checked) --------------------------------------

def test_pillars_hand_checked():
    rec = nalog.fetch_financials("7701234567", FakeBackend(), snapshot=False)
    pil = P.compute_pillars(rec.balance, rec.financial_result, rec.funds_movement,
                            rec.balance_prev)
    inp = pil.inputs
    # total debt = 1410 + 1510 = 800000 + 400000 = 1,200,000
    assert inp.total_debt == 1200000
    # cash = 1250 + 1240 = 120000 + 30000 = 150,000
    assert inp.cash == 150000
    # debt/EBIT = 1,200,000 / 300,000 = 4.0
    assert abs(pil.debt_to_ebit - 4.0) < 1e-9
    # interest coverage = EBIT / interest = 300,000 / 150,000 = 2.0
    assert abs(pil.interest_coverage - 2.0) < 1e-9
    # current ratio = 1200 / 1500 = 1,500,000 / 1,000,000 = 1.5
    assert abs(pil.current_ratio - 1.5) < 1e-9
    # cash / ST debt = 150,000 / 400,000 = 0.375
    assert abs(pil.cash_to_short_term_debt - 0.375) < 1e-9
    # ST debt share = 400,000 / 1,200,000 = 0.3333
    assert abs(pil.short_term_debt_share - 1/3) < 1e-9
    # net margin = 90,000 / 3,000,000 = 0.03
    assert abs(pil.net_margin - 0.03) < 1e-9


def test_pillar_scores_in_range():
    rec = nalog.fetch_financials("7701234567", FakeBackend(), snapshot=False)
    pil = P.compute_pillars(rec.balance, rec.financial_result, rec.funds_movement)
    for s in (pil.score_leverage, pil.score_liquidity, pil.score_refinancing,
              pil.score_operations):
        assert s is None or 0 <= s <= 100
    # debt/EBIT 4.0 -> leverage band (<=4 -> 60)
    assert pil.score_leverage == 60
    # current ratio 1.5 -> liquidity band (>=1.5 -> 80)
    assert pil.score_liquidity == 80


def test_missing_codes_yield_none_not_zero():
    # a payload with no debt lines: leverage metrics must be None, with a note
    pil = P.compute_pillars(
        balance={1200: 100, 1500: 80, 1600: 300, 1300: 200},
        fin={2110: 500, 2200: 50, 2400: 20},
        cf={4100: 40})
    assert pil.inputs.total_debt is None
    assert pil.debt_to_ebit is None
    assert pil.score_leverage is None
    assert "no debt lines" in pil.coverage_note


# --- factlist integration: pillars + thesis fill the reserved placeholders --

def test_factlist_pulls_pillars_and_thesis(tmp_path):
    con = db.connect(tmp_path / "s4.sqlite")
    db.init_db(con); db.init_pillars(con); db.init_filing(con)
    db.upsert_issuer(con, "7701234567", "ООО ТестВДО", "microfinance")
    db.upsert_bond(con, secid="RU000S4", isin="RU000S4", issuer_id="7701234567",
                   short_name="Тест", mat_date="2028-07-25", face_value=1000.0,
                   face_unit="SUR", coupon_pct=18.0, list_level=3, qualified=1,
                   updated="2026-07-25")
    db.insert_spread_snapshot(con, secid="RU000S4", snap_date="2026-07-25",
                              ytm=0.2, tenor_yrs=2.0, rf_rate=0.14,
                              g_spread_bp=600.0, mod_dur=1.5, convexity=3.0)
    # store computed pillars + a thesis
    rec = nalog.fetch_financials("7701234567", FakeBackend(), snapshot=False)
    pil = P.compute_pillars(rec.balance, rec.financial_result, rec.funds_movement)
    db.upsert_pillars(con, "7701234567", "2025", "2026-07-25",
                      json.dumps(pil.to_json(), ensure_ascii=False))
    db.upsert_thesis(con, "7701234567", "рефинансирует 2028",
                     "EBITDA>250k; сохранит банк. линии", "просрочка купона; leverage>6x",
                     "holding", "2026-07-25")
    con.commit()

    fl = build_factlist(con, "7701234567", "2026-07-25", flags=[],
                        stat_warming_up=True).to_json()
    # the placeholders Slice 2 reserved are now populated
    assert fl["six_pillars"]["debt_to_ebit"] == 4.0
    assert fl["six_pillars"]["score_liquidity"] == 80
    assert fl["thesis_status"] == "holding"
    con.close()


def test_thesis_store_lifecycle(tmp_path):
    con = db.connect(tmp_path / "th.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "I1", "E", None)
    db.upsert_thesis(con, "I1", "t", "a", "coupon miss", "holding", "2026-07-25")
    con.commit()
    assert db.get_thesis(con, "I1")["status"] == "holding"
    db.set_thesis_status(con, "I1", "broken", "2026-07-26")
    con.commit()
    assert db.get_thesis(con, "I1")["status"] == "broken"
    con.close()


def test_consolidated_flag_surfaced():
    # an issuer WITH consolidated statements: the flag must propagate so the
    # scorecard skill can warn about single-entity РСБУ understating group debt
    d = json.loads(json.dumps(DETAILS))
    d["reports"][0]["organization_info"]["has_ks"] = True
    class B:
        def search_id(self, inn): return "1", {}
        def details(self, org_id): return d
    rec = nalog.fetch_financials("x", B(), snapshot=False)
    assert rec.has_consolidated is True


def test_direct_backend_is_stub():
    b = nalog.NalogDirectBackend()
    try:
        b.search_id("7701234567")
        assert False, "should be a stub"
    except NotImplementedError:
        pass
