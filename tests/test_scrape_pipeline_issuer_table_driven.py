"""Tests for scrape_pipeline.py's migration off watchlist.json as the
issuer-scope source (Watchlist Tier System follow-up, 2026-08-20). Network
calls (scrape.edisclosure) are monkeypatched."""
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import scrape_pipeline  # noqa: E402
from hybond.scrape import edisclosure as ed  # noqa: E402
from hybond.store import db  # noqa: E402

SNAP = date(2026, 8, 20)


def _setup_con(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_filing(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    return con


def _patch_network(monkeypatch):
    monkeypatch.setattr(ed, "fetch_homepage", lambda backend: "")
    monkeypatch.setattr(ed, "search_org_by_inn", lambda inn, backend: None)


def test_tier2_issuer_scraped(tmp_path, monkeypatch):
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "5249111131", "ООО «ХРОМОС Инжиниринг»", watchlist_tier=2)
    con.commit()
    con.close()

    _patch_network(monkeypatch)
    summary = scrape_pipeline.run_scrape(None, fetch_bodies=False, snap_date=SNAP)

    assert "1" in summary["issuers"]
    assert "5249111131" in summary["issuers"]  # tier=2 got scraped, same as tier=1


def test_tier3_issuer_not_scraped(tmp_path, monkeypatch):
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.execute("INSERT INTO issuer(issuer_id,name,watchlist_tier) VALUES('3','Excluded Co',3)")
    con.commit()
    con.close()

    _patch_network(monkeypatch)
    summary = scrape_pipeline.run_scrape(None, fetch_bodies=False, snap_date=SNAP)
    assert "1" in summary["issuers"]
    assert "3" not in summary["issuers"]


def test_missing_watchlist_path_does_not_crash(tmp_path, monkeypatch):
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.commit()
    con.close()

    _patch_network(monkeypatch)
    nonexistent = tmp_path / "no_such_watchlist.json"
    summary = scrape_pipeline.run_scrape(nonexistent, fetch_bodies=False, snap_date=SNAP)
    assert "1" in summary["issuers"]
