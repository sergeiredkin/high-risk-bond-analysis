"""CLI-level tests for `hybond portfolio-monitor` -- the Phase B daily
pipeline step (cmd_portfolio_monitor). Same Namespace-call + monkeypatched
DB_PATH pattern as test_portfolio_build_cli.py."""
import json
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond.store import db  # noqa: E402


def _seed_bond(con, idx, health="green", g_spread_bp=500.0, mat_date="2030-01-01"):
    issuer_id = f"ISS{idx:04d}"
    secid = f"SECID{idx:04d}"
    isin = f"RU{idx:06d}"
    db.add_issuer_at_tier(con, issuer_id, f"Issuer {idx}", watchlist_tier=1, sector="finance")
    db.set_issuer_health_status(con, issuer_id, health)
    db.upsert_rating(con, issuer_id=issuer_id, agency="expert_ra", rating="ruBBB+",
                      outlook=None, action=None, rated_on="2026-01-01")
    db.add_issuer_bond(con, issuer_id=issuer_id, secid=secid, source="manual")
    db.upsert_bond(con, secid=secid, isin=isin, issuer_id=issuer_id, short_name=f"B{idx}",
                    mat_date=mat_date, face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated="2026-08-01")
    db.insert_spread_snapshot(con, secid=secid, snap_date="2026-08-26", ytm=0.20,
                               tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=g_spread_bp,
                               mod_dur=1.5, convexity=None)
    con.commit()
    return isin


def test_portfolio_monitor_no_active_portfolios_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    con.close()

    rc = cli.cmd_portfolio_monitor(Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {}


def test_portfolio_monitor_writes_alerts_and_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_portfolio(con)
    isin = _seed_bond(con, 1, g_spread_bp=800.0)  # baseline 500 -> +300bp alert
    portfolio_id = db.create_portfolio(con, name="Live Test", preset_type="conservative",
                                        sizing_mode="equal", status="active")
    db.add_portfolio_bond(con, portfolio_id=portfolio_id, isin=isin, weight=1.0,
                           flags_json=json.dumps({"gspread_baseline_bp": 500.0,
                                                     "health_at_save": "green"}))
    con.commit()
    con.close()

    rc = cli.cmd_portfolio_monitor(Namespace())
    assert rc == 0
    err = capsys.readouterr().err
    assert "1 alert(s)" in err

    con = db.connect()
    flags = con.execute(
        "SELECT * FROM flag_log WHERE detector='portfolio_monitor'").fetchall()
    assert len(flags) == 1
    assert flags[0]["signal"] == "portfolio_gspread_alert"
    assert flags[0]["severity"] == "high"
    snaps = con.execute(
        "SELECT * FROM portfolio_snapshot WHERE portfolio_id=?", (portfolio_id,)).fetchall()
    assert len(snaps) == 1
    con.close()
