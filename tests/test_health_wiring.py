"""Tests for Task 2 of 3 (Pipeline Health Logging System, 2026-08-28): wiring
log_health() into real ingest/compute/export entry points. Each test drives
the actual detection condition through the real code path (not health.py's
API directly -- that's covered by test_pipeline_health.py) and checks the
right pipeline_health row lands.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import cbr_tracker, health, pipeline, scrape_pipeline, universe_pipeline  # noqa: E402
from hybond.ingest import sources  # noqa: E402
from hybond.store import db  # noqa: E402


def _unresolved_codes(con):
    return {r["code"] for r in health.get_unresolved(con, min_level='warn')}


# --- universe_pipeline.py: MOEX_EMPTY / MOEX_COUNT_DROP / GSPREAD_NEGATIVE --

def _uni_con(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_universe(con)
    db.init_rating(con)
    db.init_cbr(con)
    con.commit()
    con.close()
    return dbpath


def test_moex_empty_logged_when_bond_master_returns_nothing(tmp_path, monkeypatch):
    _uni_con(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "fetch_bond_master", lambda: [])
    monkeypatch.setattr(sources, "fetch_marketdata", lambda: [])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_emitent_map", lambda: {})

    universe_pipeline.run_universe(snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    assert "MOEX_EMPTY" in _unresolved_codes(con)
    con.close()


def _master_row(secid, mat="2028-01-01"):
    return {"SECID": secid, "ISIN": f"RU_{secid}", "SHORTNAME": secid,
            "FACEUNIT": "SUR", "MATDATE": mat, "BOARDID": "TQCB",
            "COUPONPERCENT": 15.0, "LISTLEVEL": 3, "OFFERDATE": None,
            "ISQUALIFIEDINVESTORS": False, "COUPONPERIOD": 182}


def _quote_row(secid, yld=20.0, last=95.0):
    return {"SECID": secid, "LAST": last, "YIELD": yld, "DURATION": 400,
            "VALTODAY": 50000, "NUMTRADES": 10}


def test_moex_count_drop_logged_when_priced_count_falls_over_20pct(tmp_path, monkeypatch):
    _uni_con(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_emitent_map", lambda: {})

    secids_day1 = [f"S{i}" for i in range(10)]
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_master_row(s) for s in secids_day1])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda: [_quote_row(s) for s in secids_day1])
    universe_pipeline.run_universe(snap_date=date(2026, 8, 26))

    secids_day2 = [f"S{i}" for i in range(3)]  # 10 -> 3, a 70% drop
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_master_row(s) for s in secids_day2])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda: [_quote_row(s) for s in secids_day2])
    universe_pipeline.run_universe(snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    assert "MOEX_COUNT_DROP" in _unresolved_codes(con)
    con.close()


def test_gspread_negative_logged_for_unexplained_negative_spread(tmp_path, monkeypatch):
    _uni_con(tmp_path, monkeypatch)
    # rf curve pinned at 25% -- a bond yielding 5% (normal coupon, not
    # commodity-linked/thin/degenerate/near-offer) produces a genuinely
    # negative, unexplained g_spread.
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.25), (5.0, 0.25)])
    monkeypatch.setattr(sources, "fetch_emitent_map", lambda: {})
    monkeypatch.setattr(sources, "fetch_bond_master", lambda: [_master_row("S1")])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda: [_quote_row("S1", yld=5.0, last=95.0)])

    universe_pipeline.run_universe(snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    matches = [r for r in health.get_unresolved(con, min_level='warn') if r["code"] == "GSPREAD_NEGATIVE"]
    assert len(matches) == 1
    assert "S1" in matches[0]["detail"]
    con.close()


def test_gspread_negative_not_logged_for_excluded_zero_yield_bond(tmp_path, monkeypatch):
    """ZERO_YIELD_DEGENERATE bonds always look 'negative' vs a positive rf
    curve -- already a known, excluded category, must not fire GSPREAD_NEGATIVE."""
    _uni_con(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_emitent_map", lambda: {})
    monkeypatch.setattr(sources, "fetch_bond_master", lambda: [_master_row("S1")])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda: [_quote_row("S1", yld=0.0, last=3.0)])

    universe_pipeline.run_universe(snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    assert "GSPREAD_NEGATIVE" not in _unresolved_codes(con)
    con.close()


# --- pipeline.py: MOEX_WATCHLIST_EMPTY / MODDUR_NULL -----------------------

def _pipeline_con(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID_T1", source="bond")
    con.commit()
    con.close()
    return dbpath


def _patch_pipeline_network(monkeypatch, dur_days):
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [{"SECID": "SECID_T1", "ISIN": "RU_S1", "SHORTNAME": "SECID_T1",
                                   "FACEVALUE": 1000.0, "FACEUNIT": "SUR", "COUPONPERCENT": 15.0,
                                   "LISTLEVEL": 3, "MATDATE": "2028-01-01",
                                   "ISSUESIZE": 1_000_000, "ISSUESIZEPLACED": 1_000_000}])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [{"SECID": s, "LAST": 98.5, "YIELD": 20.0,
                                                "DURATION": dur_days, "VALTODAY": 50000,
                                                "NUMTRADES": 10, "SYSTIME": "2026-08-27 12:00:00"}
                                               for s in secids or []])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_security_qualified", lambda secid: False)
    monkeypatch.setattr(sources, "fetch_bondization", lambda secid: {
        "coupons": [{"coupondate": "2030-01-01", "value": None}], "amortizations": []})


def test_moex_watchlist_empty_logged_when_marketdata_empty(tmp_path, monkeypatch):
    _pipeline_con(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [{"SECID": "SECID_T1", "ISIN": "RU_S1", "SHORTNAME": "SECID_T1",
                                   "FACEVALUE": 1000.0, "FACEUNIT": "SUR", "COUPONPERCENT": 15.0,
                                   "LISTLEVEL": 3, "MATDATE": "2028-01-01",
                                   "ISSUESIZE": 1_000_000, "ISSUESIZEPLACED": 1_000_000}])
    monkeypatch.setattr(sources, "fetch_marketdata", lambda secids=None: [])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])

    pipeline.run(None, snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    assert "MOEX_WATCHLIST_EMPTY" in _unresolved_codes(con)
    con.close()


def test_moddur_null_logged_when_moex_duration_missing(tmp_path, monkeypatch):
    _pipeline_con(tmp_path, monkeypatch)
    _patch_pipeline_network(monkeypatch, dur_days=None)  # floater path, no DURATION

    pipeline.run(None, snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    matches = [r for r in health.get_unresolved(con, min_level='warn') if r["code"] == "MODDUR_NULL"]
    assert len(matches) == 1
    assert matches[0]["issuer_id"] == "1"
    con.close()


def test_moddur_null_not_logged_when_duration_present(tmp_path, monkeypatch):
    _pipeline_con(tmp_path, monkeypatch)
    _patch_pipeline_network(monkeypatch, dur_days=400)

    pipeline.run(None, snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    assert "MODDUR_NULL" not in _unresolved_codes(con)
    con.close()


# --- scrape_pipeline.py: AZIPI_FEED_DRY -------------------------------------

def test_azipi_feed_dry_logged_after_3_consecutive_dry_days(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_filing(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.commit()
    con.close()

    from hybond.scrape import edisclosure as ed
    monkeypatch.setattr(ed, "fetch_homepage", lambda backend: "")
    monkeypatch.setattr(ed, "search_org_by_inn", lambda issuer_id, backend: None)

    for i in range(3):
        scrape_pipeline.run_scrape(None, fetch_bodies=False,
                                    snap_date=date(2026, 8, 25) + timedelta(days=i))

    con = db.connect(db.DB_PATH)
    assert "AZIPI_FEED_DRY" in _unresolved_codes(con)
    con.close()


def test_azipi_feed_dry_not_logged_when_feed_was_active_2_of_last_3_days(tmp_path, monkeypatch):
    """The feed was genuinely active 2 days ago and yesterday (real filing
    rows exist), then goes quiet today -- only 1 dry day so far, must not
    fire yet. A totally empty, never-populated filing table can't be
    distinguished from a genuine 3-day dry streak by this window query
    alone; that's a fresh-install edge case, not the scenario this guards."""
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_filing(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.execute(
        "INSERT INTO filing(body_hash,issuer_id,fetched_on,title) VALUES (?,?,?,?)",
        ("h1", "1", "2026-08-25", "Old filing"))
    con.execute(
        "INSERT INTO filing(body_hash,issuer_id,fetched_on,title) VALUES (?,?,?,?)",
        ("h2", "1", "2026-08-26", "Yesterday's filing"))
    con.commit()
    con.close()

    from hybond.scrape import edisclosure as ed
    monkeypatch.setattr(ed, "fetch_homepage", lambda backend: "")
    monkeypatch.setattr(ed, "search_org_by_inn", lambda issuer_id, backend: None)

    scrape_pipeline.run_scrape(None, fetch_bodies=False, snap_date=date(2026, 8, 27))

    con = db.connect(db.DB_PATH)
    assert "AZIPI_FEED_DRY" not in _unresolved_codes(con)
    con.close()


# --- cbr_tracker.py: check_and_fetch_today() bug fix + CBR_FETCH_FAIL/STALE -

class _FixedDate(date):
    _fixed = date(2026, 2, 13)

    @classmethod
    def today(cls):
        return cls._fixed


def test_check_and_fetch_today_meeting_day_does_not_raise_nameerror(tmp_path, monkeypatch):
    """Regression test for the missing `result = fetch_meeting(...)` line --
    check_and_fetch_today() used to raise NameError on any real scheduled
    meeting/résюме/comment day."""
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    _FixedDate._fixed = date(2026, 2, 13)  # a real CBR_MEETINGS_2026 date
    monkeypatch.setattr(cbr_tracker, "date", _FixedDate)
    monkeypatch.setattr(cbr_tracker, "fetch_meeting",
                         lambda meeting_date, is_anchor: {
                             "meeting_date": meeting_date, "is_anchor": int(is_anchor),
                             "fetched_on": "2026-02-13"})

    result = cbr_tracker.check_and_fetch_today()  # must not raise

    assert result["action"] == "meeting"
    assert result["meeting_date"] == "2026-02-13"


def test_cbr_fetch_fail_logged_when_press_release_missing_on_meeting_day(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    _FixedDate._fixed = date(2026, 2, 13)
    monkeypatch.setattr(cbr_tracker, "date", _FixedDate)
    monkeypatch.setattr(cbr_tracker, "fetch_meeting",
                         lambda meeting_date, is_anchor: {
                             "meeting_date": meeting_date, "is_anchor": int(is_anchor),
                             "fetched_on": "2026-02-13"})  # no press_release_text

    cbr_tracker.check_and_fetch_today()

    con = db.connect(db.DB_PATH)
    assert "CBR_FETCH_FAIL" in _unresolved_codes(con)
    con.close()


def test_cbr_stale_logged_when_resume_missing_on_resume_day(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    _FixedDate._fixed = date(2026, 2, 26)  # 2026-02-13 meeting's resume_date
    monkeypatch.setattr(cbr_tracker, "date", _FixedDate)
    monkeypatch.setattr(cbr_tracker, "fetch_meeting",
                         lambda meeting_date, is_anchor: {
                             "meeting_date": meeting_date, "is_anchor": int(is_anchor),
                             "fetched_on": "2026-02-26",
                             "press_release_text": "...", "nabiullina_speech_text": "..."})

    cbr_tracker.check_and_fetch_today()

    con = db.connect(db.DB_PATH)
    assert "CBR_STALE" in _unresolved_codes(con)
    con.close()


# --- export_dashboard_data.py: EXPORT_STALE ---------------------------------
# EXPORT_EMPTY/EXPORT_STALE both live in main(), which reads watchlist.json/
# report.json from the real project root and isn't independently importable
# as a pure function -- covered instead by DoD #11's live-verify run against
# the real DB/export below, not a unit test here.
