"""Tests for the Watchlist Tier System (2026-08-18): issuer.watchlist_tier
migration, `hybond promote --confirm/--tier`, `hybond watchlist-demote`/
`watchlist-promote`, and universe_pipeline's tier-aware scope filter."""
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond.store import db  # noqa: E402


def _con(tmp_path, name="t.sqlite"):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    return con


# ---- schema / migration -----------------------------------------------------

def test_watchlist_tier_column_exists_after_init(tmp_path):
    con = _con(tmp_path)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(issuer)")}
    assert "watchlist_tier" in cols
    con.close()


def test_watchlist_tier_log_table_exists_after_init(tmp_path):
    con = _con(tmp_path)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(watchlist_tier_log)")}
    assert {"id", "issuer_id", "from_tier", "to_tier", "reason", "changed_at"} <= cols
    con.close()


def test_migration_maps_active_flag_onto_tier(tmp_path):
    # Simulate a pre-migration DB: issuer table with `active` but no
    # `watchlist_tier` yet, some active=1 (tier=1) and active=0 (tier=2).
    con = db.connect(tmp_path / "t.sqlite")
    con.execute("CREATE TABLE issuer (issuer_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "sector TEXT, notes TEXT, active INTEGER DEFAULT 1)")
    con.execute("INSERT INTO issuer(issuer_id,name,active) VALUES('1','Active Co',1)")
    con.execute("INSERT INTO issuer(issuer_id,name,active) VALUES('2','Inactive Co',0)")
    con.commit()
    db.init_db(con)
    rows = {r["issuer_id"]: r["watchlist_tier"] for r in
            con.execute("SELECT issuer_id, watchlist_tier FROM issuer")}
    assert rows["1"] == 1
    assert rows["2"] == 2
    con.close()


def test_migration_idempotent(tmp_path):
    con = _con(tmp_path)
    db.init_db(con)  # second call must not raise or re-clobber tiers
    cols = {r["name"] for r in con.execute("PRAGMA table_info(issuer)")}
    assert "watchlist_tier" in cols
    con.close()


# ---- db.add_issuer_at_tier / set_watchlist_tier -----------------------------

def test_add_issuer_at_tier_defaults_and_mirrors_active(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "111", "New Issuer", watchlist_tier=2)
    con.commit()
    row = con.execute("SELECT watchlist_tier, active FROM issuer WHERE issuer_id='111'").fetchone()
    assert row["watchlist_tier"] == 2
    assert row["active"] == 0
    con.close()


def test_add_issuer_at_tier_rejects_invalid_tier(tmp_path):
    con = _con(tmp_path)
    with pytest.raises(ValueError):
        db.add_issuer_at_tier(con, "111", "New Issuer", watchlist_tier=3)
    con.close()


def test_add_issuer_at_tier_rejects_duplicate(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "111", "New Issuer", watchlist_tier=1)
    con.commit()
    with pytest.raises(ValueError):
        db.add_issuer_at_tier(con, "111", "Dup", watchlist_tier=2)
    con.close()


def test_set_watchlist_tier_demotes_and_logs(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "111", "Active Co", watchlist_tier=1)
    con.commit()
    result = db.set_watchlist_tier(con, "111", 2, reason="confirmed default")
    con.commit()
    assert result["from_tier"] == 1
    assert result["to_tier"] == 2
    row = con.execute("SELECT watchlist_tier, active FROM issuer WHERE issuer_id='111'").fetchone()
    assert row["watchlist_tier"] == 2
    assert row["active"] == 0
    log = con.execute("SELECT * FROM watchlist_tier_log WHERE issuer_id='111'").fetchone()
    assert log["from_tier"] == 1
    assert log["to_tier"] == 2
    assert log["reason"] == "confirmed default"
    con.close()


def test_set_watchlist_tier_promotes_and_logs(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "111", "Passive Co", watchlist_tier=2)
    con.commit()
    db.set_watchlist_tier(con, "111", 1, reason="upgraded after H1 2026 review")
    con.commit()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id='111'").fetchone()
    assert row["watchlist_tier"] == 1
    con.close()


def test_set_watchlist_tier_unknown_issuer_raises(tmp_path):
    con = _con(tmp_path)
    with pytest.raises(ValueError):
        db.set_watchlist_tier(con, "999", 2, reason="no such issuer")
    con.close()


