"""Tests for export_dashboard_data.primary_placements() (Первичные
размещения task, 2026-08-25; switched from source='moex_listing' to
source='google_news' in a same-day follow-up task).

The switch was driven by a data audit: source='smartlab_news' (the task's
original assumption) had only 2 matching rows in the real DB, both routine
coupon-payment news, not placements. source='moex_listing' structurally
couldn't carry issuer/YTM (issuer_id NULL, summary truncated ~200 chars --
see this file's git history for that first version's tests). source=
'google_news' (republished Smart-Lab content via Google News RSS) actually
has usable placement text -- confirmed live: 21 genuine placement-signal
headlines in the real DB, ~33% with a confident NEWS_ALIASES issuer match,
~19% with a YTM regex match. Эмитент/Доходность are best-effort, not
comprehensive -- "—"/None means not confidently extracted, never guessed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond.store import db  # noqa: E402
from export_dashboard_data import (  # noqa: E402
    _extract_placement_issuer, _extract_ytm, _is_placement_headline, primary_placements)

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


# ---- _is_placement_headline (pure filter logic) -----------------------------

def test_keeps_genuine_placement_headlines():
    assert _is_placement_headline('«ИЭК ХОЛДИНГ» размещает 2-летний выпуск облигаций')
    assert _is_placement_headline('26 августа - размещение облигаций МФК МК (bb-, YTM 25,58%)')
    assert _is_placement_headline('Сегодня у нас на обзоре новый выпуск МФК «Быстроденьги»')
    assert _is_placement_headline('Календарь первички ВДО и розничных облигаций')


def test_drops_coupon_payment_headlines():
    assert not _is_placement_headline(
        'Компания «Пионер-Лизинг» выплатила 82-й купон по 3-му выпуску облигаций')
    assert not _is_placement_headline('Облигации «Мани Капитал» с купоном 23.00% (001Р-04)')


def test_drops_denial_headlines():
    assert not _is_placement_headline(
        'ЕвроТранс опроверг информацию о дефолтах по двум выпускам облигаций')


def test_drops_unrelated_headlines():
    assert not _is_placement_headline('АКРА подтвердило рейтинг компании на уровне ruBBB')


# ---- _extract_placement_issuer -----------------------------------------------

def test_extracts_issuer_via_news_aliases():
    name, issuer_id = _extract_placement_issuer(
        'Новое размещение облигаций Л-Старт (B.ru, 100 млн руб., YTM 32,53%)')
    assert name == 'Л-Старт'
    assert issuer_id == '7703550709'


def test_extracts_issuer_via_quoted_fallback_when_no_alias_match():
    name, issuer_id = _extract_placement_issuer(
        'стоит ли лезть в новый выпуск ООО «ПИР»?')
    assert name == 'ПИР'
    assert issuer_id is None  # "ПИР" deliberately excluded from NEWS_ALIASES (common word)


def test_returns_none_none_when_neither_matches():
    name, issuer_id = _extract_placement_issuer(
        '26 августа - размещение облигаций МФК МК (bb-, YTM 25,58%)')
    assert name is None
    assert issuer_id is None


# ---- _extract_ytm -------------------------------------------------------------

def test_extracts_ytm_with_comma_decimal():
    assert _extract_ytm('размещение облигаций МФК МК (bb-, YTM 25,58%)') == '25.58%'


def test_extracts_ytm_with_dot_decimal():
    assert _extract_ytm('доходность к погашению YTM 29.34%') == '29.34%'


def test_returns_none_when_no_ytm_present():
    assert _extract_ytm('«ИЭК ХОЛДИНГ» размещает выпуск, купон до 17,5%') is None


# ---- primary_placements (end-to-end) -----------------------------------------

def test_only_google_news_source_included(tmp_path):
    con = _con(tmp_path)
    _insert(con, "moex_listing", "О регистрации выпуска биржевых облигаций",
            "2026-08-24T18:51:33+03:00", "n1")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert rows == []


def test_keeps_placement_drops_coupon_payment(tmp_path):
    con = _con(tmp_path)
    _insert(con, "google_news", "«ИЭК ХОЛДИНГ» размещает 2-летний выпуск облигаций на 2 млрд ₽",
            "2026-08-24T09:14:52+00:00", "n1")
    _insert(con, "google_news",
            "Компания «Пионер-Лизинг» выплатила 82-й купон по 3-му выпуску облигаций",
            "2026-08-20T23:34:57+00:00", "n2")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert len(rows) == 1
    assert rows[0]["issuer"] == "ИЭК ХОЛДИНГ"


def test_row_shape_includes_issuer_issuer_id_and_ytm(tmp_path):
    con = _con(tmp_path)
    _insert(con, "google_news",
            'Новое размещение облигаций Л-Старт (B.ru, 100 млн руб., YTM 32,53%) 22 мая',
            "2026-08-20T07:00:00+00:00", "n1", url="https://news.google.com/x")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert len(rows) == 1
    r = rows[0]
    assert r["issuer"] == "Л-Старт"
    assert r["issuer_id"] == "7703550709"
    assert r["ytm"] == "32.53%"
    assert r["url"] == "https://news.google.com/x"


def test_issuer_and_ytm_null_when_not_extractable(tmp_path):
    con = _con(tmp_path)
    _insert(con, "google_news", "Первичка на следующую неделю",
            "2026-08-23T09:04:46+00:00", "n1")
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert len(rows) == 1
    assert rows[0]["issuer"] is None
    assert rows[0]["ytm"] is None


def test_excludes_rows_older_than_30_days(tmp_path):
    con = _con(tmp_path)
    _insert(con, "google_news", "Стартовало размещение облигаций серии 001",
            "2026-07-01T10:00:00+00:00", "n1")  # 55 days before TODAY
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert rows == []


def test_includes_row_within_30_day_window(tmp_path):
    con = _con(tmp_path)
    _insert(con, "google_news", "Стартовало размещение облигаций серии 001",
            "2026-07-28T10:00:00+00:00", "n1")  # 28 days before TODAY
    con.commit()
    rows = primary_placements(con, TODAY)
    con.close()
    assert len(rows) == 1


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
