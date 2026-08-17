"""Tests for the feed_url column + market-wide (issuer_id IS NULL)
market_note rows -- MOEX Listing/CBR Press task (2026-08-17)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond.store import db  # noqa: E402
from export_dashboard_data import market_wide_notes  # noqa: E402


def test_feed_url_column_exists_after_init(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(market_note)")}
    assert "feed_url" in cols
    con.close()


def test_feed_url_migration_idempotent_on_pre_existing_table(tmp_path):
    # Simulates a DB created before feed_url existed (a bare CREATE TABLE
    # matching the OLD schema), then init_market_note() must ALTER TABLE to
    # add it without erroring on a second call.
    con = db.connect(tmp_path / "t2.sqlite")
    con.execute("""
        CREATE TABLE market_note (
            note_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            issuer_id TEXT, occurred TEXT, headline TEXT, summary TEXT,
            url TEXT, severity TEXT, category TEXT, confidence REAL,
            fetched_on TEXT, body_hash TEXT UNIQUE
        )
    """)
    con.commit()
    db.init_market_note(con)  # should ALTER TABLE, not raise
    db.init_market_note(con)  # second call: column already exists, still no error
    cols = {r["name"] for r in con.execute("PRAGMA table_info(market_note)")}
    assert "feed_url" in cols
    con.close()


def test_existing_rows_get_null_feed_url_not_backfilled(tmp_path):
    con = db.connect(tmp_path / "t3.sqlite")
    con.execute("""
        CREATE TABLE market_note (
            note_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            issuer_id TEXT, occurred TEXT, headline TEXT, summary TEXT,
            url TEXT, severity TEXT, category TEXT, confidence REAL,
            fetched_on TEXT, body_hash TEXT UNIQUE
        )
    """)
    con.execute("INSERT INTO market_note(source, headline, body_hash) "
                "VALUES('smartlab_news', 'old row', 'oldhash')")
    con.commit()
    db.init_market_note(con)
    row = con.execute("SELECT feed_url FROM market_note WHERE body_hash='oldhash'").fetchone()
    assert row["feed_url"] is None
    con.close()


def test_insert_market_note_stores_feed_url(tmp_path):
    con = db.connect(tmp_path / "t4.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    db.insert_market_note(
        con, source="moex_listing", issuer_id=None,
        occurred="2026-08-17T09:00:00+03:00", headline="test",
        summary=None, url="http://x", severity=None, category=None,
        confidence=None, fetched_on="2026-08-17", body_hash="h1",
        feed_url="https://moex.com/export/news.aspx?cat=104")
    con.commit()
    row = con.execute("SELECT feed_url, source FROM market_note WHERE body_hash='h1'").fetchone()
    assert row["feed_url"] == "https://moex.com/export/news.aspx?cat=104"
    assert row["source"] == "moex_listing"
    con.close()


def test_insert_market_note_feed_url_defaults_to_none_when_omitted(tmp_path):
    # Existing callers that don't pass feed_url (none currently do after
    # this task, but the kwarg must stay optional) keep working.
    con = db.connect(tmp_path / "t5.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, "123", "Test Issuer")
    ok = db.insert_market_note(
        con, source="smartlab_news", issuer_id="123", occurred=None,
        headline="no feed_url passed", summary=None, url="http://y",
        severity=None, category=None, confidence=None,
        fetched_on="2026-08-17", body_hash="h2")
    assert ok is True
    row = con.execute("SELECT feed_url FROM market_note WHERE body_hash='h2'").fetchone()
    assert row["feed_url"] is None
    con.close()


def test_dedup_by_body_hash_still_works_with_feed_url(tmp_path):
    con = db.connect(tmp_path / "t6.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    first = db.insert_market_note(
        con, source="cbr_press", issuer_id=None, occurred="2026-08-17T09:00:00+03:00",
        headline="dup test", summary=None, url="http://z", severity=None,
        category=None, confidence=None, fetched_on="2026-08-17", body_hash="dup1",
        feed_url="https://www.cbr.ru/rss/RssPress")
    second = db.insert_market_note(
        con, source="cbr_press", issuer_id=None, occurred="2026-08-17T09:00:00+03:00",
        headline="dup test", summary=None, url="http://z", severity=None,
        category=None, confidence=None, fetched_on="2026-08-17", body_hash="dup1",
        feed_url="https://www.cbr.ru/rss/RssPress")
    con.commit()
    assert first is True
    assert second is False  # dedup: same body_hash, no second row
    count = con.execute("SELECT COUNT(*) c FROM market_note WHERE body_hash='dup1'").fetchone()["c"]
    assert count == 1
    con.close()


# ---- market_wide_notes() (export_dashboard_data.py) -----------------------

def _seed(con):
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, "7701234567", "Test Issuer")
    # issuer-scoped row (smartlab_news style) -- must NOT appear in market_wide_notes()
    db.insert_market_note(
        con, source="smartlab_news", issuer_id="7701234567", occurred="2026-08-17T08:00:00+03:00",
        headline="issuer-specific note", summary=None, url="http://a", severity=None,
        category=None, confidence=None, fetched_on="2026-08-17", body_hash="mw1",
        feed_url="https://smart-lab.ru/news/rss/")
    # market-wide rows (issuer_id NULL) -- MUST appear
    db.insert_market_note(
        con, source="moex_listing", issuer_id=None, occurred="2026-08-17T09:00:00+03:00",
        headline="О приостановке торгов ценными бумагами", summary=None, url="http://b",
        severity=None, category=None, confidence=None, fetched_on="2026-08-17", body_hash="mw2",
        feed_url="https://moex.com/export/news.aspx?cat=104")
    db.insert_market_note(
        con, source="cbr_press", issuer_id=None, occurred="2026-08-17T10:00:00+03:00",
        headline="Банк России принял решение снизить ключевую ставку", summary=None,
        url="http://c", severity=None, category=None, confidence=None,
        fetched_on="2026-08-17", body_hash="mw3", feed_url="https://www.cbr.ru/rss/RssPress")
    con.commit()


def test_market_wide_notes_excludes_issuer_scoped_rows(tmp_path):
    con = db.connect(tmp_path / "t7.sqlite")
    _seed(con)
    notes = market_wide_notes(con)
    con.close()
    assert len(notes) == 2
    assert all(n["source"] in ("moex_listing", "cbr_press") for n in notes)


def test_market_wide_notes_pseudo_severity_from_queue_bucket(tmp_path):
    con = db.connect(tmp_path / "t8.sqlite")
    _seed(con)
    notes = market_wide_notes(con)
    con.close()
    by_source = {n["source"]: n for n in notes}
    # "О приостановке торгов" -> MOEX priority pattern -> pseudo-severity 'high'
    assert by_source["moex_listing"]["severity"] == "high"
    # "снизить ключевую ставку" -> CBR priority pattern -> pseudo-severity 'high'
    assert by_source["cbr_press"]["severity"] == "high"


def test_market_wide_notes_real_severity_column_untouched(tmp_path):
    # market_wide_notes() computes a DISPLAY-only pseudo-severity -- the
    # real `severity` column in the DB must stay NULL (never classified).
    con = db.connect(tmp_path / "t9.sqlite")
    _seed(con)
    row = con.execute("SELECT severity FROM market_note WHERE body_hash='mw2'").fetchone()
    con.close()
    assert row["severity"] is None
