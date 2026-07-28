"""Tests for cbr_tracker's deterministic parsing (no network). Regex/text
extraction is tested against small fixture strings shaped like the real
cbr.ru pages, not live pages -- keeps the parsers testable without a
network round-trip, per the project's snapshot-first convention.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.cbr_tracker import (  # noqa: E402
    _extract_row, _html_to_text, _midpoint, _parse_materials_index,
    extract_forecast_ranges, extract_rate_decision)


def test_extract_rate_decision_decrease():
    text = "Совет директоров Банка России 24 июля 2026 года принял решение снизить ключевую ставку на 25 б.п., до 14,00% годовых."
    before, after, bp = extract_rate_decision(text)
    assert before == 14.25
    assert after == 14.0
    assert bp == -25


def test_extract_rate_decision_increase():
    text = "принял решение повысить ключевую ставку на 100 б.п., до 21,00% годовых."
    before, after, bp = extract_rate_decision(text)
    assert before == 20.0
    assert after == 21.0
    assert bp == 100


def test_extract_rate_decision_hold():
    text = "принял решение сохранить ключевую ставку на уровне 21,00% годовых."
    before, after, bp = extract_rate_decision(text)
    assert before == after == 21.0
    assert bp == 0


def test_extract_rate_decision_no_match_returns_nones():
    before, after, bp = extract_rate_decision("невнятный текст без решения")
    assert (before, after, bp) == (None, None, None)


def test_extract_row_stops_at_row_boundary():
    # Regression guard: a fixed token count bled into the NEXT table row
    # when the forecast table had fewer forward-year columns (4 vs 5) --
    # this pins the fix (stop at newline, not at a fixed count).
    text = ("Ключевая ставка, в среднем за год, в % годовых 19,2 14,0-14,5 8,0-10,0 7,5-8,5 \n"
            "Валовой внутренний продукт 1,0 0,5-1,5 1,5-2,5 1,5-2,5 \n")
    row = _extract_row(text, "Ключевая ставка, в среднем за год")
    assert row == ["19,2", "14,0-14,5", "8,0-10,0", "7,5-8,5"]


def test_extract_forecast_ranges_four_column_vintage():
    text = ("Ключевая ставка, в среднем за год, в % годовых 19,2 14,0-14,5 8,0-10,0 7,5-8,5 \n"
            "Инфляция, в %, декабрь к декабрю предыдущего года 5,6 4,5-5,5 4,0 4,0 \n")
    ranges = extract_forecast_ranges(text)
    assert ranges["rate_2026_mid"] == "14,0-14,5"
    assert ranges["rate_2027_mid"] == "8,0-10,0"
    assert ranges["rate_neutral"] == "7,5-8,5"  # last column, not a fixed index
    assert ranges["inflation_forecast_2026"] == "4,5-5,5"


def test_extract_forecast_ranges_five_column_vintage():
    text = ("Ключевая ставка, в среднем за год, в % годовых 19,2 14,5-14,6 10,5-12,5 8,0-9,0 7,5-8,5 \n"
            "Инфляция, в %, декабрь к декабрю предыдущего года 5,6 6,0-7,0 4,0 4,0 4,0 \n")
    ranges = extract_forecast_ranges(text)
    assert ranges["rate_2026_mid"] == "14,5-14,6"
    assert ranges["rate_2027_mid"] == "10,5-12,5"
    assert ranges["rate_neutral"] == "7,5-8,5"  # still the last column, now index 4 not 3


def test_html_to_text_strips_script_and_style():
    html = ('<div class="landing-text"><p>Реальный текст.</p>'
            '<script>var x = 1;</script><style>.a{}</style></div>')
    text = _html_to_text(html)
    assert "Реальный текст." in text
    assert "var x" not in text
    assert ".a{}" not in text


def test_parse_materials_index_finds_press_and_speech_links():
    html = """
    <a id="a_1">Заседание Совета директоров от 24.07.2026</a>
    <a href="/press/pr/?file=24072026_133000key.htm">Банк России принял решение...</a>
    <a href="/press/event/?id=32726"><span>Заявление Председателя Банка России ...</span></a>
    <a id="a_2">Заседание Совета директоров от 19.06.2026</a>
    <a href="/press/pr/?file=19062026_133000key.htm">Банк России принял решение...</a>
    """
    parsed = _parse_materials_index(html)
    assert parsed["24.07.2026"]["press_release_url"] == "https://cbr.ru/press/pr/?file=24072026_133000key.htm"
    assert parsed["24.07.2026"]["speech_url"] == "https://cbr.ru/press/event/?id=32726"
    assert parsed["19.06.2026"]["press_release_url"] == "https://cbr.ru/press/pr/?file=19062026_133000key.htm"
    # non-anchor block correctly has no forecast/comment link
    assert parsed["19.06.2026"]["forecast_pdf_url"] is None


def test_midpoint_handles_en_dash_from_real_forecast_text():
    # Regression guard: the stored range strings come from PDF text
    # extraction and use an en-dash (U+2013), not ASCII "-" -- splitting on
    # "-" alone silently returned None for every real range (caught by
    # checking the actual stored cbr_meeting_diff rows, not just that the
    # function ran without raising).
    assert _midpoint("14,5–14,6") == 14.55
    assert _midpoint("10,5–12,5") == 11.5


def test_midpoint_handles_ascii_hyphen_too():
    assert _midpoint("14.5-14.6") == 14.55


def test_midpoint_none_on_unparseable():
    assert _midpoint(None) is None
    assert _midpoint("") is None
    assert _midpoint("н/д") is None
