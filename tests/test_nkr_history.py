"""Tests for the Rating History Backfill task (2026-08-25) additions to
scrape/nkr.py (find_issuer_slug/fetch_issuer_ratings, appended alongside
the pre-existing bulk-listing scraper from 2026-07-31 -- see that module's
own test_nkr.py for the bulk-listing/normalize_name coverage this file
doesn't repeat), store/db.py's insert_historical_rating(), and
scrape_pipeline.run_nkr_backfill()."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import scrape_pipeline  # noqa: E402
from hybond.scrape import nkr  # noqa: E402
from hybond.store import db  # noqa: E402

# Real per-issuer page markup shape (ratings.ru/ratings/issuers/Plaza-Telecom/,
# captured 2026-08-25): 4 `.rate-item` blocks, newest first, covering all
# four action verbs this module recognizes except withdrawn.
ISSUER_PAGE_FIXTURE = """
<div class="rate-cols">
  <div class="rate-col">
    <p class="rate-col__title">Рейтинги эмитента</p>
    <div class="rate-items">
      <div class="rate-item">
        <div class="rate-item__wrapper">
          <p class="rate-item__rating"> BB.ru </p>
        </div>
        <a href="/ratings/press-releases/Plaza-Telecom-RA-210726/" class="rate-item__title">
          НКР подтвердило кредитный рейтинг ООО «Плаза-Телеком» на уровне BB.ru, прогноз — стабильный
        </a>
        <p class="rate-item__date"> 21.07.2026 </p>
      </div>
      <div class="rate-item">
        <div class="rate-item__wrapper">
          <p class="rate-item__rating"> BB.ru </p>
        </div>
        <a href="/ratings/press-releases/Plaza-Telecom-RA-290724/" class="rate-item__title">
          НКР повысило кредитный рейтинг ООО «Плаза-Телеком» с BB-.ru до BB.ru со стабильным прогнозом
        </a>
        <p class="rate-item__date"> 29.07.2024 </p>
      </div>
      <div class="rate-item">
        <div class="rate-item__wrapper">
          <p class="rate-item__rating"> BB-.ru </p>
        </div>
        <a href="/ratings/press-releases/Plaza-Telecom-RA-020823/" class="rate-item__title">
          НКР присвоило ООО «Плаза-Телеком» кредитный рейтинг BB-.ru со стабильным прогнозом
        </a>
        <p class="rate-item__date"> 02.08.2023 </p>
      </div>
      <div class="rate-item">
        <div class="rate-item__wrapper">
          <p class="rate-item__rating"> B.ru </p>
        </div>
        <a href="/ratings/press-releases/Plaza-Telecom-RA-999999/" class="rate-item__title">
          НКР понизило кредитный рейтинг ООО «Плаза-Телеком» с BB-.ru до B.ru с негативным прогнозом
        </a>
        <p class="rate-item__date"> 01.01.2023 </p>
      </div>
    </div>
  </div>
