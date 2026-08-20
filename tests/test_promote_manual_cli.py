"""CLI-level tests for `hybond promote --inn/--name` (manual-candidate flag
mode) and `hybond scan-company-decide`."""
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond.store import db  # noqa: E402


def test_promote_inn_name_flags_a_candidate_without_touching_watchlist(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    args = Namespace(rating_floor=None, n_before_cap=None, cap=None, target=None,
                      inn="7723609647", name="ООО «ТЕХНО Лизинг»", note="found via company-scan")
    rc = cli.cmd_promote(args)
    assert rc == 0

    con = db.connect()
    rows = db.list_manual_candidates(con)
    assert len(rows) == 1
    assert rows[0]["inn"] == "7723609647"
    assert con.execute("SELECT COUNT(*) c FROM issuer").fetchone()["c"] == 0
    con.close()


def test_promote_inn_without_name_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t2.sqlite")
    args = Namespace(rating_floor=None, n_before_cap=None, cap=None, target=None,
                      inn="123", name=None, note=None)
    rc = cli.cmd_promote(args)
    assert rc == 1
    con = db.connect()
    db.init_manual_candidate(con)
    assert db.list_manual_candidates(con) == []
    con.close()


def test_promote_normal_run_surfaces_manual_candidates_in_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t3.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "7723609647", "ООО «ТЕХНО Лизинг»", note="Q1 2026 concern")
    con.commit()
    con.close()

    args = Namespace(rating_floor=None, n_before_cap=None, cap=None, target=None,
                      inn=None, name=None, note=None)
    rc = cli.cmd_promote(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "ТЕХНО Лизинг" in captured.err
    assert "manually-flagged candidate" in captured.err


def test_scan_company_decide_cli_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t4.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_company_scan(con)
    scan_id = db.insert_company_scan_result(
        con, inn="7723609647", query="7723609647", scanned_at="2026-08-17T13:13:30",
        issuer_name="ООО «ТЕХНО Лизинг»", track="Small", issuer_quality="Weak",
        bond_suitability="Watchlist", confidence="Medium", data_coverage_pct=78.0)
    con.commit()
    con.close()

    args = Namespace(id=scan_id, decision="hold", reason="Q1 2026 OCF negative")
    rc = cli.cmd_scan_company_decide(args)
    assert rc == 0

    con = db.connect()
    row = con.execute("SELECT decision, decision_reason FROM company_scan_result "
                       "WHERE id=?", (scan_id,)).fetchone()
    assert row["decision"] == "hold"
    assert row["decision_reason"] == "Q1 2026 OCF negative"
    con.close()


def test_scan_company_decide_cli_unknown_id_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t5.sqlite")
    args = Namespace(id=999, decision="hold", reason="no such row")
    rc = cli.cmd_scan_company_decide(args)
    assert rc == 1
