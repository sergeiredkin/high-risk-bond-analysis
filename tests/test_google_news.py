"""Tests for scrape/google_news.py + scrape_pipeline.run_google_news()
(Google News RSS Monitoring task, 2026-08-24)."""
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.scrape import google_news as gn  # noqa: E402
from hybond import scrape_pipeline  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "7701234599"

# Shaped like a real Google News RSS search result: standard RSS 2.0,
# <item> with title/link/pubDate.
RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>"Тест Компани" облигации - Google News</title>
  <item>
    <title>Тест Компани допустила техдефолт по купону - РБК</title>
    <link>https://news.google.com/rss/articles/CBMi1</link>
    <pubDate>Mon, 24 Aug 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>АКРА понизило рейтинг Тест Компани - Коммерсантъ</title>
    <link>https://news.google.com/rss/articles/CBMi2</link>
    <pubDate>Sun, 23 Aug 2026 08:30:00 GMT</pubDate>
  </item>
  <item>
    <title>Новость без даты</title>
    <link>https://news.google.com/rss/articles/CBMi3</link>
  </item>
</channel>
</rss>
"""


# ---- URL template ------------------------------------------------------------

def test_build_url_uses_correct_base_and_params():
    url = gn.build_url("Тест Компани")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == gn.BASE_URL
    assert qs["hl"] == ["ru"]
    assert qs["gl"] == ["RU"]
    assert qs["ceid"] == ["RU:ru"]
    assert qs["q"] == ["Тест Компани облигации OR дефолт OR рейтинг OR оферта OR суд"]


def test_build_url_percent_encodes_special_characters():
    url = gn.build_url('ООО "Компания & Ко"')
    assert " " not in url.split("q=")[1].split("&hl")[0] or "%20" in url or "+" in url
    # round-trips cleanly through parse_qs regardless of quoting style
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["q"][0].startswith('ООО "Компания & Ко"')


# ---- feed parsing / date handling ---------------------------------------------

def test_parse_feed_extracts_all_fields():
    items = gn.parse_feed(RSS_FIXTURE)
    first = items[0]
    assert first["title"] == "Тест Компани допустила техдефолт по купону - РБК"
    assert first["url"] == "https://news.google.com/rss/articles/CBMi1"
    assert first["source"] == "google_news"


def test_parse_feed_published_date_parses_google_rfc822_to_iso():
    items = gn.parse_feed(RSS_FIXTURE)
    assert items[0]["published"] == "2026-08-24T10:00:00+00:00"
    assert items[1]["published"] == "2026-08-23T08:30:00+00:00"


def test_parse_feed_missing_pubdate_returns_none_not_fabricated():
    items = gn.parse_feed(RSS_FIXTURE)
    no_date = [i for i in items if i["title"] == "Новость без даты"][0]
    assert no_date["published"] is None


def test_parse_feed_skips_items_with_no_title_or_link():
    xml = """<rss><channel>
      <item><title>Есть только заголовок</title></item>
      <item><link>https://example.com/x</link></item>
    </channel></rss>"""
    assert gn.parse_feed(xml) == []


def test_parse_feed_malformed_xml_raises():
    with pytest.raises(Exception):
        gn.parse_feed("<rss><channel><item><title>unterminated")


# ---- retry on timeout ----------------------------------------------------------

class _FakeBackend:
    """Injectable Backend: `responses` is a list consumed one per .get()
    call -- an Exception instance is raised, anything else is returned as
    the response body."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_fetch_issuer_news_retries_on_timeout_then_succeeds():
    import requests
    backend = _FakeBackend([
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.Timeout("timed out again"),
        RSS_FIXTURE,
    ])
    items = gn.fetch_issuer_news("Тест Компани", ISSUER, backend=backend, max_retries=3)
    assert backend.calls == 3
    # 3 items parse (title+link), but only 2 mention "Тест Компани" in the
    # title -- the issuer-relevance filter (Google News Issuer-Name Filter
    # task, 2026-08-24) drops "Новость без даты", which parses fine but
    # never mentions the issuer.
    assert len(items) == 2


def test_fetch_issuer_news_gives_up_after_max_retries():
    import requests
    backend = _FakeBackend([
        requests.exceptions.Timeout("t1"),
        requests.exceptions.Timeout("t2"),
        requests.exceptions.Timeout("t3"),
    ])
    with pytest.raises(RuntimeError, match="after 3 attempt"):
        gn.fetch_issuer_news("Тест Компани", ISSUER, backend=backend, max_retries=3)
    assert backend.calls == 3


def test_fetch_issuer_news_does_not_retry_non_timeout_error():
    backend = _FakeBackend([RuntimeError("HTTP 404")])
    with pytest.raises(RuntimeError, match="404"):
        gn.fetch_issuer_news("Тест Компани", ISSUER, backend=backend, max_retries=3)
    assert backend.calls == 1  # no retry for a non-timeout error


# ---- issuer-name relevance filter (Google News Issuer-Name Filter task, 2026-08-24) --

# Shaped like the real complaint: a market-wide roundup that matches the
# query's OR-clause (mentions "дефолт") without mentioning this issuer at
# all, alongside one genuinely relevant item.
OFF_TOPIC_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <item>
    <title>Волна дефолтов 2026: что происходит на рынке ВДО</title>
    <link>https://news.google.com/rss/articles/CBMi9</link>
    <pubDate>Mon, 24 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>РОЛЬФ допустил техдефолт по купону - РБК</title>
    <link>https://news.google.com/rss/articles/CBMi10</link>
    <pubDate>Mon, 24 Aug 2026 10:00:00 GMT</pubDate>
  </item>
