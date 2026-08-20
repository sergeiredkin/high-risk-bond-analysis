"""Tests for financials_pipeline.py's migration off watchlist.json
(Watchlist Tier System follow-up, 2026-08-20) -- pillars must now be
computed for tier=2 issuers too, since that's what Step 4's ХРОМОС
verification actually depends on (pillars are NOT touched by
pipeline.py's own migration, this is a separate loop)."""
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import financials_pipeline  # noqa: E402
from hybond.ingest import nalog  # noqa: E402
from hybond.store import db  # noqa: E402

SNAP = date(2026, 8, 20)


def _fake_record(inn):
    return nalog.FinancialsRecord(
        inn=inn, org_name=f"Org {inn}", period="2025", actual_date="2026-04-01",
        has_consolidated=False, has_audit=True,
        balance={1600: 100000.0, 1400: 50000.0, 1500: 20000.0, 1300: 30000.0},
        financial_result={2110: 90000.0, 2200: 10000.0, 2330: 2000.0},
        funds_movement={4100: 5000.0},
        balance_prev={1600: 90000.0},
    )


def _setup_con(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_pillars(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    return con


def test_tier2_issuer_gets_pillars_computed(tmp_path, monkeypatch):
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "5249111131", "ООО «ХРОМОС Инжиниринг»", watchlist_tier=2)
    con.commit()
    con.close()

    monkeypatch.setattr(nalog, "fetch_financials", lambda inn, backend: _fake_record(inn))
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    summary = financials_pipeline.run_financials(None, api_key="fake-key", snap_date=SNAP)

    assert "error" not in summary
    assert "5249111131" in summary["issuers"]  # tier=2 got pillars, same as tier=1
    assert "1" in summary["issuers"]

    con = db.connect(db.DB_PATH)
    row = con.execute("SELECT * FROM pillars_snapshot WHERE issuer_id='5249111131'").fetchone()
    assert row is not None
    con.close()


def test_tier3_issuer_not_scored(tmp_path, monkeypatch):
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.execute("INSERT INTO issuer(issuer_id,name,watchlist_tier) VALUES('3','Excluded Co',3)")
    con.commit()
    con.close()

    monkeypatch.setattr(nalog, "fetch_financials", lambda inn, backend: _fake_record(inn))
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    summary = financials_pipeline.run_financials(None, api_key="fake-key", snap_date=SNAP)
    assert "3" not in summary["issuers"]
    assert "1" in summary["issuers"]


def test_missing_watchlist_json_does_not_crash(tmp_path, monkeypatch):
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.commit()
    con.close()

    monkeypatch.setattr(nalog, "fetch_financials", lambda inn, backend: _fake_record(inn))
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    nonexistent = tmp_path / "no_such_watchlist.json"
    summary = financials_pipeline.run_financials(nonexistent, api_key="fake-key", snap_date=SNAP)
    assert "1" in summary["issuers"]


def test_leasing_sector_note_uses_db_sector_when_watchlist_absent(tmp_path, monkeypatch):
    """sector used to come only from watchlist.json's v.get('sector') --
    must now fall back to issuer.sector so the leasing-specific coverage
    note still fires without watchlist.json."""
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Leasing Co", watchlist_tier=1, sector="leasing")
    con.commit()
    con.close()

    rec = _fake_record("1")
    rec.financial_result[2330] = 0  # interest_expense = 0, triggers the leasing note
    monkeypatch.setattr(nalog, "fetch_financials", lambda inn, backend: rec)
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    summary = financials_pipeline.run_financials(None, api_key="fake-key", snap_date=SNAP)
    assert "unreliable" in summary["issuers"]["1"]["coverage_note"]
    con = db.connect(db.DB_PATH)
    row = con.execute("SELECT payload FROM pillars_snapshot WHERE issuer_id='1'").fetchone()
    assert "sector_note" in row["payload"]
    con.close()
