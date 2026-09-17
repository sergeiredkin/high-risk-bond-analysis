"""CBR key-rate parsing, calendar-driven schedule and missed-meeting catch-up
(Ставка и ОФЗ plan, 2026-09-17).

Every fixture below is cut from a real cbr.ru document, keeping the
typography that broke earlier parsers: the footnote digit fused onto a
number ("14,5–14,61"), a hyphen instead of an en dash (every forecast up to
February 2025), and &nbsp; in calendar dates ("7&nbsp;мая&nbsp;2026").
No network: fetches are monkeypatched.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import cbr_tracker  # noqa: E402
from hybond.store import db  # noqa: E402


# ---- rate ranges -------------------------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("14,5–14,61", (14.5, 14.6)),      # footnote 1 fused onto 14,6 (forecast 24.07.2026)
    ("13,5-15,51", (13.5, 15.5)),      # hyphen era (forecast 16.02.2024)
    ("17,51", (17.5, 17.5)),           # single value + footnote (forecast 25.10.2024)
    ("19,0 – 21,0", (19.0, 21.0)),     # spaces inside the range (ОНЕГДКП 2027-2029)
    ("14,0 –16,0", (14.0, 16.0)),
    ("7,5-8,5", (7.5, 8.5)),
    ("14.5-14.6", (14.5, 14.6)),
])
def test_parse_rate_range_handles_every_real_spelling(token, expected):
    assert cbr_tracker.parse_rate_range(token) == expected


@pytest.mark.parametrize("token", [None, "", "факт", "1-2-3", "—"])
def test_parse_rate_range_rejects_non_ranges(token):
    assert cbr_tracker.parse_rate_range(token) is None


def test_format_rate_range_round_trip():
    assert cbr_tracker.format_rate_range((14.5, 14.6)) == "14,5–14,6"
    assert cbr_tracker.format_rate_range((17.5, 17.5)) == "17,5"
    assert cbr_tracker.format_rate_range(None) is None


# ---- forecast table: years from the header, never by position ---------------

FORECAST_2026_07 = (
    "Среднесрочный прогноз Банка России \n"
    "по итогам заседания Совета директоров по ключевой ставке 24 июля 2026 года \n"
    "Основные параметры прогноза Банка России в рамках базового сценария \n"
    "(прирост в % к предыдущему году, если не указано иное) \n"
    " 2025 \n(факт) 2026 2027 2028 2029 \n"
    "Инфляция, в %, декабрь к декабрю предыдущего года 5,6 6,0–7,0 4,0 4,0 4,0 \n"
    "Инфляция, в среднем за год, в % к предыдущему году 8,7 5,9–6,2 4,3–5,2 4,0 4,0 \n"
    "Ключевая ставка, в среднем за год, в % годовых 19,2 14,5–14,61 10,5–12,5 8,0–9,0 7,5–8,5 \n"
    "Валовой внутренний продукт 1,0 0,0–1,0 1,5–2,5 1,5–2,5 1,5–2,5 \n"
    "1 С учетом того, что с 1 января по 26 июля 2026 года средняя ключевая ставка равна 15,0%, "
    "с 27 июля до конца 2026 года средняя ключевая ставка \n"
    "прогнозируется в диапазоне 13,7–14,0%. Дополнительная информация\n"
)

# A February forecast one year on: the table starts at 2026 (факт). A
# positional reader files the 2027 column under 2026.
FORECAST_2027_02 = (
    "Основные параметры прогноза Банка России в рамках базового сценария \n"
    " 2026 \n(факт) 2027 2028 2029 \n"
    "Инфляция, в %, декабрь к декабрю предыдущего года 6,5 4,0–5,0 4,0 4,0 \n"
    "Ключевая ставка, в среднем за год, в % годовых 14,5 11,0–13,01 8,5–9,5 7,5–8,5 \n"
)

FORECAST_2024_02 = (
    "Основные параметры прогноза Банка России в рамках базового сценария \n"
    " 2023 \n(факт / оценка) 2024 2025 2026 \n"
    "Инфляция, в %, декабрь к декабрю предыдущего года 7,4 4,0-4,5 4,0 4,0 \n"
    "Ключевая ставка, в среднем за год, в % годовых 9,9 13,5-15,51 8,0-10,0 6,0-7,0 \n"
    "С учетом того, что с 1 января по 18 февраля 2024 года средняя ключевая ставка равна 16,0%, с 19 февраля "
    "до конца 2024 года средняя ключевая ставка \nпрогнозируется в диапазоне 13,2-15,5%. Дополнительная\n"
)


def test_forecast_table_years_reads_the_header():
    assert cbr_tracker.forecast_table_years(FORECAST_2026_07) == [2025, 2026, 2027, 2028, 2029]
    assert cbr_tracker.forecast_table_years(FORECAST_2024_02) == [2023, 2024, 2025, 2026]


def test_forecast_rate_by_year_strips_footnotes():
    by_year = cbr_tracker.forecast_rate_by_year(FORECAST_2026_07)
    assert by_year == {2025: (19.2, 19.2), 2026: (14.5, 14.6), 2027: (10.5, 12.5),
                       2028: (8.0, 9.0), 2029: (7.5, 8.5)}


def test_forecast_rate_by_year_refuses_misaligned_columns():
    broken = FORECAST_2026_07.replace("8,0–9,0 7,5–8,5 \n", "8,0–9,0 \n")
    assert cbr_tracker.forecast_rate_by_year(broken) == {}


def test_extract_forecast_ranges_files_columns_under_their_own_year():
    ranges = cbr_tracker.extract_forecast_ranges(FORECAST_2026_07)
    assert ranges["rate_2026_mid"] == "14,5–14,6"      # footnote gone
    assert ranges["rate_2027_mid"] == "10,5–12,5"
    assert ranges["rate_2028_mid"] == "8,0–9,0"        # used to be dropped
    assert ranges["rate_2029_mid"] == "7,5–8,5"
    assert ranges["rate_neutral"] == "7,5–8,5"
    assert ranges["inflation_forecast_2026"] == "6,0–7,0"


def test_extract_forecast_ranges_does_not_shift_a_year_after_rollover():
    """Regression for the positional parser ('index 1 is always 2026'): in a
    February 2027 forecast the first forecast column is 2027."""
    ranges = cbr_tracker.extract_forecast_ranges(FORECAST_2027_02)
    assert ranges["rate_2026_mid"] is None             # 2026 is the факт column
    assert ranges["rate_2027_mid"] == "11,0–13,0"
    assert ranges["rate_2028_mid"] == "8,5–9,5"
    assert ranges["rate_2029_mid"] == "7,5–8,5"


def test_forecast_rest_of_year_en_dash_and_hyphen_eras():
    new = cbr_tracker.forecast_rest_of_year(FORECAST_2026_07)
    assert new == {"year": 2026, "lo": 13.7, "hi": 14.0, "ytd_avg": 15.0, "rest_from": "2026-07-27"}
    old = cbr_tracker.forecast_rest_of_year(FORECAST_2024_02)
    assert old == {"year": 2024, "lo": 13.2, "hi": 15.5, "ytd_avg": 16.0, "rest_from": "2024-02-19"}


def test_forecast_rest_of_year_absent():
    assert cbr_tracker.forecast_rest_of_year("Ключевая ставка, в среднем за год 19,2") is None


# ---- calendar ------------------------------------------------------------------

CALENDAR_HTML = """
<div class="calendar-main-events">
<div class="main-events_day">
<div class="date col-md-5">24&nbsp;апреля 2026 года</div>
<div class="main-events offset-md-1 col-md-17"><div class="main-event">
<div class="title"><span class="col-md-15">Заседание Совета директоров Банка России по&nbsp;ключевой ставке</span></div>
<div class="info col-md-15"><a href="/press/pr/?file=24042026_133000key.htm">Пресс-релиз</a> по&nbsp;ключевой ставке<br /><a href="/Content/Document/File/189801/forecast_260424.pdf">Среднесрочный прогноз</a> <br /><a href="/press/event/?id=28507"><span class="col-md-15">Пресс-конференция Председателя Банка России</span></a></div>
</div></div></div>
<div class="main-events_day">
<div class="date col-md-5">7&nbsp;мая&nbsp;2026 года</div>
<div class="main-events offset-md-1 col-md-17"><div class="main-event">
<div class="info col-md-15"><span class="col-md-15"><a href="/dkp/mp_dec/decision_key_rate/summary_key_rate_07052026/" target="_blank" rel="noopener">Резюме обсуждения ключевой ставки</a> <br /><a href="/dkp/mp_dec/decision_key_rate/comment_07052026/" target="_blank" rel="noopener">Комментарий к&nbsp;среднесрочному прогнозу</a></span></div>
</div></div></div>
<div class="main-events_day">
<div class="date col-md-5">11&nbsp;сентября 2026 года</div>
<div class="main-events offset-md-1 col-md-17"><div class="main-event">
<div class="title"><span class="col-md-15">Заседание Совета директоров Банка России по&nbsp;ключевой ставке</span></div>
<div class="info col-md-15"><a href="/press/pr/?file=11092026_133000key.htm">Пресс-релиз</a> по&nbsp;ключевой ставке <br /><a href="/press/event/?id=32832">Пресс-конференция Председателя Банка России</a></div>
</div></div></div>
<div class="main-events_day">
<div class="date col-md-5">23&nbsp;сентября 2026 года</div>
<div class="main-events offset-md-1 col-md-17"><div class="main-event">
<div class="info col-md-15"><span class="col-md-15">Резюме обсуждения ключевой ставки</span></div>
</div></div></div>
<div class="main-events_day">
<div class="date col-md-5">23&nbsp;октября 2026 года</div>
<div class="main-events offset-md-1 col-md-17"><div class="main-event">
<div class="title"><span class="col-md-15">Заседание Совета директоров Банка России по&nbsp;ключевой ставке</span></div>
<div class="info col-md-15">Пресс-релиз по&nbsp;ключевой ставке <br />Среднесрочный прогноз <br /><span class="col-md-15">Пресс-конференция Председателя Банка России</span></div>
</div></div></div>
</div>
"""


def test_parse_calendar_reads_meetings_summaries_and_anchor_flag():
    events = cbr_tracker.parse_calendar(CALENDAR_HTML)
    by = {(e["event_date"], e["kind"]): e for e in events}
    assert by[("2026-04-24", "meeting")]["is_anchor"] is True
    assert by[("2026-04-24", "meeting")]["url"] == "https://cbr.ru/press/pr/?file=24042026_133000key.htm"
    assert ("2026-05-07", "summary") in by               # "7&nbsp;мая&nbsp;2026"
    assert ("2026-05-07", "comment") in by
    assert by[("2026-09-11", "meeting")]["is_anchor"] is False
    # a future anchor meeting: the forecast line is listed without a link
    assert by[("2026-10-23", "meeting")]["is_anchor"] is True
    assert by[("2026-10-23", "meeting")]["url"] is None
    assert by[("2026-09-23", "summary")]["url"] is None


def test_schedule_from_events_pairs_each_meeting_with_its_resume():
    schedule = cbr_tracker.schedule_from_events(cbr_tracker.parse_calendar(CALENDAR_HTML))
    assert schedule == [
        {"date": "2026-04-24", "anchor": True, "resume_date": "2026-05-07"},
        {"date": "2026-09-11", "anchor": False, "resume_date": "2026-09-23"},
        {"date": "2026-10-23", "anchor": True, "resume_date": None},
    ]


def test_get_schedule_falls_back_to_the_hardcoded_2026_list(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    schedule = cbr_tracker.get_schedule(con)
    assert [m["date"] for m in schedule] == [m["date"] for m in cbr_tracker.CBR_MEETINGS_2026]
    con.close()


def test_get_schedule_prefers_the_stored_calendar_and_extends_past_2026(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_rates(con)
    for e in cbr_tracker.parse_calendar(CALENDAR_HTML):
        db.upsert_cbr_schedule_event(con, e["event_date"], e["kind"], e["is_anchor"], e["url"])
    db.upsert_cbr_schedule_event(con, "2027-02-12", "meeting", True)
    con.commit()
    by = {m["date"]: m for m in cbr_tracker.get_schedule(con)}
    assert by["2026-09-11"]["resume_date"] == "2026-09-23"   # calendar, not the hardcoded 09-24
    assert by["2027-02-12"]["anchor"] is True                 # a year the hardcoded list never had
    assert "2026-12-18" in by                                 # fallback still fills gaps
    con.close()


def test_refresh_schedule_refuses_an_empty_parse(tmp_path, monkeypatch):
    class _R:
        text = "<html>layout changed</html>"
    monkeypatch.setattr(cbr_tracker, "_get", lambda url: _R())
    con = db.connect(tmp_path / "t.sqlite")
    with pytest.raises(ValueError):
        cbr_tracker.refresh_schedule(con)
    con.close()


# ---- missed-meeting catch-up ---------------------------------------------------

def _seed_calendar(con):
    db.init_rates(con)
    for e in cbr_tracker.parse_calendar(CALENDAR_HTML):
        db.upsert_cbr_schedule_event(con, e["event_date"], e["kind"], e["is_anchor"], e["url"])
    con.commit()


def test_missed_meetings_finds_the_lost_1109_decision(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    _seed_calendar(con)
    db.init_cbr(con)
    db.upsert_cbr_meeting(con, meeting_date="2026-07-24", rate_after=14.0, rate_change_bp=-25,
                          press_release_text="...", resume_text="...", comment_text="...")
    missed = cbr_tracker.missed_meetings(con, date(2026, 9, 17))
    assert {"date": "2026-09-11", "anchor": False, "missing": "decision"} in missed
    # older meetings outside the window are left to a manual --fetch
    assert all(m["date"] >= "2026-07-19" for m in missed)
    con.close()


def test_missed_meetings_asks_for_a_resume_once_it_is_due(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    _seed_calendar(con)
    db.init_cbr(con)
    db.upsert_cbr_meeting(con, meeting_date="2026-09-11", rate_after=14.0, rate_change_bp=0,
                          press_release_text="...")
    assert not [m for m in cbr_tracker.missed_meetings(con, date(2026, 9, 17)) if m["date"] == "2026-09-11"]
    due = [m for m in cbr_tracker.missed_meetings(con, date(2026, 9, 25)) if m["date"] == "2026-09-11"]
    assert due == [{"date": "2026-09-11", "anchor": False, "missing": "resume"}]
    con.close()


def test_catch_up_stores_a_found_decision_and_logs_a_still_missing_one(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    con = db.connect()
    db.init_db(con)
    _seed_calendar(con)
    db.init_pipeline_health(con)
    con.close()

    def fake_fetch(meeting_date, is_anchor):
        out = {"meeting_date": meeting_date, "is_anchor": int(is_anchor), "fetched_on": "2026-09-17"}
        if meeting_date == "2026-09-11":
            out.update(press_release_text="сохранить ключевую ставку на уровне 14,00% годовых",
                       rate_before=14.0, rate_after=14.0, rate_change_bp=0)
        return out
    monkeypatch.setattr(cbr_tracker, "fetch_meeting", fake_fetch)

    report = cbr_tracker.catch_up_missed_meetings(date(2026, 9, 17))
    assert {"date": "2026-09-11", "missing": "decision", "found": True} in report

    con = db.connect()
    assert db.get_cbr_meeting(con, "2026-09-11")["rate_after"] == 14.0
    # the hardcoded fallback still lists 2026-07-24; with no row it is reported as missing
    stale = [r for r in con.execute("SELECT detail FROM pipeline_health WHERE code='CBR_STALE'")]
    assert any("2026-07-24" in r["detail"] for r in stale)
    con.close()


def test_check_and_fetch_today_acts_on_a_stored_calendar_date(tmp_path, monkeypatch):
    """A 2027 meeting the hardcoded list never had still triggers a fetch
    once the calendar is stored."""
    class _Fixed(date):
        @classmethod
        def today(cls):
            return date(2027, 2, 12)
    dbpath = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    con = db.connect()
    db.init_db(con)
    db.init_rates(con)
    db.upsert_cbr_schedule_event(con, "2027-02-12", "meeting", True)
    con.commit()
    con.close()
    monkeypatch.setattr(cbr_tracker, "date", _Fixed)
    monkeypatch.setattr(cbr_tracker, "fetch_meeting", lambda d, a: {
        "meeting_date": d, "is_anchor": int(a), "fetched_on": "2027-02-12", "press_release_text": "..."})
    result = cbr_tracker.check_and_fetch_today()
    assert result["action"] == "meeting" and result["meeting_date"] == "2027-02-12"


def test_daily_run_schedules_ofz_and_the_cbr_catch_up_as_non_fatal_steps():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "daily_run.sh").read_text(encoding="utf-8")
    assert "python -m hybond ofz" in text
    assert "python -m hybond cbr --catch-up" in text
    # both are network steps: a MOEX/cbr.ru hiccup must not abort the dashboard rebuild
    for step in ("python -m hybond ofz", "python -m hybond cbr --catch-up"):
        at = text.index(step)
        assert "non-fatal" in text[at:at + 160]
    # OFZ runs after the universe screen (which stores the day's zero curve)
    assert text.index("hybond universe") < text.index("python -m hybond ofz")