</div>
"""

# Bulk-listing rows for name resolution -- reuses NkrRating directly rather
# than a fresh HTML fixture, since parse_issuers() already has its own
# dedicated coverage in test_nkr.py.
BULK_ROWS = [
    nkr.NkrRating(slug="Plaza-Telecom", name="ООО «Плаза-Телеком»",
                  name_core="ПЛАЗА-ТЕЛЕКОМ", rating="ruBB", outlook="стабильный",
                  sector="Торговля", rated_on="2026-07-21",
                  press_release_url="https://ratings.ru/x", is_withdrawn=False),
    nkr.NkrRating(slug="Delimobil", name="ПАО «Каршеринг Руссия»",
                  name_core="КАРШЕРИНГ РУССИЯ", rating="ruBBB+", outlook="стабильный",
                  sector="Транспорт", rated_on="2026-06-03",
                  press_release_url="https://ratings.ru/y", is_withdrawn=False),
]


class _FakeBackend:
    def __init__(self, page: str):
        self.page = page
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return self.page


# ---- find_issuer_slug (bulk-listing name resolution) ------------------------

def test_find_issuer_slug_exact_match():
    assert nkr.find_issuer_slug("ПЛАЗА-Телеком", rows=BULK_ROWS) == "Plaza-Telecom"


def test_find_issuer_slug_strips_trailing_parenthetical_alias():
    # issuer.name carries a brand alias ratings.ru's own name never has.
    assert nkr.find_issuer_slug("Каршеринг Руссия (Делимобиль)", rows=BULK_ROWS) == "Delimobil"


def test_find_issuer_slug_returns_none_when_not_in_bulk_listing():
    assert nkr.find_issuer_slug("Совершенно Другая Компания", rows=BULK_ROWS) is None


# ---- fetch_issuer_ratings (.rate-item HTML parsing) -------------------------

def test_fetch_issuer_ratings_extracts_all_items_newest_first():
    backend = _FakeBackend(ISSUER_PAGE_FIXTURE)
    rows = nkr.fetch_issuer_ratings("Plaza-Telecom", backend=backend)
    assert [r["rated_on"] for r in rows] == [
        "2026-07-21", "2024-07-29", "2023-08-02", "2023-01-01"]


def test_fetch_issuer_ratings_converts_rating_to_ru_convention():
    rows = nkr.fetch_issuer_ratings("Plaza-Telecom", backend=_FakeBackend(ISSUER_PAGE_FIXTURE))
    assert rows[0]["rating"] == "ruBB"
    assert rows[2]["rating"] == "ruBB-"


def test_fetch_issuer_ratings_extracts_action_verbs():
    rows = nkr.fetch_issuer_ratings("Plaza-Telecom", backend=_FakeBackend(ISSUER_PAGE_FIXTURE))
    actions = {r["rated_on"]: r["action"] for r in rows}
    assert actions["2026-07-21"] == "confirmed"
    assert actions["2024-07-29"] == "upgraded"
    assert actions["2023-08-02"] == "assigned"
    assert actions["2023-01-01"] == "downgraded"


def test_fetch_issuer_ratings_normalizes_outlook_to_nominative_case():
    rows = nkr.fetch_issuer_ratings("Plaza-Telecom", backend=_FakeBackend(ISSUER_PAGE_FIXTURE))
    outlooks = {r["rated_on"]: r["outlook"] for r in rows}
    assert outlooks["2026-07-21"] == "стабильный"   # "прогноз — стабильный" (dash form)
    assert outlooks["2024-07-29"] == "стабильный"   # "со стабильным прогнозом" (instrumental)
    assert outlooks["2023-01-01"] == "негативный"   # "с негативным прогнозом"


def test_fetch_issuer_ratings_builds_full_press_release_url():
    rows = nkr.fetch_issuer_ratings("Plaza-Telecom", backend=_FakeBackend(ISSUER_PAGE_FIXTURE))
    assert rows[0]["url"] == "https://ratings.ru/ratings/press-releases/Plaza-Telecom-RA-210726/"


def test_fetch_issuer_ratings_empty_page_returns_empty_list():
    rows = nkr.fetch_issuer_ratings("Nobody", backend=_FakeBackend("<html></html>"))
    assert rows == []


# ---- db.insert_historical_rating --------------------------------------------

ISSUER = "1234567890"


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rating(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer")
    return con


def test_insert_historical_rating_never_sets_is_current():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        con = _con(Path(d))
        inserted = db.insert_historical_rating(
            con, issuer_id=ISSUER, agency="НКР", rating="ruBB", outlook="стабильный",
            action="assigned", rated_on="2023-08-02", source="nkr_historical")
        con.commit()
        assert inserted is True
        row = con.execute(
            "SELECT is_current, needs_verification, verified_on FROM rating "
            "WHERE issuer_id=? AND agency='НКР' AND rated_on='2023-08-02'", (ISSUER,)
        ).fetchone()
        con.close()
        assert row["is_current"] == 0
        assert row["needs_verification"] == 0
        assert row["verified_on"] is not None


def test_insert_historical_rating_does_not_flip_existing_current_row():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        con = _con(Path(d))
        db.upsert_rating(con, ISSUER, "НКР", "ruBB", "стабильный", "confirmed",
                          "2026-07-21", source="manual")
        con.commit()
        db.insert_historical_rating(
            con, issuer_id=ISSUER, agency="НКР", rating="ruBB-", outlook="стабильный",
            action="assigned", rated_on="2023-08-02", source="nkr_historical")
        con.commit()
        current = con.execute(
            "SELECT rated_on FROM rating WHERE issuer_id=? AND agency='НКР' AND is_current=1",
            (ISSUER,)).fetchone()
        con.close()
        assert current["rated_on"] == "2026-07-21"  # untouched


def test_insert_historical_rating_idempotent_on_same_pk():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        con = _con(Path(d))
        first = db.insert_historical_rating(
            con, issuer_id=ISSUER, agency="НКР", rating="ruBB", outlook="стабильный",
            action="assigned", rated_on="2023-08-02", source="nkr_historical")
        con.commit()
        second = db.insert_historical_rating(
            con, issuer_id=ISSUER, agency="НКР", rating="ruBB", outlook="стабильный",
            action="assigned", rated_on="2023-08-02", source="nkr_historical")
        con.commit()
        count = con.execute(
            "SELECT COUNT(*) c FROM rating WHERE issuer_id=? AND agency='НКР'",
            (ISSUER,)).fetchone()["c"]
        con.close()
        assert first is True
        assert second is False
        assert count == 1


# ---- scrape_pipeline.run_nkr_backfill ---------------------------------------

def test_run_nkr_backfill_scopes_to_tier1_issuers_with_active_nkr_rating(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.add_issuer_at_tier(con, "1111111111", "ПЛАЗА-Телеком", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2222222222", "No Rating Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "3333333333", "ПЛАЗА-Телеком Tier2", watchlist_tier=2)
    db.upsert_rating(con, "1111111111", "НКР", "ruBB", "стабильный", "confirmed",
                      "2026-07-21", source="manual")
    db.upsert_rating(con, "3333333333", "НКР", "ruBB", "стабильный", "confirmed",
                      "2026-07-21", source="manual")
    con.commit()
    con.close()

    monkeypatch.setattr(nkr, "fetch_issuers_page", lambda backend=None, snapshot=True: "")
    monkeypatch.setattr(nkr, "parse_issuers", lambda html: BULK_ROWS)
    monkeypatch.setattr(nkr, "fetch_issuer_ratings", lambda slug, backend=None: [
        {"rating": "ruBB", "outlook": "стабильный", "action": "upgraded",
         "rated_on": "2024-07-29", "url": "https://x/1"},
    ])

    result = scrape_pipeline.run_nkr_backfill()
    assert result["scanned"] == 1  # only the tier=1 issuer with an active НКР rating
    assert result["new_by_issuer"] == {"ПЛАЗА-Телеком": 1}

    con = db.connect()
    tier2_rows = con.execute(
        "SELECT COUNT(*) c FROM rating WHERE issuer_id='3333333333' AND source='nkr_historical'"
    ).fetchone()["c"]
    con.close()
    assert tier2_rows == 0  # tier=2 issuer never touched


def test_run_nkr_backfill_inn_restricts_to_one_issuer(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.add_issuer_at_tier(con, "1111111111", "ПЛАЗА-Телеком", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2222222222", "Каршеринг Руссия (Делимобиль)", watchlist_tier=1)
    db.upsert_rating(con, "1111111111", "НКР", "ruBB", "стабильный", "confirmed",
                      "2026-07-21", source="manual")
    db.upsert_rating(con, "2222222222", "НКР", "ruBBB+", "стабильный", "assigned",
                      "2026-06-03", source="manual")
    con.commit()
    con.close()

    monkeypatch.setattr(nkr, "fetch_issuers_page", lambda backend=None, snapshot=True: "")
    monkeypatch.setattr(nkr, "parse_issuers", lambda html: BULK_ROWS)
    monkeypatch.setattr(nkr, "fetch_issuer_ratings", lambda slug, backend=None: [])

    result = scrape_pipeline.run_nkr_backfill(inn="1111111111")
    assert result["scanned"] == 1


def test_run_nkr_backfill_flags_scraped_action_newer_than_current(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.add_issuer_at_tier(con, "1111111111", "ПЛАЗА-Телеком", watchlist_tier=1)
    db.upsert_rating(con, "1111111111", "НКР", "ruBB", "стабильный", "confirmed",
                      "2025-07-25", source="manual")  # stale on purpose
    con.commit()
    con.close()

    monkeypatch.setattr(nkr, "fetch_issuers_page", lambda backend=None, snapshot=True: "")
    monkeypatch.setattr(nkr, "parse_issuers", lambda html: BULK_ROWS)
    monkeypatch.setattr(nkr, "fetch_issuer_ratings", lambda slug, backend=None: [
        {"rating": "ruBB", "outlook": "стабильный", "action": "confirmed",
         "rated_on": "2026-07-21", "url": "https://x/1"},  # newer than our "current"
    ])

    result = scrape_pipeline.run_nkr_backfill()
    assert len(result["newer_than_current"]) == 1
    assert "2026-07-21" in result["newer_than_current"][0]
    # still inserted as history (is_current untouched), not silently dropped
    con = db.connect()
    row = con.execute(
        "SELECT is_current FROM rating WHERE issuer_id='1111111111' AND rated_on='2026-07-21'"
    ).fetchone()
    con.close()
    assert row["is_current"] == 0


def test_run_nkr_backfill_issuer_not_in_bulk_listing_is_reported_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.add_issuer_at_tier(con, "9999999999", "Совершенно Другая Компания", watchlist_tier=1)
    db.upsert_rating(con, "9999999999", "НКР", "ruBB", "стабильный", "confirmed",
                      "2026-07-21", source="manual")
    con.commit()
    con.close()

    monkeypatch.setattr(nkr, "fetch_issuers_page", lambda backend=None, snapshot=True: "")
    monkeypatch.setattr(nkr, "parse_issuers", lambda html: BULK_ROWS)

    result = scrape_pipeline.run_nkr_backfill()
    assert result["not_found"] == ["Совершенно Другая Компания (9999999999)"]
    assert result["total_new"] == 0


def test_run_nkr_backfill_one_issuer_error_does_not_block_others(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.add_issuer_at_tier(con, "1111111111", "ПЛАЗА-Телеком", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2222222222", "Каршеринг Руссия (Делимобиль)", watchlist_tier=1)
    db.upsert_rating(con, "1111111111", "НКР", "ruBB", "стабильный", "confirmed",
                      "2026-07-21", source="manual")
    db.upsert_rating(con, "2222222222", "НКР", "ruBBB+", "стабильный", "assigned",
                      "2026-06-03", source="manual")
    con.commit()
    con.close()

    monkeypatch.setattr(nkr, "fetch_issuers_page", lambda backend=None, snapshot=True: "")
    monkeypatch.setattr(nkr, "parse_issuers", lambda html: BULK_ROWS)

    def fake_fetch(slug, backend=None):
        if slug == "Plaza-Telecom":
            raise RuntimeError("network unreachable")
        return [{"rating": "ruBBB", "outlook": "стабильный", "action": "assigned",
                  "rated_on": "2025-01-01", "url": "https://x/1"}]
    monkeypatch.setattr(nkr, "fetch_issuer_ratings", fake_fetch)

    result = scrape_pipeline.run_nkr_backfill()
    assert len(result["errors"]) == 1
    assert "Plaza-Telecom" not in result["errors"][0]  # error message uses issuer name, not slug
    assert "ПЛАЗА-Телеком" in result["errors"][0]
    assert result["total_new"] == 1  # Delimobil's row still landed
