"""hybond/expectations.py -- the CBR's and the surveyed analysts' key-rate
expectations, with history (Ставка и ОФЗ plan rev 2, 2026-09-17).

Fixtures are trimmed from the real cbr.ru pages the parsers read (fetched
2026-09-17), keeping the markup quirks that matter: HTML entities around
scenario names, &nbsp; inside dates, an upper-case "Key" in one 2024 press
release URL, "Проект от" on a draft ОНЕГДКП edition. No network.
"""
import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import cbr_tracker, expectations  # noqa: E402
from hybond.store import db  # noqa: E402


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rates(con)
    db.init_cbr(con)
    db.init_pipeline_health(con)
    return con


# ---- ОНЕГДКП -------------------------------------------------------------------

def _scenario_table(first_year, rates):
    years = "".join(f'<th style="text-align: center;">{first_year + i}</th>' for i in range(len(rates)))
    cells = "".join(f'<td style="text-align: center;">{v}</td>' for v in rates)
    return (f'<table class="data"><tbody><tr><th></th>{years}</tr>'
            f'<tr><td>Годовая инфляция,&nbsp; %</td>{cells}</tr>'
            f'<tr><td>Ключевая ставка, в среднем за год, %&nbsp;годовых</td>{cells}</tr>'
            f'</tbody></table>')


def _ondkp_page(date_line, first_year=2025, tables=None):
    tables = tables or {
        "base": ["19,2", "14,5-14,6", "10,5&ndash;12,5", "8,0&ndash;9,0", "7,5&ndash;8,5"],
        "Дезинфляционный": ["19,2", "14,5-14,6", "9,0 &ndash; 11,0", "7,0&ndash;8,0", "7,5&ndash;8,5"],
        "Проинфляционный": ["19,2", "14,5-14,6", "13,0&ndash;15,0", "11,0&ndash;12,0", "8,5&ndash;9,5"],
        "Рисковый": ["19,2", "14,5 &ndash; 14,6", "19,0 &ndash; 21,0", "14,0 &ndash;16,0", "9,5 &ndash; 10,5"],
    }
    parts = [f'<div class="col-md-6 col-12 news-info-line_date">{date_line}</div>']
    for name, rates in tables.items():
        if name == "base":
            parts.append('<div class="block-title"><h3><span class="referenceable">Базовый сценарий</span></h3></div>')
        else:
            parts.append(f'<a class="dropdown_title referenceable" role="button">Сценарий &#171;{name}&#187;</a>')
        parts.append("<p>Предпосылки сценария…</p>")
        parts.append(_scenario_table(first_year, rates))
    return "\n".join(parts)


def test_parse_ondkp_edition_draft_with_four_scenarios():
    parsed = expectations.parse_ondkp_edition(_ondkp_page("Проект от 31 августа 2026 года"))
    assert parsed["published"] == "2026-08-31"
    assert parsed["status"] == "draft"
    assert set(parsed["scenarios"]) == {"base", "disinflation", "proinflation", "risk"}
    assert parsed["scenarios"]["risk"] == {2026: (14.5, 14.6), 2027: (19.0, 21.0),
                                            2028: (14.0, 16.0), 2029: (9.5, 10.5)}
    assert 2025 not in parsed["scenarios"]["base"]      # the факт column is dropped


def test_parse_ondkp_edition_final_edition_date():
    parsed = expectations.parse_ondkp_edition(_ondkp_page("27 октября 2025 года", first_year=2024))
    assert (parsed["published"], parsed["status"]) == ("2025-10-27", "final")


def test_parse_ondkp_index_newest_first():
    page = ('<a href="/about_br/publ/ondkp/on_2026_2028/">…</a><a href="/about_br/publ/ondkp/on_2027_2029/">…</a>'
            '<a href="/about_br/publ/ondkp/on_2026_2028/">dup</a>')
    assert expectations.parse_ondkp_index(page) == ["on_2027_2029", "on_2026_2028"]


# ---- survey --------------------------------------------------------------------

SURVEY_PAGE = """
<div class="col-md-6 col-12 news-info-line_date">2 сентября 2026 года</div>
<h2><span class="referenceable" id="a_185229">Результаты опроса: сентябрь 2026 года<sup>1</sup></span></h2>
<a href="/Content/Document/File/144490/full.xlsx">Все результаты</a>
<a href="/Content/Document/File/120859/Questionnaire.xlsx">Анкета</a>
"""


