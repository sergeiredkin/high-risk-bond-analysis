"""Tests for promotion_audit.py (Expanded pre-promotion audit task,
2026-08-24) -- the pure DB-query/classification logic behind
`hybond watchlist-promote`'s pre-promotion audit, plus CLI-level coverage of
the BLOCKED/WARNINGS/--force flow in cmd_watchlist_promote."""
import sys
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import promotion_audit  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "111"
TODAY = "2026-08-24"


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_pillars(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, ISSUER, "Test Co", watchlist_tier=2)
    con.commit()
    return con


def _bond_with_history(con, secid="B1", volume=1000.0, ytm=0.15, g_spread=200.0,
                       rating="ruBB", snap_date="2026-08-20"):
    db.upsert_bond(con, secid=secid, isin="RU1", issuer_id=ISSUER, short_name=secid,
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated=snap_date)
    db.insert_price_snapshot(con, secid=secid, snap_date=snap_date, last_pct=100,
                              yield_moex=ytm * 100, duration=400, volume=volume,
                              num_trades=10, systime="t")
    db.insert_universe_snapshot(
        con, secid=secid, snap_date=snap_date, short_name=secid, emitent_inn=ISSUER,
        emitent_title="Test Co", mat_date="2028-01-01", offer_date=None,
        coupon_pct=15.0, list_level=3, qualified=1, last_pct=100.0, yield_moex=ytm * 100,
        yield_to_offer=None, accrued_int=0.0, volume=volume, num_trades=10,
        duration_moex=365.0, ytm=ytm, tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=g_spread,
        yield_source="moex", rating=rating, implied_fair_spread_bp=None,
        mispricing_bp=None, in_watchlist=1, signal_excluded=0, mod_dur=1.0,
        aey_base=ytm, aey_stress=ytm, aey_bull=ytm, aey_base_risk_adj=ytm)
    return secid


# ---- pure audit logic -------------------------------------------------------

