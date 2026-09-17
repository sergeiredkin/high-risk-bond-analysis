"""«Ставка и ОФЗ» updates itself (captain, 2026-09-17: "it should update this
page automatically. gather data after release and publish in the same format
and recalculate").

A fake cbr.ru serves the state before and after a release day -- a new
key-rate decision with its forecast, the final ОНЕГДКП edition replacing the
draft on the same URL, a new macro survey -- and the test runs the very
steps daily_run.sh runs, in its order, asserting that nothing needs a hand:
new publications are stored in the same format beside the old ones, the
scenario deltas are recalculated from them, and the exported page data
(scenarios, history, decisions, sources) moves with them. No network.
"""
import io
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hybond import cbr_tracker, expectations, portfolio  # noqa: E402
from hybond.store import db  # noqa: E402
import export_dashboard_data as export  # noqa: E402

W = "https://www.cbr.ru"


# ---- the fake cbr.ru -------------------------------------------------------------

def _decisions_page(meetings):
    """meetings: [(dd.mm.yyyy, press_title, forecast_file_or_None)] newest first."""
    out = []
    for d, title, fc in meetings:
        dd, mm, yyyy = d.split(".")
        out.append(f'<a class="dropdown_title">Заседание Совета директоров от {d}</a>')
        out.append(f'<div class="document-regular_date">{d}</div><a class="referenceable" href="/press/pr/?file={dd}{mm}{yyyy}_133000key.htm">'
                   f'<span class="document-regular_name_visible">{title}</span></a>')
        if fc:
            out.append(f'<div class="document-regular_date">{d}</div><a class="referenceable" href="/Content/Document/File/1/{fc}">'
                       f'<span class="document-regular_name_visible">Среднесрочный прогноз Банка России</span></a>')
    return "\n".join(out)


def _forecast_text(day, month_gen, rates, rest):
    return (f"Среднесрочный прогноз Банка России по итогам заседания {day} {month_gen} 2026 года\n"
            "Основные параметры прогноза Банка России в рамках базового сценария\n"
            " 2025 \n(факт) 2026 2027 2028 2029 \n"
            "Инфляция, в %, декабрь к декабрю предыдущего года 5,6 6,0–7,0 4,0 4,0 4,0 \n"
            f"Ключевая ставка, в среднем за год, в % годовых 19,2 {rates} \n"
            f"С учетом того, что с 1 января по {rest[0]} 2026 года средняя ключевая ставка равна 15,0%, с {rest[1]} до "
            f"конца 2026 года средняя ключевая ставка прогнозируется в диапазоне {rest[2]}%.\n")


def _ondkp_page(date_line, pro_2027):
    def table(rates):
        years = "".join(f"<th>{y}</th>" for y in range(2025, 2030))
        cells = "".join(f"<td>{v}</td>" for v in rates)
        return f"<table><tr><th></th>{years}</tr><tr><td>Ключевая ставка, в среднем за год, % годовых</td>{cells}</tr></table>"
    return "\n".join([
        f'<div class="news-info-line_date">{date_line}</div>',
        "<h3>Базовый сценарий</h3>", table(["19,2", "14,5-14,6", "10,5–12,5", "8,0–9,0", "7,5–8,5"]),
        "<a>Сценарий &#171;Дезинфляционный&#187;</a>", table(["19,2", "14,5-14,6", "9,0–11,0", "7,0–8,0", "7,5–8,5"]),
        "<a>Сценарий &#171;Проинфляционный&#187;</a>", table(["19,2", "14,5-14,6", pro_2027, "11,0–12,0", "8,5–9,5"]),
        "<a>Сценарий &#171;Рисковый&#187;</a>", table(["19,2", "14,5-14,6", "19,0–21,0", "14,0–16,0", "9,5–10,5"]),
    ])


def _survey_workbook(periods):
    """periods: [(datetime, median_2027)]"""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    nav = wb.active
    nav.title = "Навигация (Navigation) "
    nav.append([None, "Ключевая ставка\nKey rate", "(в % годовых)", 3])
    ws = wb.create_sheet("3")
    ws.append([None, None, None, "Прогнозный период"] + [p for p, _ in periods])
    ws.append([None, "Медиана", "Median", datetime(2027, 12, 31)] + [m for _, m in periods])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _calendar():
    return """
<div class="main-events_day"><div class="date col-md-5">24&nbsp;июля 2026 года</div>
<div class="title">Заседание Совета директоров Банка России по&nbsp;ключевой ставке</div><div class="info">Пресс-релиз<br />Среднесрочный прогноз</div></div>
<div class="main-events_day"><div class="date col-md-5">23&nbsp;октября 2026 года</div>
<div class="title">Заседание Совета директоров Банка России по&nbsp;ключевой ставке</div><div class="info">Пресс-релиз<br />Среднесрочный прогноз</div></div>
<div class="main-events_day"><div class="date col-md-5">18&nbsp;декабря 2026 года</div>
<div class="title">Заседание Совета директоров Банка России по&nbsp;ключевой ставке</div></div>
"""