</channel>
</rss>
"""


def test_default_alias_strips_legal_form_and_quotes():
    assert gn._default_alias('АО "РОЛЬФ"') == "РОЛЬФ"
    assert gn._default_alias('ООО «Тест Компани»') == "Тест Компани"
    assert gn._default_alias("Тест Компани") == "Тест Компани"


def test_fetch_issuer_news_drops_items_not_mentioning_issuer():
    backend = _FakeBackend([OFF_TOPIC_FIXTURE])
    items = gn.fetch_issuer_news('АО "РОЛЬФ"', "5047254063", backend=backend)
    assert len(items) == 1
    assert items[0]["url"] == "https://news.google.com/rss/articles/CBMi10"


def test_fetch_issuer_news_explicit_aliases_override_default():
    xml = """<rss><channel>
      <item><title>SMLT отчиталась за 2 квартал</title>
             <link>https://x/1</link></item>
      <item><title>Не имеет отношения к делу</title>
             <link>https://x/2</link></item>
    </channel></rss>"""
    backend = _FakeBackend([xml])
    # "Самолёт" alone would match neither title -- explicit aliases (e.g.
    # smartlab_news.NEWS_ALIASES' brand-ticker entries) let a caller widen
    # the match beyond the derived default.
    items = gn.fetch_issuer_news("Самолёт", "9731004688", backend=backend,
                                  issuer_aliases=["SMLT", "Самолёт"])
    assert len(items) == 1
    assert items[0]["url"] == "https://x/1"


def test_fetch_issuer_news_title_match_is_case_insensitive():
    xml = """<rss><channel>
      <item><title>рольф допустил техдефолт</title><link>https://x/1</link></item>
    </channel></rss>"""
    backend = _FakeBackend([xml])
    items = gn.fetch_issuer_news('АО "РОЛЬФ"', "5047254063", backend=backend)
    assert len(items) == 1


# ---- URL dedup (db.seen_market_note_urls + scrape_pipeline.run_google_news) --

def test_seen_market_note_urls_reflects_stored_rows(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, ISSUER, "Тест Компани")
    db.insert_market_note(
        con, source="google_news", issuer_id=ISSUER, occurred="2026-08-24T10:00:00+00:00",
        headline="X", summary=None, url="https://news.google.com/rss/articles/CBMi1",
        severity=None, category=None, confidence=None, fetched_on="2026-08-24",
        body_hash="abc123")
    con.commit()
    urls = db.seen_market_note_urls(con)
    con.close()
    assert urls == {"https://news.google.com/rss/articles/CBMi1"}


def test_run_google_news_skips_already_stored_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, ISSUER, "Тест Компани", watchlist_tier=1)
    # Pre-store one of the two URLs the fixture will return.
    db.insert_market_note(
        con, source="google_news", issuer_id=ISSUER, occurred="2026-08-24T10:00:00+00:00",
        headline="Тест Компани допустила техдефолт по купону - РБК", summary=None,
        url="https://news.google.com/rss/articles/CBMi1", severity=None, category=None,
        confidence=None, fetched_on="2026-08-23", body_hash="preexisting")
    con.commit()
    con.close()

    monkeypatch.setattr(gn, "fetch_issuer_news",
                        lambda name, inn, **kw: gn.parse_feed(RSS_FIXTURE))
    monkeypatch.setattr(gn, "SLEEP_BETWEEN_ISSUERS_S", 0)

    result = scrape_pipeline.run_google_news(snap_date=__import__("datetime").date(2026, 8, 24))

    assert result["scanned"] == 1
    assert result["parsed"] == 3  # all 3 items in the fixture have title+link
    assert result["new"] == 2       # 2 of the 3 URLs are not pre-stored
    assert result["skipped_dup"] == 1

    con = db.connect()
    rows = con.execute(
        "SELECT url, severity, category FROM market_note WHERE issuer_id=? ORDER BY url",
        (ISSUER,)).fetchall()
    con.close()
    assert len(rows) == 3  # 1 pre-existing + 2 new, no duplicate row for the pre-stored URL
    assert all(r["severity"] is None and r["category"] is None for r in rows)


def test_run_google_news_one_issuer_error_does_not_block_others(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, "1111111111", "Broken Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2222222222", "Working Co", watchlist_tier=1)
    con.commit()
    con.close()

    def fake_fetch(name, inn, **kw):
        if inn == "1111111111":
            raise RuntimeError("network unreachable")
        return gn.parse_feed(RSS_FIXTURE)

    monkeypatch.setattr(gn, "fetch_issuer_news", fake_fetch)
    monkeypatch.setattr(gn, "SLEEP_BETWEEN_ISSUERS_S", 0)

    result = scrape_pipeline.run_google_news()
    assert result["scanned"] == 2
    assert len(result["errors"]) == 1
    assert "1111111111" in result["errors"][0]
    assert result["new"] == 3  # Working Co's 3 items still got stored


def test_run_google_news_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, "1111111111", "A Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2222222222", "B Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "3333333333", "C Co", watchlist_tier=1)
    con.commit()
    con.close()

    monkeypatch.setattr(gn, "fetch_issuer_news", lambda name, inn, **kw: [])
    monkeypatch.setattr(gn, "SLEEP_BETWEEN_ISSUERS_S", 0)

    result = scrape_pipeline.run_google_news(limit=2)
    assert result["scanned"] == 2