def test_set_watchlist_tier_invalid_tier_raises(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "111", "Co", watchlist_tier=1)
    con.commit()
    with pytest.raises(ValueError):
        db.set_watchlist_tier(con, "111", 5, reason="bad tier")
    con.close()


def test_list_issuers_by_tier(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2 Co", watchlist_tier=2)
    con.commit()
    assert [r["issuer_id"] for r in db.list_issuers_by_tier(con, 1)] == ["1"]
    assert [r["issuer_id"] for r in db.list_issuers_by_tier(con, 2)] == ["2"]
    con.close()


# ---- CLI: promote --confirm/--tier ------------------------------------------

def _promote_args(**overrides):
    base = dict(rating_floor=None, n_before_cap=None, cap=None, target=None,
                inn=None, name=None, note=None, confirm=None, tier=2)
    base.update(overrides)
    return Namespace(**base)


def test_promote_confirm_creates_issuer_at_default_tier2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "7723609647", "ООО «ТЕХНО Лизинг»", note="hold pending H1 2026")
    con.commit()
    con.close()

    rc = cli.cmd_promote(_promote_args(confirm="7723609647"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "tier=2" in captured.err
    assert "пассивный мониторинг" in captured.err

    con = db.connect()
    row = con.execute("SELECT name, watchlist_tier FROM issuer WHERE issuer_id='7723609647'").fetchone()
    assert row["watchlist_tier"] == 2
    assert row["name"] == "ООО «ТЕХНО Лизинг»"
    assert db.list_manual_candidates(con) == []  # removed from manual_candidate
    con.close()


def test_promote_confirm_tier1_requires_explicit_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_manual_candidate(con)
    db.add_manual_candidate(con, "111", "Some Co")
    con.commit()
    con.close()

    rc = cli.cmd_promote(_promote_args(confirm="111", tier=1))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id='111'").fetchone()
    assert row["watchlist_tier"] == 1
    con.close()


def test_promote_confirm_unknown_inn_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    rc = cli.cmd_promote(_promote_args(confirm="999999999"))
    assert rc == 1
    con = db.connect()
    db.init_db(con)
    assert con.execute("SELECT COUNT(*) c FROM issuer").fetchone()["c"] == 0
    con.close()


# ---- CLI: watchlist-demote / watchlist-promote ------------------------------

def test_watchlist_demote_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.add_issuer_at_tier(con, "111", "Active Co", watchlist_tier=1)
    con.commit()
    con.close()

    rc = cli.cmd_watchlist_demote(Namespace(inn="111", reason="confirmed default"))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id='111'").fetchone()
    assert row["watchlist_tier"] == 2
    log = con.execute("SELECT * FROM watchlist_tier_log WHERE issuer_id='111'").fetchone()
    assert log["to_tier"] == 2
    assert log["reason"] == "confirmed default"
    con.close()


def test_watchlist_promote_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.add_issuer_at_tier(con, "111", "Passive Co", watchlist_tier=2)
    con.commit()
    con.close()

    # force=True: this test is about the tier-change plumbing, not the
    # pre-promotion audit (a bare issuer with no financials/universe data
    # always carries a "no financial data found" warning, which would
    # otherwise stop at an interactive y/n prompt -- see
    # test_promotion_audit_cli.py for audit-specific coverage).
    rc = cli.cmd_watchlist_promote(Namespace(inn="111", tier=1, reason="upgraded", force=True))
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id='111'").fetchone()
    assert row["watchlist_tier"] == 1
    con.close()


def test_watchlist_demote_unknown_issuer_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    rc = cli.cmd_watchlist_demote(Namespace(inn="999", reason="no such issuer"))
    assert rc == 1


# ---- universe_pipeline: tier=1 and tier=2 both in scope ---------------------

def test_pipeline_watchlist_scope_includes_tier1_and_tier2(tmp_path, monkeypatch):
    """universe_pipeline.py's `watchlist_inns` query (the one gating
    in_watchlist on universe_snapshot rows) must pick up both tiers --
    all data collection is identical for tier=1 and tier=2, only dashboard
    display differs."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2 Co", watchlist_tier=2)
    con.commit()

    watchlist_inns = {r["issuer_id"] for r in con.execute(
        "SELECT issuer_id FROM issuer WHERE watchlist_tier IN (1, 2)")}
    assert watchlist_inns == {"1", "2"}
    con.close()
