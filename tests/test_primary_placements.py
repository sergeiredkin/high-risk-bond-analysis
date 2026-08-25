"""Tests for export_dashboard_data.primary_placements() (Первичные
размещения task, 2026-08-25).

Ships a thin date+headline+url export -- NOT the originally-specced
issuer/rating/YTM/amount columns -- because Step 1 audit for this task
found moex_listing rows have issuer_id=NULL (rss_generic.py stores this
feed market-wide) and `summary` truncated to ~200 chars by MOEX's own RSS
feed before ingestion, so those fields aren't derivable from stored data.
See the user's explicit decision in that task's follow-up message."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond.store import db  # noqa: E402
from export_dashboard_data import primary_placements  # noqa: E402

TODAY = "2026-08-25"


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    return con


def _insert(con, source, headline, occurred, note_id_hint, url="https://x/1"):
    db.insert_market_note(
        con, source=source, issuer_id=None, occurred=occurred, headline=headline,
        summary=None, url=url, severity=None, category=None, confidence=None,
        fetched_on=occurred, body_hash=f"h-{note_id_hint}")


def test_keeps_registration_result_and_trading_start_headlines(tmp_path):
    con = _con(tmp_path)
    _insert(con, "moex_listing", "О регистрации выпуска биржевых облигаций",
            "2026-08-24T18:51:33+03:00", "n1")
    _insert(con, "moex_listing", "Итоги выпуска биржевых облигаций",
            "2026-08-24T12:31:42+03:00", "n2")
    _insert(con, "moex_listing", 'О начале торгов ценными бумагами "25" августа 2026 года',
            "2026-08-24T18:46:37+03:00", "n3")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert len(rows) == 3
    assert {r["headline"] for r in rows} == {
        "О регистрации выпуска биржевых облигаций",
        "Итоги выпуска биржевых облигаций",
        'О начале торгов ценными бумагами "25" августа 2026 года',
    }


def test_drops_delisting_and_listing_change_headlines(tmp_path):
    con = _con(tmp_path)
    _insert(con, "moex_listing", "Об исключении ценных бумаг из Списка",
            "2026-08-24T18:23:21+03:00", "n1")
    _insert(con, "moex_listing", "О внесении изменений в Список ценных бумаг",
            "2026-08-24T18:29:55+03:00", "n2")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert rows == []


def test_only_moex_listing_source_included(tmp_path):
    con = _con(tmp_path)
    _insert(con, "google_news", "О регистрации выпуска биржевых облигаций",
            "2026-08-24T18:51:33+03:00", "n1")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert rows == []


def test_excludes_rows_older_than_30_days(tmp_path):
    con = _con(tmp_path)
    _insert(con, "moex_listing", "Итоги выпуска биржевых облигаций",
            "2026-07-01T10:00:00+03:00", "n1")  # 55 days before TODAY
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert rows == []


def test_includes_row_within_30_day_window(tmp_path):
    con = _con(tmp_path)
    _insert(con, "moex_listing", "Итоги выпуска биржевых облигаций",
            "2026-07-28T10:00:00+03:00", "n1")  # 28 days before TODAY
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert len(rows) == 1


def test_row_shape_is_date_headline_url_only(tmp_path):
    con = _con(tmp_path)
    _insert(con, "moex_listing", "Итоги выпуска биржевых облигаций",
            "2026-08-24T12:31:42+03:00", "n1", url="https://www.moex.com/n103616")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert rows == [{
        "date": "2026-08-24T12:31:42+03:00",
        "headline": "Итоги выпуска биржевых облигаций",
        "url": "https://www.moex.com/n103616",
    }]


def test_empty_market_note_table_returns_empty_list(tmp_path):
    con = _con(tmp_path)
    con.commit()
    assert primary_placements(con, TODAY) == []
    con.close()


def test_missing_market_note_table_returns_empty_list_not_raises(tmp_path):
    con = db.connect(tmp_path / "t2.sqlite")
    db.init_db(con)  # deliberately no init_market_note()
    assert primary_placements(con, TODAY) == []
    con.close()
