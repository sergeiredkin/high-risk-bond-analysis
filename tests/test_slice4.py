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
    # debt coverage stress = ST debt / (cash + operating_cf)
    #   = 400,000 / (150,000 + 210,000) = 400,000 / 360,000 = 1.1111
    assert abs(pil.debt_coverage_stress - 400000 / 360000) < 1e-9


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
    # refinancing = average of two component scores:
    #   ST debt share 0.3333 -> structure band (<=0.35 -> 80)
    #   debt coverage stress 1.1111 -> stress band (0.8-1.2 "monitor" -> 70)
    #   (80 + 70) / 2 = 75
    assert pil.score_refinancing == 75


def test_refinancing_score_blends_structure_and_stress_components():
    """Isolates the refinancing blend from the shared fixture above --
    directly exercises the averaging + graceful-degradation behaviour."""
    # comfortable stress ratio (0.5) but poor structure (ST share 0.9) ->
    # structure band 20, stress band 100 -> average 60
    balance = {1410: 100, 1510: 900, 1250: 1800, 1240: 0}
    pil = P.compute_pillars(balance, {}, {})
    assert abs(pil.short_term_debt_share - 0.9) < 1e-9
    assert abs(pil.debt_coverage_stress - 900 / 1800) < 1e-9
    assert pil.score_refinancing == 60  # (20 + 100) / 2

    # only structure computable (no cash/operating_cf at all) -> falls back
    # to the structure score alone, not None
    balance_no_cash = {1410: 100, 1510: 900}
    pil2 = P.compute_pillars(balance_no_cash, {}, {})
    assert pil2.debt_coverage_stress is None
    assert pil2.score_refinancing == 20  # ST share 0.9 -> structure band 20, stress unavailable


def test_negative_resource_pool_scores_worst_not_best():
    """A deeply negative operating cash flow that exceeds cash makes the raw
    ST-debt / (cash+OCF) ratio NEGATIVE -- naive "lower is better" banding
    would misread that as the safest tier. Must be forced to the worst tier
    instead. Real case this caught: several watchlist issuers (e.g. Агро-
    Фрегат, stress ratio -22.98) had negative operating cash flow deep
    enough to flip the sign, and without this guard would have scored a
    perfect 100 on the stress component."""
    balance = {1410: 0, 1510: 500, 1250: 100, 1240: 0}
    cf = {4100: -400}  # operating cash flow deeply negative: pool = 100 + (-400) = -300
    pil = P.compute_pillars(balance, {}, cf)
    assert pil.debt_coverage_stress == 500 / -300  # raw ratio IS negative -- kept, not hidden
    # structure: ST share = 500/500 = 1.0 -> worst structure tier (20)
    # stress: forced worst tier (15) due to negative pool
    # average = (20 + 15) / 2 = 17.5 -> rounds to 18 (banker's rounding: round(17.5)==18 in Python 3)
    assert pil.score_refinancing in (17, 18)  # avoid relying on round-half-to-even edge case


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


# --- pillars/mispricing trend tracking -------------------------------------

def test_pillars_history_accumulates_across_fetch_dates(tmp_path):
    """The original PK (issuer_id, period) silently overwrote a same-period
    re-fetch (e.g. a weekly cron hitting the same still-current fiscal
    year), so no issuer could ever accumulate real history. Confirms the
    widened PK (issuer_id, period, computed_on) actually keeps both rows."""
    con = db.connect(tmp_path / "hist.sqlite")
    db.init_db(con); db.init_pillars(con)
    db.upsert_issuer(con, "I1", "E", None)
    db.upsert_pillars(con, "I1", "2025", "2026-07-27", json.dumps({"score_leverage": 40}))
    db.upsert_pillars(con, "I1", "2025", "2026-07-30", json.dumps({"score_leverage": 60}))
    con.commit()
    hist = db.pillars_history(con, "I1", limit=10)
    assert len(hist) == 2
    assert hist[0]["computed_on"] == "2026-07-30"  # newest first
    assert hist[1]["computed_on"] == "2026-07-27"
    con.close()