class FakeCBR:
    """URL -> page, switchable from "before" to "after" the release."""

    def __init__(self):
        self.state = "before"

    def pages(self):
        before = self.state == "before"
        meetings = [("24.07.2026", "Банк России принял решение снизить ключевую ставку на 25 б.п., до 14,00% годовых",
                     "forecast_260724.pdf")]
        if not before:
            meetings.insert(0, ("23.10.2026", "Банк России принял решение снизить ключевую ставку на 50 б.п., до 13,50% годовых",
                                "forecast_261023.pdf"))
        survey_page = ('<div class="news-info-line_date">{}</div><h2>Результаты опроса: {} 2026 года</h2>'
                       '<a href="/Content/Document/File/144490/full.xlsx">x</a>').format(
            *(("2 сентября 2026 года", "сентябрь") if before else ("27 октября 2026 года", "октябрь")))
        return {
            expectations.DECISIONS_URL: _decisions_page(meetings),
            expectations.ONDKP_INDEX_URL: '<a href="/about_br/publ/ondkp/on_2027_2029/">ОНЕГДКП</a>',
            f"{expectations.ONDKP_INDEX_URL}on_2027_2029/": (_ondkp_page("Проект от 31 августа 2026 года", "13,0–15,0") if before
                                                            else _ondkp_page("26 октября 2026 года", "12,0–14,0")),
            expectations.SURVEY_PAGE_URL: survey_page,
            f"{W}/Content/Document/File/144490/full.xlsx": _survey_workbook(
                [(datetime(2026, 9, 1), 12.43)] + ([] if before else [(datetime(2026, 10, 1), 11.8)])),
            expectations.KEY_RATE_URL: ("<tr><td>28.07.2026</td><td>14,00</td></tr>" if before else
                                        "<tr><td>28.07.2026</td><td>14,00</td></tr><tr><td>26.10.2026</td><td>13,50</td></tr>"),
            cbr_tracker.CALENDAR_URL: _calendar(),
            f"{W}/press/pr/?file=24072026_133000key.htm": ('<div class="landing-text"><p>Совет директоров принял решение '
                                                          'снизить ключевую ставку на 25 б.п., до 14,00% годовых. Банк России будет '
                                                          'принимать дальнейшие решения по ключевой ставке в зависимости от инфляции.</p></div>'),
            f"{W}/press/pr/?file=23102026_133000key.htm": ('<div class="landing-text"><p>Совет директоров принял решение '
                                                          'снизить ключевую ставку на 50 б.п., до 13,50% годовых. Банк России будет '
                                                          'оценивать целесообразность дальнейшего снижения ключевой ставки.</p></div>'),
            f"{W}/Content/Document/File/1/forecast_260724.pdf": _forecast_text(
                24, "июля", "14,5–14,61 10,5–12,5 8,0–9,0 7,5–8,5", ("26 июля", "27 июля", "13,7–14,0")),
            f"{W}/Content/Document/File/1/forecast_261023.pdf": _forecast_text(
                23, "октября", "14,4–14,5 9,5–11,5 8,0–9,0 7,5–8,5", ("25 октября", "26 октября", "13,5–13,6")),
        }

    def get(self, url, params=None, timeout=30):
        body = self.pages()[url]

        class R:
            text = body if isinstance(body, str) else ""
            content = body if isinstance(body, bytes) else body.encode("utf-8")
        return R()


