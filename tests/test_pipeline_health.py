"""Tests for hybond.health (Pipeline Health Logging System, Task 1 of 3,
2026-08-28) -- schema + logger module. Covers log_health validation/DB-error
handling, rotate_pipeline_health's retention rules, get_unresolved ordering,
resolve/resolve_code, summary(), and one integration test wiring the real
FLOATER_YIELD_SANITY_MAX guard in pipeline.py through to get_unresolved()."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import health, pipeline  # noqa: E402
from hybond.ingest import sources  # noqa: E402
from hybond.store import db  # noqa: E402


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_pipeline_health(con)
    return con


# --- log_health -------------------------------------------------------

def test_log_health_valid_input_writes_row(tmp_path):
    con = _con(tmp_path)
    row_id = health.log_health(con, 'moex', 'ingest', 'error',
                                'MOEX_EMPTY', 'No bonds returned', issuer_id='1')
    assert row_id is not None
    row = con.execute("SELECT * FROM pipeline_health WHERE id=?", (row_id,)).fetchone()
    assert row["source"] == "moex"
    assert row["stage"] == "ingest"
    assert row["level"] == "error"
    assert row["code"] == "MOEX_EMPTY"
    assert row["detail"] == "No bonds returned"
    assert row["issuer_id"] == "1"
    assert row["resolved"] == 0


@pytest.mark.parametrize("field,value", [
    ("source", "bogus_source"),
    ("stage", "bogus_stage"),
    ("level", "bogus_level"),
])
def test_log_health_invalid_input_raises(tmp_path, field, value):
    con = _con(tmp_path)
    kwargs = dict(source='moex', stage='ingest', level='error', code='X')
    kwargs[field] = value
    with pytest.raises(ValueError):
        health.log_health(con, kwargs["source"], kwargs["stage"], kwargs["level"], kwargs["code"])


def test_log_health_db_error_logs_to_stderr_not_raise(tmp_path, capsys):
    con = _con(tmp_path)
    con.close()  # force a DB error on the next operation
    row_id = health.log_health(con, 'moex', 'ingest', 'error', 'MOEX_EMPTY')
    assert row_id is None
    captured = capsys.readouterr()
    assert "DB error" in captured.err


# --- get_unresolved -----------------------------------------------------

def test_get_unresolved_level_ordering_and_limit(tmp_path):
    con = _con(tmp_path)
    health.log_health(con, 'moex', 'ingest', 'ok', 'OK1')
    health.log_health(con, 'moex', 'ingest', 'warn', 'WARN1')
    health.log_health(con, 'moex', 'ingest', 'error', 'ERR1')
    health.log_health(con, 'moex', 'ingest', 'critical', 'CRIT1')

    at_warn = health.get_unresolved(con, min_level='warn')
    assert {r["code"] for r in at_warn} == {"WARN1", "ERR1", "CRIT1"}

    at_error = health.get_unresolved(con, min_level='error')
    assert {r["code"] for r in at_error} == {"ERR1", "CRIT1"}

    at_critical = health.get_unresolved(con, min_level='critical')
    assert {r["code"] for r in at_critical} == {"CRIT1"}

    for i in range(5):
        health.log_health(con, 'moex', 'ingest', 'warn', f'MANY{i}')
    limited = health.get_unresolved(con, min_level='warn', limit=3)
    assert len(limited) == 3


def test_get_unresolved_excludes_resolved(tmp_path):
    con = _con(tmp_path)
    row_id = health.log_health(con, 'moex', 'ingest', 'error', 'ERR1')
    health.resolve(con, row_id)
    assert health.get_unresolved(con, min_level='warn') == []


# --- resolve / resolve_code ----------------------------------------------

def test_resolve_marks_single_entry(tmp_path):
    con = _con(tmp_path)
    row_id = health.log_health(con, 'moex', 'ingest', 'error', 'ERR1')
    health.resolve(con, row_id, note="fixed manually")
    row = con.execute("SELECT * FROM pipeline_health WHERE id=?", (row_id,)).fetchone()
    assert row["resolved"] == 1
    assert row["resolved_note"] == "fixed manually"
    assert row["resolved_at"] is not None


def test_resolve_code_bulk_resolves_and_returns_count(tmp_path):
    con = _con(tmp_path)
    health.log_health(con, 'moex', 'ingest', 'warn', 'DUPCODE')
    health.log_health(con, 'moex', 'ingest', 'warn', 'DUPCODE')
    health.log_health(con, 'moex', 'ingest', 'warn', 'OTHERCODE')

    n = health.resolve_code(con, 'DUPCODE', note="root cause fixed")
    assert n == 2
    remaining = health.get_unresolved(con, min_level='warn')
    assert {r["code"] for r in remaining} == {"OTHERCODE"}


# --- rotate_pipeline_health -----------------------------------------------

def _insert_aged(con, level, code, days_old, resolved=0):
    con.execute(
        "INSERT INTO pipeline_health(recorded_at,source,stage,level,code,resolved) "
        "VALUES(datetime('now', ?), 'moex', 'ingest', ?, ?, ?)",
        (f"-{days_old} days", level, code, resolved),
    )
    con.commit()


def test_rotate_deletes_old_ok_warn_preserves_unresolved_errors(tmp_path):
    con = _con(tmp_path)
    _insert_aged(con, 'ok', 'OLD_OK', days_old=10)
    _insert_aged(con, 'warn', 'OLD_WARN', days_old=10)
    _insert_aged(con, 'error', 'OLD_UNRESOLVED_ERR', days_old=10, resolved=0)
    _insert_aged(con, 'error', 'OLD_RESOLVED_ERR', days_old=10, resolved=1)
    _insert_aged(con, 'warn', 'RECENT_WARN', days_old=1)

    result = health.rotate_pipeline_health(con, days=7)

    remaining_codes = {r["code"] for r in con.execute("SELECT code FROM pipeline_health")}
    assert "OLD_OK" not in remaining_codes
    assert "OLD_WARN" not in remaining_codes
    assert "OLD_RESOLVED_ERR" not in remaining_codes
    assert "OLD_UNRESOLVED_ERR" in remaining_codes  # never deleted
    assert "RECENT_WARN" in remaining_codes  # not old enough

    assert result["rows_deleted"] == 3
    assert result["errors_preserved"] == 1

    rotated_row = con.execute(
        "SELECT * FROM pipeline_health WHERE code='LOG_ROTATED'").fetchone()
    assert rotated_row is not None
    assert rotated_row["level"] == "ok"
    assert rotated_row["id"] == result["rotation_id"]


# --- summary --------------------------------------------------------------

def test_summary_ok_when_nothing_unresolved(tmp_path):
    con = _con(tmp_path)
    result = health.summary(con)
    assert result["status"] == "ok"
    assert result["unresolved_counts"] == {"warn": 0, "error": 0, "critical": 0}
    assert result["latest_error"] is None
    assert result["last_rotation"] is None


def test_summary_status_is_worst_unresolved_level(tmp_path):
    con = _con(tmp_path)
    health.log_health(con, 'moex', 'ingest', 'warn', 'W1')
    result = health.summary(con)
    assert result["status"] == "warn"

    health.log_health(con, 'moex', 'ingest', 'error', 'E1', detail='boom')
    result = health.summary(con)
    assert result["status"] == "error"
    assert result["latest_error"]["code"] == "E1"

    health.log_health(con, 'moex', 'ingest', 'critical', 'C1')
    result = health.summary(con)
    assert result["status"] == "critical"
    assert result["unresolved_counts"] == {"warn": 1, "error": 1, "critical": 1}


def test_summary_reports_last_rotation(tmp_path):
    con = _con(tmp_path)
    health.rotate_pipeline_health(con, days=7)
    result = health.summary(con)
    assert result["last_rotation"] is not None


# --- integration: real pipeline guard path --------------------------------

SNAP = date(2026, 8, 27)


def _bond_master_row(secid, isin):
    return {
        "SECID": secid, "ISIN": isin, "SHORTNAME": secid, "FACEVALUE": 1000.0,
        "FACEUNIT": "SUR", "COUPONPERCENT": 15.0, "LISTLEVEL": 3,
        "MATDATE": "2028-01-01", "ISSUESIZE": 1_000_000, "ISSUESIZEPLACED": 1_000_000,
    }


def _marketdata_row(secid, yld):
    return {"SECID": secid, "LAST": 98.5, "YIELD": yld, "DURATION": 400,
            "VALTODAY": 50000, "NUMTRADES": 10, "SYSTIME": "2026-08-27 12:00:00"}


def test_ytm_sanity_fail_wired_through_real_pipeline_guard(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    monkeypatch.setattr(db, "DB_PATH", dbpath)
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_bond_master_row("SECID_T1", "RU_SECID_T1")])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [_marketdata_row(s, 696957.81) for s in secids or []])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_security_qualified", lambda secid: False)
    monkeypatch.setattr(sources, "fetch_bondization", lambda secid: {
        "coupons": [{"coupondate": "2030-01-01", "value": None}],
        "amortizations": [],
    })

    pipeline.run(None, snap_date=SNAP)

    con = db.connect(dbpath)
    db.init_pipeline_health(con)
    unresolved = health.get_unresolved(con, min_level='warn')
    con.close()

    matches = [r for r in unresolved if r["code"] == "YTM_SANITY_FAIL"]
    assert len(matches) == 1
    assert matches[0]["source"] == "moex"
    assert matches[0]["stage"] == "compute"
    assert matches[0]["issuer_id"] == "1"
    assert "SECID_T1" in matches[0]["detail"]


# --- log_health_once_today (2026-08-28 fix: EXPORT_STALE dedup) -----------

def test_log_health_once_today_skips_on_second_call_same_day(tmp_path):
    con = _con(tmp_path)
    first_id = health.log_health_once_today(con, 'dashboard', 'export', 'warn',
                                             'EXPORT_STALE', 'first call')
    second_id = health.log_health_once_today(con, 'dashboard', 'export', 'warn',
                                              'EXPORT_STALE', 'second call, same day')
    assert first_id is not None
    assert second_id is None
    rows = con.execute("SELECT * FROM pipeline_health WHERE code='EXPORT_STALE'").fetchall()
    assert len(rows) == 1
    assert rows[0]["detail"] == "first call"


def test_log_health_once_today_logs_again_once_prior_entry_resolved(tmp_path):
    con = _con(tmp_path)
    first_id = health.log_health_once_today(con, 'dashboard', 'export', 'warn',
                                             'EXPORT_STALE', 'first call')
    health.resolve(con, first_id)
    second_id = health.log_health_once_today(con, 'dashboard', 'export', 'warn',
                                              'EXPORT_STALE', 'second call, after resolve')
    assert second_id is not None
    assert second_id != first_id


# --- FLOATER_YIELD_SANITY_MIN (2026-08-28 fix: negative YTM lower bound) --

def test_negative_ytm_below_min_rejected_and_logged(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    monkeypatch.setattr(db, "DB_PATH", dbpath)
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_bond_master_row("SECID_T1", "RU_SECID_T1")])
    # -83% -- a real observed garbage value (see the mislabeled real
    # ytm=-0.83 entry this fix corrects), well below FLOATER_YIELD_SANITY_MIN
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [_marketdata_row(s, -83.0) for s in secids or []])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_security_qualified", lambda secid: False)
    monkeypatch.setattr(sources, "fetch_bondization", lambda secid: {
        "coupons": [{"coupondate": "2030-01-01", "value": None}],
        "amortizations": [],
    })

    report = pipeline.run(None, snap_date=SNAP)

    assert {r["secid"] for r in report["spread_screen"]} == set()
    con = db.connect(dbpath)
    db.init_pipeline_health(con)
    unresolved = health.get_unresolved(con, min_level='warn')
    con.close()

    matches = [r for r in unresolved if r["code"] == "YTM_SANITY_FAIL"]
    assert len(matches) == 1
    assert "below FLOATER_YIELD_SANITY_MIN" in matches[0]["detail"]
    assert "SECID_T1" in matches[0]["detail"]
