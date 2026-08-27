"""Tests for pipeline.py's migration off watchlist.json as the daily-run
issuer/bond source (Watchlist Tier System follow-up, 2026-08-20). Network
calls (sources.fetch_*) are monkeypatched -- these exercise the real issuer
WHERE watchlist_tier IN (1, 2) loop and issuer_bond secid resolution, not
MOEX itself."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import pipeline  # noqa: E402
from hybond.ingest import sources  # noqa: E402
from hybond.store import db  # noqa: E402

SNAP = date(2026, 8, 20)


def _bond_master_row(secid, isin):
    return {
        "SECID": secid, "ISIN": isin, "SHORTNAME": secid, "FACEVALUE": 1000.0,
        "FACEUNIT": "SUR", "COUPONPERCENT": 15.0, "LISTLEVEL": 3,
        "MATDATE": "2028-01-01", "ISSUESIZE": 1_000_000, "ISSUESIZEPLACED": 1_000_000,
    }


def _marketdata_row(secid, yld=18.0):
    return {"SECID": secid, "LAST": 98.5, "YIELD": yld, "DURATION": 400,
            "VALTODAY": 50000, "NUMTRADES": 10, "SYSTIME": "2026-08-20 12:00:00"}


def _patch_network(monkeypatch, secids, yld=18.0):
    # Floater path (a coupon with a future date and no fixed value yet) --
    # shortest route through pipeline.run()'s per-bond branch, avoids needing
    # a fully correct cashflow schedule for this test's purposes. `yld`
    # overrides MOEX's reported YIELD for every bond -- used by the
    # FLOATER_YIELD_SANITY_MAX tests to simulate a garbage feed value.
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_bond_master_row(s, f"RU_{s}") for s in secids])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [_marketdata_row(s, yld) for s in secids or []])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_security_qualified", lambda secid: False)
    monkeypatch.setattr(sources, "fetch_bondization", lambda secid: {
        "coupons": [{"coupondate": "2030-01-01", "value": None}],
        "amortizations": [],
    })


def _setup_con(tmp_path):
    monkeypatch_db_path = tmp_path / "t.sqlite"
    con = db.connect(monkeypatch_db_path)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    return con, monkeypatch_db_path


def test_tier2_issuer_bonds_get_processed(tmp_path, monkeypatch):
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2 Co (passive)", watchlist_tier=2)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    db.add_issuer_bond(con, "2", "SECID_T2", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1", "SECID_T2"])
    report = pipeline.run(None, snap_date=SNAP)

    secids_in_screen = {r["secid"] for r in report["spread_screen"]}
    assert "SECID_T1" in secids_in_screen
    assert "SECID_T2" in secids_in_screen  # tier=2 got processed, same as tier=1


def test_tier3_issuer_not_processed(tmp_path, monkeypatch):
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.execute("INSERT INTO issuer(issuer_id,name,watchlist_tier) VALUES('3','Excluded Co',3)")
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    db.add_issuer_bond(con, "3", "SECID_T3", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1", "SECID_T3"])
    report = pipeline.run(None, snap_date=SNAP)

    secids_in_screen = {r["secid"] for r in report["spread_screen"]}
    assert "SECID_T1" in secids_in_screen
    assert "SECID_T3" not in secids_in_screen


def test_missing_watchlist_json_does_not_crash(tmp_path, monkeypatch):
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1"])
    nonexistent = tmp_path / "no_such_watchlist.json"
    report = pipeline.run(nonexistent, snap_date=SNAP)  # must not raise
    assert {r["secid"] for r in report["spread_screen"]} == {"SECID_T1"}


def test_none_watchlist_path_does_not_crash(tmp_path, monkeypatch):
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1"])
    report = pipeline.run(None, snap_date=SNAP)  # must not raise
    assert {r["secid"] for r in report["spread_screen"]} == {"SECID_T1"}


def test_watchlist_json_secid_absorbed_into_issuer_bond(tmp_path, monkeypatch):
    """A secid present only in watchlist.json (not yet in issuer_bond) gets
    picked up this run via sync_issuer_bond, and persists into issuer_bond
    for future runs even without watchlist.json."""
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.commit()
    con.close()

    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(
        '{"1": {"name": "Tier1 Co", "sector": null, "secids": ["SECID_FROM_WL"]}}',
        encoding="utf-8")

    _patch_network(monkeypatch, ["SECID_FROM_WL"])
    report = pipeline.run(watchlist_path, snap_date=SNAP)
    assert {r["secid"] for r in report["spread_screen"]} == {"SECID_FROM_WL"}

    con = db.connect(dbpath)
    assert db.list_bonds_for_issuer(con, "1") == ["SECID_FROM_WL"]
    con.close()


# --- floater YIELD sanity bound (2026-08-27 data-quality fix) --------------
# Confirmed live: MOEX's own YIELD field for some floaters occasionally
# returns an implausible value (0%, or hundreds of thousands of percent) --
# found across 3 real secids including a tier=1 watchlist bond (АПРИ 2Р10),
# which then poisoned spread_snapshot's ytm/g_spread_bp/mod_dur for that
# bond. FLOATER_YIELD_SANITY_MAX now rejects the write entirely rather than
# a real distress bond's legitimately high (but plausible) YTM.

def test_floater_implausible_zero_yield_is_not_written(tmp_path, monkeypatch):
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1"], yld=0.0)
    report = pipeline.run(None, snap_date=SNAP)

    assert {r["secid"] for r in report["spread_screen"]} == set()
    con = db.connect(dbpath)
    rows = con.execute("SELECT * FROM spread_snapshot WHERE secid='SECID_T1'").fetchall()
    assert rows == []
    con.close()


def test_floater_implausible_huge_yield_is_not_written(tmp_path, monkeypatch):
    """The other observed garbage shape -- a YIELD several orders of
    magnitude too large (696,957.81% in the real incident), not just zero."""
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1"], yld=696957.81)
    report = pipeline.run(None, snap_date=SNAP)

    assert {r["secid"] for r in report["spread_screen"]} == set()
    con = db.connect(dbpath)
    rows = con.execute("SELECT * FROM spread_snapshot WHERE secid='SECID_T1'").fetchall()
    assert rows == []
    con.close()


def test_floater_plausible_high_distress_yield_is_still_written(tmp_path, monkeypatch):
    """The sanity bound must not reject a real ВДО distress bond's
    legitimately elevated YTM (real АПРИ bonds show 30-45% YTM) -- only
    values no real bond could ever have."""
    con, dbpath = _setup_con(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()

    _patch_network(monkeypatch, ["SECID_T1"], yld=43.4)  # 43.4%, real АПРИ 2Р2 figure
    report = pipeline.run(None, snap_date=SNAP)

    assert {r["secid"] for r in report["spread_screen"]} == {"SECID_T1"}
    con = db.connect(dbpath)
    row = con.execute("SELECT ytm FROM spread_snapshot WHERE secid='SECID_T1'").fetchone()
    assert row["ytm"] == pytest.approx(0.434)
    con.close()