def test_clean_when_no_signals(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    db.upsert_pillars(con, issuer_id=ISSUER, period="2025", computed_on=TODAY,
                       payload_json='{"score_leverage": 80, "score_liquidity": 80, '
                                    '"score_refinancing": 80, "score_operations": 80}')
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "CLEAN"
    assert audit["blocking"] == []
    assert audit["warnings"] == []


def test_blocked_on_critical_insolvency_event(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    db.insert_event(con, issuer_id=ISSUER, occurred="2026-08-01", category="insolvency",
                     severity="critical", summary="Технический дефолт", source_url=None)
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "BLOCKED"
    assert any("insolvency" in b for b in audit["blocking"])


def test_not_blocked_on_default_category_event_older_than_90_days(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    old = "2026-01-01"  # > 90 days before TODAY
    db.insert_event(con, issuer_id=ISSUER, occurred=old, category="default",
                     severity="critical", summary="Старый дефолт", source_url=None)
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] != "BLOCKED"
    assert audit["events"]["rows"] == []  # outside the 90-day window entirely


def test_blocked_on_ytm_over_100(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=1.50, g_spread=5000.0)  # 150% YTM
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "BLOCKED"
    assert any("YTM" in b for b in audit["blocking"])


def test_warned_on_ytm_30_to_100(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.50, g_spread=500.0)  # 50% YTM -- warn, not block
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "WARNINGS"
    assert audit["blocking"] == []
    assert any("YTM" in w for w in audit["warnings"])


def test_blocked_on_gspread_delta_over_500bp_single_period(tmp_path):
    con = _con(tmp_path)
    secid = "B1"
    db.upsert_bond(con, secid=secid, isin="RU1", issuer_id=ISSUER, short_name=secid,
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated=TODAY)
    db.insert_price_snapshot(con, secid=secid, snap_date=TODAY, last_pct=100,
                              yield_moex=15.0, duration=400, volume=1000.0,
                              num_trades=10, systime="t")
    db.insert_spread_snapshot(con, secid=secid, snap_date="2026-08-10", ytm=0.15,
                               tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=200.0,
                               mod_dur=1.0, convexity=None)
    db.insert_spread_snapshot(con, secid=secid, snap_date="2026-08-11", ytm=0.15,
                               tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=900.0,  # +700bp jump
                               mod_dur=1.0, convexity=None)
    db.insert_universe_snapshot(
        con, secid=secid, snap_date="2026-08-11", short_name=secid, emitent_inn=ISSUER,
        emitent_title="Test Co", mat_date="2028-01-01", offer_date=None,
        coupon_pct=15.0, list_level=3, qualified=1, last_pct=100.0, yield_moex=15.0,
        yield_to_offer=None, accrued_int=0.0, volume=1000.0, num_trades=10,
        duration_moex=365.0, ytm=0.15, tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=900.0,
        yield_source="moex", rating="ruBB", implied_fair_spread_bp=None,
        mispricing_bp=None, in_watchlist=1, signal_excluded=0, mod_dur=1.0,
        aey_base=0.15, aey_stress=0.15, aey_bull=0.15, aey_base_risk_adj=0.15)
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "BLOCKED"
    assert any("G-spread delta" in b for b in audit["blocking"])


def test_warned_on_stale_pillars(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    old_computed = "2026-04-01"  # well over 90 days before TODAY
    db.upsert_pillars(con, issuer_id=ISSUER, period="2025", computed_on=old_computed,
                       payload_json='{"score_leverage": 80}')
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "WARNINGS"
    assert audit["pillars"]["stale"] is True
    assert any("pillar scores are" in w for w in audit["warnings"])


def test_warned_when_no_financial_data_found(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    con.commit()  # no pillars_snapshot row at all

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["pillars"]["found"] is False
    assert any("ГИР БО не найден" in w for w in audit["warnings"])


def test_gspread_trend_last_5_entries_with_deltas(tmp_path):
    con = _con(tmp_path)
    secid = "B1"
    db.upsert_bond(con, secid=secid, isin="RU1", issuer_id=ISSUER, short_name=secid,
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated=TODAY)
    db.insert_price_snapshot(con, secid=secid, snap_date=TODAY, last_pct=100,
                              yield_moex=15.0, duration=400, volume=1000.0,
                              num_trades=10, systime="t")
    dates_spreads = [("2026-08-01", 100.0), ("2026-08-05", 150.0), ("2026-08-10", 140.0),
                     ("2026-08-15", 160.0), ("2026-08-18", 170.0), ("2026-08-20", 180.0),
                     ("2026-08-22", 190.0)]
    for d, sp in dates_spreads:
        db.insert_spread_snapshot(con, secid=secid, snap_date=d, ytm=0.15, tenor_yrs=1.5,
                                  rf_rate=0.14, g_spread_bp=sp, mod_dur=1.0, convexity=None)
    con.commit()

    trend = promotion_audit.gspread_trend(con, ISSUER, today=TODAY)
    con.close()
    assert len(trend["rows"]) == 5  # only last 5 shown
    assert trend["rows"][0]["snap_date"] == "2026-08-10"  # oldest of the last 5
    assert trend["rows"][-1]["snap_date"] == "2026-08-22"
    assert trend["current_g_spread_bp"] == 190.0
    # max delta computed over the FULL 7-point window, not just the last 5
    assert trend["max_abs_delta_bp"] == 50.0  # 100 -> 150


def test_format_report_includes_all_six_sections(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    con.commit()
    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    report = promotion_audit.format_report(audit, issuer_name="Test Co")
    assert "1. G-spread trend" in report
    assert "2. Current YTM" in report
    assert "3. Critical/high events" in report
    assert "4. Detector flags" in report
    assert "5. Market notes" in report
    assert "6. Pillar scores" in report


# ---- CLI: BLOCKED / WARNINGS / --force flow --------------------------------

def test_cli_blocked_without_force_returns_1_and_does_not_promote(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    con.close()
    con = db.connect()
    db.insert_event(con, issuer_id=ISSUER, occurred=TODAY, category="insolvency",
                     severity="critical", summary="Дефолт", source_url=None)
    con.commit()
    con.close()

    rc = cli.cmd_watchlist_promote(Namespace(inn=ISSUER, tier=1, reason="test", force=False))
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?", (ISSUER,)).fetchone()
    con.close()
    assert rc == 1
    assert row["watchlist_tier"] == 2  # unchanged
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_force_overrides_block_and_records_it_in_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    con.close()
    con = db.connect()
    db.insert_event(con, issuer_id=ISSUER, occurred=date.today().isoformat(),
                     category="insolvency", severity="critical",
                     summary="Дефолт", source_url=None)
    con.commit()
    con.close()

    rc = cli.cmd_watchlist_promote(Namespace(inn=ISSUER, tier=1, reason="test", force=True))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?", (ISSUER,)).fetchone()
    log = con.execute(
        "SELECT reason FROM watchlist_tier_log WHERE issuer_id=? ORDER BY id DESC LIMIT 1",
        (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 1
    assert "FORCED past blocking audit" in log["reason"]
    assert "insolvency" in log["reason"]


def test_cli_warnings_only_prompts_and_respects_no_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.50, g_spread=500.0)  # warning-level YTM, no blocking
    con.commit()
    con.close()

    with patch("builtins.input", return_value="n"):
        rc = cli.cmd_watchlist_promote(Namespace(inn=ISSUER, tier=1, reason="test", force=False))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?", (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 2  # declined -- unchanged


def test_cli_warnings_only_proceeds_on_yes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.50, g_spread=500.0)
    con.commit()
    con.close()

    with patch("builtins.input", return_value="y"):
        rc = cli.cmd_watchlist_promote(Namespace(inn=ISSUER, tier=1, reason="test", force=False))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?", (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 1


def test_cli_clean_proceeds_without_any_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    db.upsert_pillars(con, issuer_id=ISSUER, period="2025",
                       computed_on=date.today().isoformat(),
                       payload_json='{"score_leverage": 80, "score_liquidity": 80, '
                                    '"score_refinancing": 80, "score_operations": 80}')
    con.commit()
    con.close()

    # input() must never be called -- a CLEAN audit proceeds straight through
    with patch("builtins.input", side_effect=AssertionError("should not prompt on CLEAN")):
        rc = cli.cmd_watchlist_promote(Namespace(inn=ISSUER, tier=1, reason="test", force=False))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?", (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 1


# ---- MOEX defaults registry block (2026-08-29) ------------------------------

def test_blocked_on_confirmed_moex_default(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)  # otherwise clean
    db.init_moex_default(con)
    con.execute(
        "INSERT INTO moex_default (issuer_name, inn, isin, obligation_type, status, planned_date) "
        "VALUES ('Test Co', ?, 'RU000TEST01', 'Купон', 'Дефолт', '2026-01-01')",
        (ISSUER,))
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] == "BLOCKED"
    assert "Подтверждённый дефолт в реестре MOEX" in audit["blocking"]
    assert len(audit["moex_defaults_confirmed"]) == 1


def test_not_blocked_on_technical_default_only(tmp_path):
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)  # otherwise clean
    db.init_moex_default(con)
    con.execute(
        "INSERT INTO moex_default (issuer_name, inn, isin, obligation_type, status, planned_date) "
        "VALUES ('Test Co', ?, 'RU000TEST01', 'Купон', 'Технический дефолт', '2026-01-01')",
        (ISSUER,))
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["status"] != "BLOCKED"
    assert audit["moex_defaults_confirmed"] == []


def test_no_moex_default_table_does_not_raise(tmp_path):
    # moex_default is optional infrastructure -- an audit run before anyone
    # has ever imported the registry must not crash. db.init_moex_default()
    # inside moex_confirmed_default() creates the table on demand, so this
    # mainly guards against a future regression removing that call.
    con = _con(tmp_path)
    _bond_with_history(con, ytm=0.15, g_spread=200.0)
    con.commit()

    audit = promotion_audit.run_audit(con, ISSUER, today=TODAY)
    con.close()
    assert audit["moex_defaults_confirmed"] == []
    assert audit["status"] != "BLOCKED"
