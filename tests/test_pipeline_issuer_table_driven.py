"""Tests for pipeline.py's migration off watchlist.json as the daily-run
issuer/bond source (Watchlist Tier System follow-up, 2026-08-20). Network
calls (sources.fetch_*) are monkeypatched -- these exercise the real issuer
WHERE watchlist_tier IN (1, 2) loop and issuer_bond secid resolution, not
MOEX itself."""
import sys
from pathlib import Path
from datetime import date

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


def _marketdata_row(secid):
    return {"SECID": secid, "LAST": 98.5, "YIELD": 18.0, "DURATION": 400,
            "VALTODAY": 50000, "NUMTRADES": 10, "SYSTIME": "2026-08-20 12:00:00"}


def _patch_network(monkeypatch, secids):
    # Floater path (a coupon with a future date and no fixed value yet) --
    # shortest route through pipeline.run()'s per-bond branch, avoids needing
    # a fully correct cashflow schedule for this test's purposes.
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_bond_master_row(s, f"RU_{s}") for s in secids])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [_marketdata_row(s) for s in secids or []])
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
