"""Tests for db.py's Ollama auto-classification support: the event table's
classified_by/confidence/needs_review migration, insert_event()'s extended
kwargs, get_auto_classify_queue()/get_auto_classify_market_notes(), and
events_needing_review()/resolve_event_review()."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.store import db  # noqa: E402


def _con(tmp_path, name="t.sqlite"):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    return con


# ---- schema migration -------------------------------------------------------

def test_event_table_has_new_columns_after_init(tmp_path):
    con = _con(tmp_path)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(event)")}
    assert {"classified_by", "confidence", "needs_review"} <= cols
    con.close()


def test_migration_idempotent_on_pre_existing_event_table(tmp_path):
    con = db.connect(tmp_path / "t2.sqlite")
    con.execute("""
        CREATE TABLE issuer (issuer_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                              sector TEXT, notes TEXT)
    """)
    con.execute("""
        CREATE TABLE event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, issuer_id TEXT,
            occurred TEXT, category TEXT, severity TEXT, summary TEXT,
            source_url TEXT
        )
    """)
    con.commit()
    db.init_db(con)
    db.init_db(con)  # second call must not raise
    cols = {r["name"] for r in con.execute("PRAGMA table_info(event)")}
    assert {"classified_by", "confidence", "needs_review"} <= cols
    con.close()


def test_existing_manual_rows_stay_null_not_backfilled(tmp_path):
    con = _con(tmp_path)
    db.upsert_issuer(con, "1", "Test Issuer")
    # Old-style manual insert_event() call, pre-dating the new columns.
    db.insert_event(con, issuer_id="1", occurred="2026-01-01", category="routine",
                     severity="noise", summary="Old manual row", source_url="http://x")
    con.commit()
    row = con.execute("SELECT classified_by, confidence, needs_review FROM event "
                       "WHERE summary='Old manual row'").fetchone()
    assert row["classified_by"] is None
    assert row["confidence"] is None
    assert row["needs_review"] == 0
    con.close()


# ---- insert_event() extended kwargs ----------------------------------------

def test_insert_event_stores_new_kwargs(tmp_path):
    con = _con(tmp_path)
    db.upsert_issuer(con, "1", "Test Issuer")
    eid = db.insert_event(
        con, issuer_id="1", occurred="2026-08-17", category="coupon_event",
        severity="critical", summary="Пропуск купона", source_url="http://x",
        classified_by="ollama:qwen3.5:latest", confidence=0.91, needs_review=1)
    con.commit()
    row = con.execute("SELECT * FROM event WHERE event_id=?", (eid,)).fetchone()
    assert row["classified_by"] == "ollama:qwen3.5:latest"
    assert row["confidence"] == 0.91
    assert row["needs_review"] == 1
    con.close()


def test_insert_event_kwargs_not_silently_dropped():
    # Regression guard for the audit finding: the original hardcoded 6-col
    # INSERT would accept classified_by/confidence/needs_review in **f
    # without erroring, but the column list wouldn't include them -- so they
    # were being silently discarded rather than written. Confirmed via the
    # SQL text itself, not just behavior, so a future edit can't reintroduce
    # the silent-drop bug undetected.
    import inspect
    src = inspect.getsource(db.insert_event)
    assert "classified_by" in src and "confidence" in src and "needs_review" in src


# ---- get_auto_classify_queue() ---------------------------------------------

def test_get_auto_classify_queue_joins_issuer_and_filters_classified(tmp_path):
    con = _con(tmp_path)
    db.init_filing(con)
    db.upsert_issuer(con, "1", "Контрол Лизинг")
    db.insert_filing(con, body_hash="h1", issuer_id="1", edisclosure_id="1",
                      published="2026-08-17", title="Присвоен рейтинг эмитенту",
                      url="http://a", fetched_on="2026-08-17")
    db.insert_filing(con, body_hash="h2", issuer_id="1", edisclosure_id="1",
                      published="2026-08-16", title="Плановая выплата купона",
                      url="http://b", fetched_on="2026-08-16")
    db.mark_classified(con, "h2")  # already classified -- must be excluded
    con.commit()

    rows = db.get_auto_classify_queue(con)
    assert len(rows) == 1
    assert rows[0]["body_hash"] == "h1"
    assert rows[0]["issuer_name"] == "Контрол Лизинг"
    assert rows[0]["title"] == "Присвоен рейтинг эмитенту"
    con.close()


def test_get_auto_classify_queue_priority_ordered_first(tmp_path):
    con = _con(tmp_path)
    db.init_filing(con)
    db.upsert_issuer(con, "1", "Test Issuer")
    db.insert_filing(con, body_hash="normal1", issuer_id="1", edisclosure_id="1",
                      published="2026-08-17", title="Обычное раскрытие",
                      url="http://a", fetched_on="2026-08-17")
    db.insert_filing(con, body_hash="prio1", issuer_id="1", edisclosure_id="1",
                      published="2026-08-16", title="Присвоение рейтинга эмитенту понижено",
                      url="http://b", fetched_on="2026-08-16")
    con.commit()

    rows = db.get_auto_classify_queue(con)
    assert rows[0]["body_hash"] == "prio1"  # priority pattern -> first despite older date


def test_get_auto_classify_queue_tier1_ordered_before_tier2(tmp_path):
    """Sub-task A: tier=1 (active watchlist) issuers' filings must sort
    ahead of tier=2's, even when the tier=2 filing has a higher-priority
    content pattern -- tier is the primary sort key, content bucket is
    secondary."""
    con = _con(tmp_path)
    db.init_filing(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2 Co", watchlist_tier=2)
    db.insert_filing(con, body_hash="t2_priority", issuer_id="2", edisclosure_id="1",
                      published="2026-08-17", title="Присвоение рейтинга эмитенту понижено",
                      url="http://a", fetched_on="2026-08-17")
    db.insert_filing(con, body_hash="t1_normal", issuer_id="1", edisclosure_id="1",
                      published="2026-08-16", title="Обычное раскрытие",
                      url="http://b", fetched_on="2026-08-16")
    con.commit()

    rows = db.get_auto_classify_queue(con)
    assert rows[0]["body_hash"] == "t1_normal"  # tier=1 first despite being content-normal
    assert rows[1]["body_hash"] == "t2_priority"


def test_get_auto_classify_queue_priority_only_filters(tmp_path):
    con = _con(tmp_path)
    db.init_filing(con)
    db.upsert_issuer(con, "1", "Test Issuer")
    db.insert_filing(con, body_hash="normal1", issuer_id="1", edisclosure_id="1",
                      published="2026-08-17", title="Обычное раскрытие",
                      url="http://a", fetched_on="2026-08-17")
    db.insert_filing(con, body_hash="prio1", issuer_id="1", edisclosure_id="1",
                      published="2026-08-16", title="Объявлена реструктуризация выпуска",
                      url="http://b", fetched_on="2026-08-16")
    con.commit()

    rows = db.get_auto_classify_queue(con, priority_only=True)
    assert len(rows) == 1
    assert rows[0]["body_hash"] == "prio1"
    con.close()


def test_get_auto_classify_queue_respects_limit(tmp_path):
    con = _con(tmp_path)
    db.init_filing(con)
    db.upsert_issuer(con, "1", "Test Issuer")
    for i in range(5):
        db.insert_filing(con, body_hash=f"h{i}", issuer_id="1", edisclosure_id="1",
                          published=f"2026-08-{10+i}", title=f"Раскрытие {i}",
                          url="http://a", fetched_on="2026-08-17")
    con.commit()
    rows = db.get_auto_classify_queue(con, limit=2)
    assert len(rows) == 2
    con.close()


# ---- get_auto_classify_market_notes() --------------------------------------

def test_get_auto_classify_market_notes_filters_severity_not_null(tmp_path):
    con = _con(tmp_path)
    db.init_market_note(con)
    db.upsert_issuer(con, "1", "Test Issuer")
    db.insert_market_note(con, source="moex_listing", issuer_id=None,
                           occurred="2026-08-17T09:00:00+03:00",
                           headline="О приостановке торгов ценными бумагами",
                           summary=None, url="http://a", severity=None, category=None,
                           confidence=None, fetched_on="2026-08-17", body_hash="mn1")
    db.insert_market_note(con, source="smartlab_news", issuer_id="1",
                           occurred="2026-08-17T09:00:00+03:00", headline="Уже классифицировано",
                           summary=None, url="http://b", severity="low", category="routine",
                           confidence=0.9, fetched_on="2026-08-17", body_hash="mn2")
    con.commit()

    rows = db.get_auto_classify_market_notes(con)
    assert len(rows) == 1
    assert rows[0]["note_id"]
    assert rows[0]["title"] == "О приостановке торгов ценными бумагами"
    assert rows[0]["published"] == "2026-08-17T09:00:00+03:00"
    con.close()


def test_get_auto_classify_market_notes_issuer_name_none_when_market_wide(tmp_path):
    con = _con(tmp_path)
    db.init_market_note(con)
    db.insert_market_note(con, source="cbr_press", issuer_id=None,
                           occurred="2026-08-17T10:00:00+03:00",
                           headline="Банк России принял решение по ставке",
                           summary=None, url="http://c", severity=None, category=None,
                           confidence=None, fetched_on="2026-08-17", body_hash="mn3")
    con.commit()
    rows = db.get_auto_classify_market_notes(con)
    assert len(rows) == 1
    assert rows[0]["issuer_name"] is None
    con.close()


# ---- events_needing_review() / resolve_event_review() ---------------------

def _seed_event(con, needs_review=1):
    db.upsert_issuer(con, "1", "Контрол Лизинг")
    return db.insert_event(
        con, issuer_id="1", occurred="2026-08-17", category="insolvency",
        severity="critical", summary="ФНС заблокировала счета эмитента",
        source_url="http://x", classified_by="ollama:qwen3.5:latest",
        confidence=0.73, needs_review=needs_review)


def test_events_needing_review_only_returns_flagged(tmp_path):
    con = _con(tmp_path)
    _seed_event(con, needs_review=1)
    db.upsert_issuer(con, "2", "Other Issuer")
    db.insert_event(con, issuer_id="2", occurred="2026-08-16", category="routine",
                     severity="noise", summary="Плановое раскрытие", source_url="http://y",
                     classified_by="ollama:qwen3.5:latest", confidence=0.95, needs_review=0)
    con.commit()

    rows = db.events_needing_review(con)
    assert len(rows) == 1
    assert rows[0]["summary"] == "ФНС заблокировала счета эмитента"
    assert rows[0]["issuer_name"] == "Контрол Лизинг"
    con.close()


def test_resolve_event_review_accept_clears_flag_without_changing_fields(tmp_path):
    con = _con(tmp_path)
    eid = _seed_event(con)
    con.commit()
    db.resolve_event_review(con, eid)
    con.commit()
    row = con.execute("SELECT * FROM event WHERE event_id=?", (eid,)).fetchone()
    assert row["needs_review"] == 0
    assert row["category"] == "insolvency"
    assert row["severity"] == "critical"
    con.close()


def test_resolve_event_review_edit_updates_fields_and_clears_flag(tmp_path):
    con = _con(tmp_path)
    eid = _seed_event(con)
    con.commit()
    db.resolve_event_review(con, eid, category="governance", severity="high")
    con.commit()
    row = con.execute("SELECT * FROM event WHERE event_id=?", (eid,)).fetchone()
    assert row["needs_review"] == 0
    assert row["category"] == "governance"
    assert row["severity"] == "high"
    con.close()
