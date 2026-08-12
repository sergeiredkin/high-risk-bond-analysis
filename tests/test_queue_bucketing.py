"""Tests for classify_queue_view()/market_note_queue_view()'s priority/
normal/noise bucketing (Part 2 of the Event Pipeline Audit task) -- a
display-only split, nothing here writes to the DB."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import scrape_pipeline  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "1234567890"


def test_classify_queue_view_buckets_filings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bucket.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer", None)
    rows = [
        ("h1", "Присвоение рейтинга эмитенту понижено"),                       # priority
        ("h2", "Плановая выплата купона по графику"),                          # noise
        ("h3", "Решения совета директоров (наблюдательного совета)"),          # normal
    ]
    for body_hash, title in rows:
        db.insert_filing(con, body_hash=body_hash, issuer_id=ISSUER, edisclosure_id=ISSUER,
                          published="2026-07-01", title=title, url="http://x",
                          fetched_on="2026-07-01")
    con.commit()
    con.close()

    view = scrape_pipeline.classify_queue_view()
    assert view["unclassified_count"] == 3
    assert view["priority_count"] == 1
    assert view["noise_count"] == 1
    assert view["normal_count"] == 1
    assert view["priority"][0]["title"] == "Присвоение рейтинга эмитенту понижено"
    assert view["noise"][0]["title"] == "Плановая выплата купона по графику"
    assert view["queue"][0]["title"] == "Решения совета директоров (наблюдательного совета)"


def test_market_note_queue_view_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bucket_mn.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer", None)
    notes = [
        ("n1", "Объявлена реструктуризация выпуска"),  # priority
        ("n2", "Routine coupon payment made on schedule"),  # noise
        ("n3", "Компания опубликовала отчёт за квартал"),  # normal
    ]
    for body_hash, headline in notes:
        db.insert_market_note(con, source="smartlab_news", issuer_id=ISSUER,
                               occurred="2026-07-01T10:00:00+03:00", headline=headline,
                               summary=headline, url="http://x", severity=None,
                               category=None, confidence=None, fetched_on="2026-07-01",
                               body_hash=body_hash)
    con.commit()
    con.close()

    view = scrape_pipeline.market_note_queue_view()
    assert view["unclassified_count"] == 3
    assert view["priority_count"] == 1
    assert view["noise_count"] == 1
    assert view["normal_count"] == 1
