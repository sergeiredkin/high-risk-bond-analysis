"""Unit tests for ofz_pipeline.py's pure (no-network) functions, plus
carry_rolldown_for_snapshot against a real tmp-file SQLite DB (same pattern
as test_portfolio.py) -- no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import ofz_pipeline  # noqa: E402
from hybond.store import db  # noqa: E402


# --- is_ofz: mirror-image of universe_pipeline's exclusion -----------------

def test_is_ofz_true_for_su_prefixed_secid():
    assert ofz_pipeline.is_ofz("SU26238RMFS4", "SPOB", "SUR")


def test_is_ofz_true_for_tqob_board_even_without_su_prefix_check_order():
    assert ofz_pipeline.is_ofz("SU29021RMFS1", "TQOB", "SUR")


def test_is_ofz_false_for_corporate_bond():
    assert not ofz_pipeline.is_ofz("RU000A10E6D0", "TQCB", "SUR")


def test_is_ofz_false_for_wrong_faceunit():
    assert not ofz_pipeline.is_ofz("SU26238RMFS4", "TQOB", "USD")


# --- classify_instrument_type: real SU-code convention, verified against
# live MOEX data 2026-08-06 (see ofz_pipeline.py module docstring) ---------

def test_classify_pd_fixed_coupon():
    t, ok = ofz_pipeline.classify_instrument_type("SU26238RMFS4")
    assert t == "ПД" and ok


def test_classify_pk_floater():
    t, ok = ofz_pipeline.classify_instrument_type("SU29021RMFS1")
    assert t == "ПК" and ok


def test_classify_in_linker():
    t, ok = ofz_pipeline.classify_instrument_type("SU52005RMFS4")
    assert t == "ИН" and ok


def test_classify_ad_amortizing():
    t, ok = ofz_pipeline.classify_instrument_type("SU46020RMFS2")
    assert t == "АД" and ok


def test_classify_unrecognised_prefix_flagged():
    t, ok = ofz_pipeline.classify_instrument_type("SU99999RMFS0")
    assert t == "ПД" and ok is False


# --- board dedup ------------------------------------------------------------

def test_dedup_by_board_prefers_tqob():
    rows = [
        {"SECID": "SU26238RMFS4", "BOARDID": "SPOB", "MATDATE": "2036-01-01"},
        {"SECID": "SU26238RMFS4", "BOARDID": "TQOB", "MATDATE": "2036-01-01"},
    ]
    best = ofz_pipeline._dedup_by_board(rows)
    assert best["SU26238RMFS4"]["BOARDID"] == "TQOB"


def test_dedup_by_board_single_row_passthrough():
    rows = [{"SECID": "SU52005RMFS4", "BOARDID": "SPOB", "MATDATE": "2033-05-11"}]
    best = ofz_pipeline._dedup_by_board(rows)
    assert best["SU52005RMFS4"]["BOARDID"] == "SPOB"


# --- carry_rolldown_for_snapshot: real tmp-file SQLite DB -------------------

def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_ofz(con)
    return con


def test_carry_rolldown_projects_every_scenario(tmp_path):
    con = _con(tmp_path)
    db.upsert_ofz_snapshot(
        con, secid="SU26238RMFS4", snap_date="2026-08-05", short_name="ОФЗ 26238",
        isin="RU000A1038V6", board_id="TQOB", instrument_type="ПД",
        mat_date="2041-05-15", coupon_pct=7.1, face_value=1000.0,
        last_pct=57.0, accrued_int_pct=None, dirty_price_pct=57.0,
        ytm=0.155, mod_dur=9.5, convexity=110.0, tenor_yrs=14.8,
        volume=50000.0, num_trades=120, yield_source="computed",
    )
    scenarios = {"base": -0.025, "stress": 0.006, "bull": -0.06, "is_fallback": False}
    rows = ofz_pipeline.carry_rolldown_for_snapshot(con, "2026-08-05", scenarios)
    assert len(rows) == 1
    proj = rows[0]["projections"]
    assert set(proj.keys()) == {"base", "stress", "bull"}
    assert [h["horizon_years"] for h in proj["base"]] == [1, 2, 3]


def test_carry_rolldown_skips_near_maturity_bonds(tmp_path):
    con = _con(tmp_path)
    db.upsert_ofz_snapshot(
        con, secid="SU26219RMFS4", snap_date="2026-08-05", short_name="ОФЗ 26219",
        isin="RU000A0JVUV3", board_id="TQOB", instrument_type="ПД",
        mat_date="2026-09-16", coupon_pct=7.75, face_value=1000.0,
        last_pct=99.315, accrued_int_pct=None, dirty_price_pct=99.315,
        ytm=0.4758, mod_dur=0.078, convexity=0.059, tenor_yrs=0.115,
        volume=10000.0, num_trades=20, yield_source="computed",
    )
    scenarios = {"base": -0.025, "stress": 0.006, "bull": -0.06, "is_fallback": False}
    rows = ofz_pipeline.carry_rolldown_for_snapshot(con, "2026-08-05", scenarios)
    assert rows == []


def test_carry_rolldown_skips_unpriced_bonds(tmp_path):
    con = _con(tmp_path)
    db.upsert_ofz_snapshot(
        con, secid="SU29021RMFS1", snap_date="2026-08-05", short_name="ОФЗ 29021",
        isin="RU000A103X66", board_id="TQOB", instrument_type="ПК",
        mat_date="2030-11-27", coupon_pct=None, face_value=1000.0,
        last_pct=100.06, accrued_int_pct=None, dirty_price_pct=100.06,
        ytm=None, mod_dur=None, convexity=None, tenor_yrs=4.3,
        volume=5000.0, num_trades=10, yield_source="moex",
    )
    scenarios = {"base": -0.025, "stress": 0.006, "bull": -0.06, "is_fallback": False}
    rows = ofz_pipeline.carry_rolldown_for_snapshot(con, "2026-08-05", scenarios)
    assert rows == []  # no coupon_pct -> can't compute coupon_return, skipped
