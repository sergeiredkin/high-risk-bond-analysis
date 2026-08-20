"""Tests for db.py's manual_candidate table (`hybond promote --inn/--name`)
and company_scan_result's decision/decision_reason/decision_at columns
(`hybond scan-company-decide`)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.store import db  # noqa: E402


def _con(tmp_path, name="t.sqlite"):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    return con


# ---- manual_candidate -------------------------------------------------------

def test_manual_candidate_table_created(tmp_path):
    con = _con(tmp_path)
    db.init_manual_candidate(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(manual_candidate)")}
    assert {"inn", "name", "added_at", "note"} <= cols
    con.close()


def test_add_and_list_manual_candidate(tmp_path):
    con = _con(tmp_path)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "7723609647", "ООО «ТЕХНО Лизинг»",
                             note="found via company-scan, hold pending H1 2026")
    con.commit()
    rows = db.list_manual_candidates(con)
    assert len(rows) == 1
    assert rows[0]["inn"] == "7723609647"
    assert rows[0]["name"] == "ООО «ТЕХНО Лизинг»"
    assert rows[0]["note"] == "found via company-scan, hold pending H1 2026"
    con.close()


def test_add_manual_candidate_upserts_by_inn(tmp_path):
    con = _con(tmp_path)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "123", "Old Name", note="first note")
    db.add_manual_candidate(con, "123", "New Name", note="updated note")
    con.commit()
    rows = db.list_manual_candidates(con)
    assert len(rows) == 1  # not two rows -- re-flagging updates in place
    assert rows[0]["name"] == "New Name"
    assert rows[0]["note"] == "updated note"
    con.close()


def test_manual_candidate_excluded_once_already_watchlisted(tmp_path):
    con = _con(tmp_path)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "999", "Some Issuer")
    con.commit()
    assert len(db.list_manual_candidates(con)) == 1

    # Issuer gets actually promoted/watchlisted -- appears in `issuer` table.
    db.upsert_issuer(con, "999", "Some Issuer")
    con.commit()
    assert db.list_manual_candidates(con) == []
    con.close()


def test_manual_candidate_does_not_touch_issuer_table(tmp_path):
    con = _con(tmp_path)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "7723609647", "ООО «ТЕХНО Лизинг»")
    con.commit()
    assert con.execute("SELECT COUNT(*) c FROM issuer").fetchone()["c"] == 0
    con.close()


# ---- company_scan_result decision -------------------------------------------

def _seed_scan(con, decision=None):
    db.init_company_scan(con)
    return db.insert_company_scan_result(
        con, inn="7723609647", query="7723609647", scanned_at="2026-08-17T13:13:30",
        issuer_name="ООО «ТЕХНО Лизинг»", track="Small", issuer_quality="Weak",
        bond_suitability="Watchlist", confidence="Medium", data_coverage_pct=78.0)


def test_scan_decision_columns_exist_after_init(tmp_path):
    con = _con(tmp_path)
    db.init_company_scan(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(company_scan_result)")}
    assert {"decision", "decision_reason", "decision_at"} <= cols
    con.close()


def test_migration_idempotent_on_pre_existing_table(tmp_path):
    con = db.connect(tmp_path / "t2.sqlite")
    con.execute("""
        CREATE TABLE company_scan_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT, inn TEXT, query TEXT NOT NULL,
            scanned_at TEXT NOT NULL
        )
    """)
    con.commit()
    db.init_company_scan(con)
    db.init_company_scan(con)  # second call must not raise
    cols = {r["name"] for r in con.execute("PRAGMA table_info(company_scan_result)")}
    assert {"decision", "decision_reason", "decision_at"} <= cols
    con.close()


def test_set_company_scan_decision_writes_all_three_fields(tmp_path):
    con = _con(tmp_path)
    scan_id = _seed_scan(con)
    con.commit()
    db.set_company_scan_decision(
        con, scan_id, "hold",
        "Q1 2026 OCF negative, ЧИЛ declining, beneficiary undisclosed — "
        "revisit after H1 2026 financials", decided_at="2026-08-18T05:19:06")
    con.commit()
    row = con.execute("SELECT * FROM company_scan_result WHERE id=?", (scan_id,)).fetchone()
    assert row["decision"] == "hold"
    assert "revisit after H1 2026" in row["decision_reason"]
    assert row["decision_at"] == "2026-08-18T05:19:06"
    con.close()


def test_set_company_scan_decision_rejects_invalid_decision(tmp_path):
    con = _con(tmp_path)
    scan_id = _seed_scan(con)
    con.commit()
    with pytest.raises(ValueError):
        db.set_company_scan_decision(con, scan_id, "maybe", "not a real decision")
    con.close()


def test_set_company_scan_decision_rejects_unknown_id(tmp_path):
    con = _con(tmp_path)
    db.init_company_scan(con)
    con.commit()
    with pytest.raises(ValueError):
        db.set_company_scan_decision(con, 999, "hold", "no such row")
    con.close()


def test_set_company_scan_decision_defaults_now_when_not_given(tmp_path):
    con = _con(tmp_path)
    scan_id = _seed_scan(con)
    con.commit()
    db.set_company_scan_decision(con, scan_id, "promote", "looks solid")
    con.commit()
    row = con.execute("SELECT decision_at FROM company_scan_result WHERE id=?",
                       (scan_id,)).fetchone()
    assert row["decision_at"] is not None
    con.close()


def test_untouched_scan_rows_have_null_decision(tmp_path):
    con = _con(tmp_path)
    _seed_scan(con)
    con.commit()
    row = con.execute("SELECT decision, decision_reason, decision_at "
                       "FROM company_scan_result").fetchone()
    assert row["decision"] is None
    assert row["decision_reason"] is None
    assert row["decision_at"] is None
    con.close()
