"""Tests for scrape/smartlab_news.py (smart-lab.ru's general news RSS feed --
distinct from smartlab.py's bonds-forum feed). Parser tested against a saved
RSS-shape fixture, not a live fetch.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.scrape import smartlab_news as sln  # noqa: E402
from hybond.store import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Shaped like the real smart-lab.ru/news/rss/ feed: standard RSS 2.0,
# <item> with title/link/pubDate/description. Confirmed live 2026-07-30.
RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>smart-lab.ru news</title>
  <item>
    <title>АФК Система понизила прогноз по выручке на 2026 год</title>
    <link>https://smart-lab.ru/blog/1334600.php</link>
    <pubDate>Thu, 30 Jul 2026 11:05:00 +0300</pubDate>
    <description>Компания снизила прогноз из-за роста процентных расходов</description>
  </item>
  <item>
    <title>Итоги дня на бирже</title>
    <link>https://smart-lab.ru/blog/1334601.php</link>
    <pubDate>Thu, 30 Jul 2026 11:03:00 +0300</pubDate>
    <description>Whoosh нарастил парк самокатов на 15% по итогам полугодия</description>
  </item>
  <item>
    <title>Индекс Мосбиржи растёт на открытии</title>
    <link>https://smart-lab.ru/blog/1334602.php</link>
    <pubDate>Thu, 30 Jul 2026 10:47:00 +0300</pubDate>
    <description>Индекс Мосбиржи на 10:45 мск растёт на 1,9%</description>
  </item>
  <item>
    <title>Новость без даты</title>
    <link>https://smart-lab.ru/blog/1334603.php</link>
    <description>Сегежа объявила о переносе сроков публикации отчётности</description>
  </item>
</channel>
</rss>
"""


def test_parse_feed_matches_title():
    notes = sln.parse_feed(RSS_FIXTURE)
    by_issuer = {n.issuer_id: n for n in notes}
    assert "7703104630" in by_issuer  # АФК Система, matched via title
    assert "прогноз по выручке" in by_issuer["7703104630"].headline


def test_parse_feed_matches_description_when_title_has_no_match():
    notes = sln.parse_feed(RSS_FIXTURE)
    by_issuer = {n.issuer_id: n for n in notes}
    # "Итоги дня на бирже" title has no issuer match; the description
    # mentions Whoosh -- must still be matched and attached.
    assert "9717068640" in by_issuer
    assert by_issuer["9717068640"].headline == "Итоги дня на бирже"


def test_parse_feed_skips_items_with_no_match():
    notes = sln.parse_feed(RSS_FIXTURE)
    headlines = [n.headline for n in notes]
    assert "Индекс Мосбиржи растёт на открытии" not in headlines


def test_parse_feed_pubdate_parses_rfc822_to_iso():
    notes = sln.parse_feed(RSS_FIXTURE)
    by_issuer = {n.issuer_id: n for n in notes}
    assert by_issuer["7703104630"].occurred.startswith("2026-07-30")


def test_parse_feed_missing_pubdate_returns_none_not_fabricated():
    notes = sln.parse_feed(RSS_FIXTURE)
    by_issuer = {n.issuer_id: n for n in notes}
    assert "9703024202" in by_issuer  # Сегежа, matched via description, no pubDate
    assert by_issuer["9703024202"].occurred is None


def test_market_note_body_hash_dedupes_same_title_and_url():
    notes = sln.parse_feed(RSS_FIXTURE)
    notes_again = sln.parse_feed(RSS_FIXTURE)
    hashes = {n.body_hash for n in notes}
    hashes_again = {n.body_hash for n in notes_again}
    assert hashes == hashes_again  # stable across re-parses, same input


def test_market_note_flow_through_store_and_dedupe(tmp_path):
    con = db.connect(tmp_path / "mn.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    notes = sln.parse_feed(RSS_FIXTURE)
    n = notes[0]
    db.upsert_issuer(con, n.issuer_id, "АФК Система", None)
    inserted = db.insert_market_note(
        con, source=sln.SOURCE_MARKER, issuer_id=n.issuer_id, occurred=n.occurred,
        headline=n.headline, summary=n.summary, url=n.url, severity=None,
        category=None, confidence=None, fetched_on="2026-07-30", body_hash=n.body_hash)
    con.commit()
    assert inserted is True
    # re-inserting the same note (same body_hash) must be a no-op dedup
    inserted_again = db.insert_market_note(
        con, source=sln.SOURCE_MARKER, issuer_id=n.issuer_id, occurred=n.occurred,
        headline=n.headline, summary=n.summary, url=n.url, severity=None,
        category=None, confidence=None, fetched_on="2026-07-30", body_hash=n.body_hash)
    con.commit()
    assert inserted_again is False
    row = con.execute("SELECT * FROM market_note WHERE body_hash=?",
                      (n.body_hash,)).fetchone()
    assert row["severity"] is None and row["category"] is None
    con.close()


def test_news_aliases_resolve_to_real_watchlist_issuers(tmp_path):
    """FK-consistency guard, explicitly requested 2026-07-30 after the
    earlier session found 8 of 11 entries in smartlab.py's INTERFAX_ISSUERS
    pointed at INN values that didn't match any real issuer -- a name match
    would "succeed" but the resulting note would attach to a nonexistent
    issuer_id and never surface anywhere. Seeds a fresh DB's `issuer` table
    from the CURRENT watchlist.json (not the production DB, so this test
    catches drift the moment watchlist.json changes, not just today)."""
    wl = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    wl = {k: v for k, v in wl.items() if not k.startswith("_")}

    con = db.connect(tmp_path / "fk.sqlite")
    db.init_db(con)
    for issuer_id, meta in wl.items():
        db.upsert_issuer(con, issuer_id, meta.get("name"), meta.get("sector"))
    con.commit()

    real_ids = {r["issuer_id"] for r in con.execute("SELECT issuer_id FROM issuer")}
    bad = {name: inn for name, inn in sln.NEWS_ALIASES.items() if inn not in real_ids}
    assert not bad, f"NEWS_ALIASES entries with no matching issuer row: {bad}"
    con.close()