def test_parse_survey_page():
    assert expectations.parse_survey_page(SURVEY_PAGE) == {
        "published": "2026-09-02", "period": "2026-09",
        "xlsx_url": "https://www.cbr.ru/Content/Document/File/144490/full.xlsx"}


def _survey_workbook() -> bytes:
    """The shape of cbr.ru full.xlsx: a navigation sheet pointing at numbered
    sheets, the key-rate sheet with survey months across and stat blocks down."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    nav = wb.active
    nav.title = "Навигация (Navigation) "
    nav.append([None, "Навигация\nNavigation", None, None])
    nav.append([None, "ИПЦ\nCPI", "(в % дек. к дек.)", 1])
    nav.append([None, "Ключевая ставка\nKey rate", "(в % годовых, в среднем за год)", 3])
    nav.append([None, "Нейтральная ключевая ставка \nNeutral key rate", "(% годовых)", 16])
    wb.create_sheet("1")
    ws = wb.create_sheet("3")
    ws.append([None, "Агрегированные результаты опроса"])
    ws.append([])
    ws.append([None, "Ключевая ставка\n(в % годовых, в среднем за год, с учетом выходных дней)"])
    ws.append([None, None, None, "Прогнозный период", datetime(2026, 7, 1), datetime(2026, 9, 1)])
    ws.append([None, None, None, "Forecast \nperiod", "July 2026", "September 2026"])
    ws.append([None, "Медиана ", "Median", datetime(2026, 12, 31), 14.52, 14.5])
    ws.append([None, None, None, datetime(2027, 12, 31), 12.2, 12.43])
    ws.append([None, None, None, datetime(2029, 12, 31), "-", 8.5625])
    ws.append([None, "1-й квартиль", "1st quartile", datetime(2027, 12, 31), 11.45, 11.9])
    ws.append([None, "Всего ответов", "Total answers", datetime(2027, 12, 31), 31, 33])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_survey_workbook_reads_every_survey_and_skips_dashes():
    values = expectations.parse_survey_workbook(_survey_workbook())
    got = {(v["period"], v["year"], v["stat"]): v["value"] for v in values}
    assert got[("2026-09", 2027, "median")] == 12.43
    assert got[("2026-07", 2027, "median")] == 12.2
    assert got[("2026-09", 2027, "q1")] == 11.9
    assert got[("2026-09", 2027, "n")] == 33
    assert ("2026-07", 2029, "median") not in got          # '-' = no answer
    assert got[("2026-09", 2029, "median")] == 8.5625


def test_parse_survey_workbook_refuses_a_workbook_without_the_key_rate_sheet():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError):
        expectations.parse_survey_workbook(buf.getvalue())


# ---- decisions page, key rate, texts ------------------------------------------------

DECISIONS_PAGE = """
<a class="dropdown_title _active referenceable" role="button">Заседание Совета директоров от 11.09.2026</a>
<div class="document-regular"><div class="document-regular_date">11.09.2026</div>
<div class="document-regular_name"><a class="referenceable" href="/press/pr/?file=11092026_133000key.htm" id="a1">
<span class="document-regular_name_visible">Банк России принял решение сохранить ключевую ставку на&#160;уровне 14,00% годовых</span></a></div></div>
<div class="document-regular"><div class="document-regular_date">11.09.2026</div>
<div class="document-regular_name"><a class="referenceable" href="/press/event/?id=32832" id="a2">
<span class="document-regular_name_visible">Заявление Председателя Банка России</span></a></div></div>
<a class="dropdown_title _active referenceable" role="button">Заседание Совета директоров от 25.10.2024</a>
<div class="document-regular"><div class="document-regular_date">06.11.2024</div>
<div class="document-regular_name"><a class="referenceable" href="/dkp/mp_dec/decision_key_rate/summary_key_rate_06112024/" id="a3">
<span class="document-regular_name_visible">Резюме обсуждения ключевой ставки</span></a></div></div>
<div class="document-regular"><div class="document-regular_date">25.10.2024</div>
<div class="document-regular_name"><a class="referenceable" href="/Content/Document/File/166586/forecast_241025.pdf" id="a4">
<span class="document-regular_name_visible">Среднесрочный прогноз Банка России</span></a></div></div>
<div class="document-regular"><div class="document-regular_date">25.10.2024</div>
<div class="document-regular_name"><a class="referenceable" href="/press/pr/?file=25102024_133000Key.htm" id="a5">
<span class="document-regular_name_visible">Банк России принял решение повысить ключевую ставку на&#160;200&#160;б.п., до&#160;21,00% годовых</span></a></div></div>
"""


def test_parse_decision_documents():
    docs = expectations.parse_decision_documents(DECISIONS_PAGE)
    by = {(d["meeting_date"], d["doc_type"]): d for d in docs}
    assert set(by) == {("2026-09-11", "press_release"), ("2026-09-11", "statement"),
                       ("2024-10-25", "summary"), ("2024-10-25", "forecast"),
                       ("2024-10-25", "press_release")}      # upper-case "Key" still a press release
    assert by[("2024-10-25", "summary")]["published"] == "2024-11-06"
    assert by[("2026-09-11", "press_release")]["url"] == "https://www.cbr.ru/press/pr/?file=11092026_133000key.htm"
    # the title alone carries the decision
    title = by[("2024-10-25", "press_release")]["title"]
    assert cbr_tracker.extract_rate_decision(title) == (19.0, 21.0, 200)


def test_parse_key_rate_table():
    page = ("<tr><td>17.09.2026</td><td>14,00</td></tr><tr><td>16.09.2026</td><td>14,00</td></tr>"
            "<tr><td>17.09.2013</td><td>5,50</td></tr>")
    assert expectations.parse_key_rate_table(page) == [
        ("2013-09-17", 5.5), ("2026-09-16", 14.0), ("2026-09-17", 14.0)]


@pytest.mark.parametrize("text,expected_start", [
    ("Совет директоров принял решение. Банк России будет принимать дальнейшие решения по ключевой ставке "
     "в зависимости от динамики инфляции. Следующее заседание 23 октября.", "Банк России будет принимать"),
    ("Решение. Банк России будет оценивать целесообразность дальнейшего снижения ключевой ставки на ближайших "
     "заседаниях.", "Банк России будет оценивать"),
    ("Решение. Банк России допускает возможность повышения ключевой ставки на ближайшем заседании.",
     "Банк России допускает"),
])
def test_extract_signal(text, expected_start):
    assert expectations.extract_signal(text).startswith(expected_start)


def test_extract_signal_absent():
    assert expectations.extract_signal("Банк России опубликовал доклад.") is None
    assert expectations.extract_signal(None) is None


SUMMARY_2026 = (
    "Резюме обсуждения ключевой ставки Содержание Выводы для денежно-кредитной политики и решение по ключевой ставке "
    "Ситуация в экономике … Выводы для денежно-кредитной политики и решение по ключевой ставке "
    "Участники рассмотрели обновленные прогнозные расчеты: базовый сценарий и его вариации. По итогам анализа "
    "данных и с учетом прогнозов участники детально обсудили следующие варианты решения : сохранение ключевой "
    "ставки на уровне 14,25% годовых; снижение ключевой ставки на 25 б.п., до 14,00% годовых. Основная часть "
    "участников обсуждения отметили, что пространство для снижения ключевой ставки до конца года сохраняется, "
    "хотя и сузилось. Основные аргументы участников, предлагавших сохранить ключевую ставку неизменной , "
    "следующие. Существенное ускорение текущего роста цен."
)
SUMMARY_2024 = (
    "Выводы для денежно-кредитной политики и решение по ключевой ставке Участники обсуждения рассмотрели "
    "обновленные прогнозные расчеты: базовый сценарий и набор вариаций к нему. Участники обсуждения согласились, "
    "что необходимо дальнейшее ужесточение денежно-кредитных условий для возвращения инфляции к цели. Участники "
    "назвали следующие основные аргументы в пользу ужесточения: Инфляция выше прогноза."
)


def test_extract_summary_options_current_format():
    got = expectations.extract_summary_options(SUMMARY_2026)
    assert got["options"] == ("сохранение ключевой ставки на уровне 14,25% годовых; "
                              "снижение ключевой ставки на 25 б.п., до 14,00% годовых")
    assert got["positions"].startswith("Основная часть участников обсуждения отметили")
    assert "аргумент" not in got["positions"]


def test_extract_summary_options_without_a_listed_options_block():
    got = expectations.extract_summary_options(SUMMARY_2024)
    assert got["options"] is None
    assert got["positions"].startswith("Участники обсуждения согласились")


def test_article_text_keeps_what_html_to_text_drops():
    page = ('<html><script>var a=1;</script><h1>Резюме обсуждения</h1>'
            '<div class="landing-text"><p>Участники обсуждения: члены Совета директоров.</p></div>'
            '<p>участники детально обсудили следующие <b>варианты решения</b>:</p><ul><li>сохранение</li></ul>'
            '<div>Страница была полезной?</div><footer>107016, Москва</footer></html>')
    text = expectations.article_text(page)
    assert "варианты решения" in text
    assert "var a" not in text and "Москва" not in text


# ---- store + refreshers -----------------------------------------------------------------

FORECAST_TEXT = (
    "Основные параметры прогноза Банка России в рамках базового сценария \n"
    " 2025 \n(факт) 2026 2027 2028 2029 \n"
    "Инфляция, в %, декабрь к декабрю предыдущего года 5,6 6,0–7,0 4,0 4,0 4,0 \n"
    "Ключевая ставка, в среднем за год, в % годовых 19,2 14,5–14,61 10,5–12,5 8,0–9,0 7,5–8,5 \n"
    "С учетом того, что с 1 января по 26 июля 2026 года средняя ключевая ставка равна 15,0%, с 27 июля до "
    "конца 2026 года средняя ключевая ставка прогнозируется в диапазоне 13,7–14,0%.\n"
)


def test_store_forecast_text_keeps_years_and_the_rest_of_year_footnote(tmp_path):
    con = _con(tmp_path)
    url = "https://www.cbr.ru/Content/Document/File/194258/forecast_260724.pdf"
    assert expectations.store_forecast_text(con, "2026-07-24", "2026-07-24", url, FORECAST_TEXT)
    rows = {(r["period"], r["year"]): r for r in db.rate_expectations(con, "cbr_forecast")}
    assert (rows[("year", 2026)]["lo"], rows[("year", 2026)]["hi"]) == (14.5, 14.6)
    assert (rows[("year", 2028)]["lo"], rows[("year", 2028)]["hi"]) == (8.0, 9.0)
    rest = rows[("rest_of_year", 2026)]
    assert (rest["lo"], rest["hi"], rest["ytd_avg"], rest["rest_from"]) == (13.7, 14.0, 15.0, "2026-07-27")
    assert all(r["url"] == url for r in rows.values())
    con.close()


def test_store_forecast_text_refuses_a_misaligned_table(tmp_path):
    con = _con(tmp_path)
    broken = FORECAST_TEXT.replace("8,0–9,0 7,5–8,5 \n", "8,0–9,0 \n")
    assert not expectations.store_forecast_text(con, "2026-07-24", "2026-07-24", "u", broken)
    assert db.rate_expectations(con, "cbr_forecast") == []
    codes = [r["code"] for r in con.execute("SELECT code FROM pipeline_health")]
    assert "EXPECT_PARSE_FAIL" in codes
    con.close()


class _Resp:
    def __init__(self, text="", content=b""):
        self.text, self.content = text, content


def test_refresh_ondkp_keeps_the_draft_when_the_final_edition_replaces_it(tmp_path, monkeypatch):
    con = _con(tmp_path)
    pages = {expectations.ONDKP_INDEX_URL: '<a href="/about_br/publ/ondkp/on_2027_2029/">x</a>'}
    edition_url = f"{expectations.ONDKP_INDEX_URL}on_2027_2029/"
    monkeypatch.setattr(expectations, "_get", lambda url, **kw: _Resp(pages[url]))

    pages[edition_url] = _ondkp_page("Проект от 31 августа 2026 года")
    expectations.refresh_ondkp(con)
    pages[edition_url] = _ondkp_page("26 октября 2026 года")
    expectations.refresh_ondkp(con)

    versions = db.rate_expectation_versions(con, "ondkp")
    assert versions == [("on_2027_2029", "2026-08-31", "draft"), ("on_2027_2029", "2026-10-26", "final")]
    con.close()


def test_refresh_ondkp_skips_an_older_edition_with_a_different_scenario_set(tmp_path, monkeypatch):
    con = _con(tmp_path)
    old_page = _ondkp_page("1 ноября 2024 года", first_year=2023,
                           tables={"base": ["16,0", "17,0", "12,0", "8,0", "7,5"]})
    pages = {expectations.ONDKP_INDEX_URL: ('<a href="/about_br/publ/ondkp/on_2027_2029/">x</a>'
                                            '<a href="/about_br/publ/ondkp/on_2025_2027/">x</a>'),
             f"{expectations.ONDKP_INDEX_URL}on_2027_2029/": _ondkp_page("Проект от 31 августа 2026 года"),
             f"{expectations.ONDKP_INDEX_URL}on_2025_2027/": old_page}
    monkeypatch.setattr(expectations, "_get", lambda url, **kw: _Resp(pages[url]))
    report = expectations.refresh_ondkp(con, editions=2)
    assert report["stored"] == ["on_2027_2029"] and report["skipped"] == ["on_2025_2027"]
    # the newest edition failing, though, is loud
    pages[f"{expectations.ONDKP_INDEX_URL}on_2027_2029/"] = old_page
    with pytest.raises(ValueError):
        expectations.refresh_ondkp(con)
    con.close()


def test_refresh_survey_downloads_the_workbook_only_for_a_new_survey(tmp_path, monkeypatch):
    con = _con(tmp_path)
    calls = []
    workbook = _survey_workbook()

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(SURVEY_PAGE) if url == expectations.SURVEY_PAGE_URL else _Resp(content=workbook)
    monkeypatch.setattr(expectations, "_get", fake_get)
    monkeypatch.setattr(expectations, "RAW_DIR", tmp_path / "raw")

    assert expectations.refresh_survey(con) > 0
    assert {e for e, _, _ in db.rate_expectation_versions(con, "survey")} == {"2026-07", "2026-09"}
    downloads = sum(1 for c in calls if c.endswith("full.xlsx"))
    assert expectations.refresh_survey(con) == 0            # page still says September: no download
    assert sum(1 for c in calls if c.endswith("full.xlsx")) == downloads
    assert db.expectation_sources(con)["survey"]["last_published"] == "2026-09-02"
    con.close()


def test_run_expectations_isolates_a_failing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(expectations, "refresh_calendar", lambda con: 3)
    monkeypatch.setattr(expectations, "refresh_key_rate", lambda con, full=False: 10)

    def boom(con, bodies=None):
        raise ValueError("decisions page: no documents parsed; layout changed?")
    monkeypatch.setattr(expectations, "refresh_documents", boom)
    monkeypatch.setattr(expectations, "refresh_ondkp", lambda con, editions=1: {"rows": 16})
    monkeypatch.setattr(expectations, "refresh_survey", lambda con, force=False: 0)

    report = expectations.run_expectations()
    assert report["documents"]["ok"] is False
    assert all(report[k]["ok"] for k in ("calendar", "key_rate", "ondkp", "survey"))
    con = db.connect()
    sources = db.expectation_sources(con)
    assert "layout changed" in sources["documents"]["last_error"]
    assert sources["documents"]["page_url"] == expectations.DECISIONS_URL
    assert "EXPECT_FETCH_FAIL" in [r["code"] for r in con.execute("SELECT code FROM pipeline_health")]
    con.close()


def test_a_meeting_fetch_files_its_documents_and_forecast_the_same_day(tmp_path):
    con = _con(tmp_path)
    db.init_cbr_diff(con)
    url = "https://cbr.ru/Content/Document/File/194258/forecast_260724.pdf"
    cbr_tracker.store_meeting(con, {
        "meeting_date": "2026-07-24", "is_anchor": 1, "fetched_on": "2026-07-24",
        "press_release_text": "…снизить ключевую ставку на 25 б.п., до 14,00% годовых…",
        "rate_after": 14.0, "rate_change_bp": -25, "forecast_text": FORECAST_TEXT,
        "document_urls": {"press_release": "https://cbr.ru/press/pr/?file=24072026_133000key.htm",
                          "forecast": url},
    })
    docs = {d["doc_type"]: d for d in db.cbr_documents(con)}
    assert docs["press_release"]["text"].startswith("…снизить")
    assert docs["forecast"]["url"] == url
    assert {r["year"] for r in db.rate_expectations(con, "cbr_forecast")} >= {2026, 2027, 2028, 2029}
    con.close()


def test_every_source_page_is_an_official_cbr_url():
    for key, url in expectations.SOURCE_PAGES.items():
        assert url.startswith("https://www.cbr.ru/"), key