@pytest.fixture
def cbr(tmp_path, monkeypatch):
    fake = FakeCBR()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hybond.sqlite")
    monkeypatch.setattr(expectations, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(expectations, "_get", fake.get)
    monkeypatch.setattr(cbr_tracker, "_get", fake.get)
    # the "PDF" bodies above are already text
    monkeypatch.setattr(expectations, "_pdf_to_text", lambda content: content.decode("utf-8"))
    return fake


def _scheduled_run(today, now):
    """What daily_run.sh does for these pages, in its order: expectations,
    then the CBR catch-up, both before the universe run reads the scenarios."""
    report = expectations.run_expectations()
    assert all(r["ok"] for r in report.values()), report
    cbr_tracker.catch_up_missed_meetings(today, now)


def test_a_cbr_release_flows_through_to_the_page_without_a_manual_step(cbr, monkeypatch):
    # decisions are fetched by fetch_meeting(); serve the meeting documents it needs
    def fake_fetch(meeting_date, is_anchor):
        rates = {"2026-07-24": (14.25, 14.0, -25), "2026-10-23": (14.0, 13.5, -50)}
        if meeting_date not in rates or (meeting_date == "2026-10-23" and cbr.state == "before"):
            return {"meeting_date": meeting_date, "is_anchor": int(is_anchor), "fetched_on": "t"}
        b, a, bp = rates[meeting_date]
        return {"meeting_date": meeting_date, "is_anchor": int(is_anchor), "fetched_on": "t",
                "press_release_text": "…", "rate_before": b, "rate_after": a, "rate_change_bp": bp}
    monkeypatch.setattr(cbr_tracker, "fetch_meeting", fake_fetch)

    # ---- before: the first scheduled run on an empty store loads the history by itself
    _scheduled_run(date(2026, 9, 17), datetime(2026, 9, 17, 10, 0))
    con = db.connect()
    s0 = portfolio.get_rate_scenarios(con, today=date(2026, 9, 17))
    assert s0["scenario_source"] == "cbr_publications"
    assert s0["base"] == pytest.approx(-0.025)          # 11,5 − 14,0
    assert s0["stress"] == pytest.approx(0.0)           # draft Проинфляционный 14,0 − 14,0
    con.close()

    # ---- release day, morning slot: the meeting is today but not yet released
    cbr.state = "after"
    con = db.connect()
    assert [m for m in cbr_tracker.missed_meetings(con, date(2026, 10, 23), datetime(2026, 10, 23, 10, 0))
            if m["date"] == "2026-10-23"] == []
    con.close()

    # ---- release day, afternoon slot (16:00 MSK): everything new is picked up
    _scheduled_run(date(2026, 10, 23), datetime(2026, 10, 23, 16, 0))
    con = db.connect()

    # the decision, stored the same afternoon and current for the scenarios
    assert db.get_cbr_meeting(con, "2026-10-23")["rate_after"] == 13.5
    assert portfolio.current_key_rate(con) == ("2026-10-26", 13.5)
    # the new forecast, same format, beside the old one
    versions = [e for e, _, _ in db.rate_expectation_versions(con, "cbr_forecast")]
    assert versions == ["2026-07-24", "2026-10-23"]
    # the final ОНЕГДКП edition, stored as a new version of the same URL
    assert db.rate_expectation_versions(con, "ondkp") == [
        ("on_2027_2029", "2026-08-31", "draft"), ("on_2027_2029", "2026-10-26", "final")]
    # the new survey, history kept
    assert [e for e, _, _ in db.rate_expectation_versions(con, "survey")] == ["2026-09", "2026-10"]

    # recalculated from the new publications, not from anything cached
    s1 = portfolio.get_rate_scenarios(con, today=date(2026, 10, 27))
    assert s1["source_date"] == "2026-10-23"
    assert s1["current_rate"] == 13.5
    assert s1["base"] == pytest.approx((10.5 - 13.5) / 100)      # new 2027 midpoint 10,5
    assert s1["stress"] == pytest.approx((13.0 - 13.5) / 100)    # final Проинфляционный 12,0–14,0
    assert s1["levels"]["stress"]["status"] == "final"
    assert s1["analysts"] == pytest.approx((11.8 - 13.5) / 100)

    # and the page data follows, in the same format
    class _Today(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 27)
    monkeypatch.setattr(export, "date", _Today)
    page = export.expectations_export(con, s1)
    assert page["forecasts"][-1]["meeting_date"] == "2026-10-23"
    assert page["forecasts"][-1]["rest"] == {"year": 2026, "lo": 13.5, "hi": 13.6, "ytd_avg": 15.0,
                                             "rest_from": "2026-10-26"}
    assert [e["status"] for e in page["ondkp"]] == ["draft", "final"]
    assert page["surveys"][-1]["period"] == "2026-10"
    assert page["decisions"][0]["date"] == "2026-10-23" and page["decisions"][0]["change_bp"] == -50
    assert page["decisions"][0]["signal"].startswith("Банк России будет оценивать")
    stress = next(x for x in page["scenarios"] if x["key"] == "stress")
    assert stress["range"] == [12.0, 14.0] and stress["status"] == "final"
    sources = {x["key"]: x for x in page["sources"]}
    assert sources["ondkp"]["last_published"] == "2026-10-26"
    assert sources["survey"]["last_published"] == "2026-10-27"
    assert sources["cbr_forecast"]["last_published"] == "2026-10-23"
    assert all(x["last_error"] is None for x in sources.values())
    con.close()


def test_the_schedule_rebuilds_the_page_after_the_data_steps():
    """The chain has no manual link: data steps, then the AEY recalculation,
    then export and build -- and the half-hourly rebuild republishes too."""
    daily = (ROOT / "scripts" / "daily_run.sh").read_text(encoding="utf-8")
    order = ["python -m hybond expectations", "python -m hybond cbr --catch-up", "python -m hybond universe",
             "python -m hybond ofz", "scripts/export_dashboard_data.py", "scripts/build_dashboard.py"]
    positions = [daily.index(step) for step in order]
    assert positions == sorted(positions)
    rebuild = (ROOT / "rebuild_dashboard.sh").read_text(encoding="utf-8")
    assert "export_dashboard_data.py" in rebuild and "build_dashboard.py" in rebuild