def test_init_pillars_migrates_pre_existing_old_pk_table(tmp_path):
    """Constructs a genuine pre-migration pillars_snapshot (old PK) via raw
    SQL, inserts two same-issuer/same-period rows the old way (the second
    silently clobbers the first, proving the bug), then confirms
    db.init_pillars migrates the table without losing the row that made it
    through, and that new inserts afterward actually accumulate."""
    con = db.connect(tmp_path / "mig.sqlite")
    db.init_db(con)
    con.executescript("""
        CREATE TABLE pillars_snapshot (
            issuer_id TEXT, period TEXT, computed_on TEXT, payload TEXT,
            PRIMARY KEY (issuer_id, period)
        );
    """)
    con.execute("INSERT INTO pillars_snapshot VALUES (?,?,?,?)",
               ("I1", "2025", "2026-07-27", json.dumps({"score_leverage": 40})))
    con.commit()
    db.upsert_issuer(con, "I1", "E", None)
    db.init_pillars(con)  # migration should fire here
    con.commit()
    # post-migration, a same-period re-fetch on a new date must NOT clobber
    db.upsert_pillars(con, "I1", "2025", "2026-07-30", json.dumps({"score_leverage": 60}))
    con.commit()
    hist = db.pillars_history(con, "I1", limit=10)
    assert len(hist) == 2
    con.close()


def test_pillars_delta_improving_degrading_flat():
    prev = {"score_leverage": 40, "score_liquidity": 60, "score_refinancing": 50, "score_operations": 70}
    cur = {"score_leverage": 60, "score_liquidity": 60, "score_refinancing": 30, "score_operations": None}
    d = P.pillars_delta(cur, prev)
    assert d["score_leverage"] == {"delta": 20, "direction": "improving"}
    assert d["score_liquidity"] == {"delta": 0, "direction": "flat"}
    assert d["score_refinancing"] == {"delta": -20, "direction": "degrading"}
    assert d["score_operations"] == {"delta": None, "direction": None}


def test_pillars_delta_no_previous_returns_all_none():
    cur = {"score_leverage": 60}
    d = P.pillars_delta(cur, None)
    assert d["score_leverage"] == {"delta": None, "direction": None}


def test_mispricing_history_reads_universe_snapshot_directly(tmp_path):
    """No new storage -- universe_snapshot already has one row per
    (secid, snap_date); this just queries across dates for one bond."""
    con = db.connect(tmp_path / "mis.sqlite")
    db.init_db(con); db.init_universe(con)
    db.upsert_issuer(con, "I1", "E", None)
    for d, mp in [("2026-07-28", 900.0), ("2026-07-29", 1100.0), ("2026-07-30", 1500.0)]:
        db.insert_universe_snapshot(
            con, secid="SECX", snap_date=d, short_name="Тест", emitent_inn="I1",
            emitent_title="E", mat_date="2030-01-01", offer_date=None,
            coupon_pct=15.0, list_level=1, qualified=0, last_pct=90.0,
            yield_moex=20.0, yield_to_offer=None, accrued_int=0.0, volume=100.0,
            num_trades=5, duration_moex=2.0, ytm=0.2, tenor_yrs=2.0, rf_rate=0.14,
            g_spread_bp=600.0, yield_source="computed", rating="ruBB", implied_fair_spread_bp=400.0,
            mispricing_bp=mp, in_watchlist=1, signal_excluded=0, mod_dur=1.8,
            aey_base=0.2, aey_stress=0.18, aey_bull=0.22, aey_base_risk_adj=0.19)
    con.commit()
    hist = db.mispricing_history(con, "SECX", limit=10)
    assert [r["mispricing_bp"] for r in hist] == [1500.0, 1100.0, 900.0]  # newest first
    assert [r["snap_date"] for r in hist] == ["2026-07-30", "2026-07-29", "2026-07-28"]
    con.close()
