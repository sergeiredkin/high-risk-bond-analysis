"""Tests for scrape/nkr.py (ratings.ru issuer-table parser). Parser tested
against a saved-shape HTML fixture (real row markup, captured 2026-07-31),
not a live fetch.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.scrape import nkr  # noqa: E402

# Real row markup from ratings.ru/ratings/issuers/, captured 2026-07-31.
# Includes one quoted-name row (the common case) and one no-quotes row
# (banks are often displayed as 'ПАО Сбербанк' with no «»), to exercise
# normalize_name()'s fallback path.
ISSUERS_FIXTURE = """
<table id="issuers-table" class="sortable-theme-dark" data-sortable>
<thead>
<tr role="row">
    <th>Наименование</th><th>Рейтинг</th><th>Прогноз</th>
    <th>ESG-рейтинг</th><th>Сектор</th><th>Дата</th>
</tr>
</thead>
<tbody>
<tr id="bx_3218110189_2737">
    <td>
        <a href="/ratings/issuers/L-Start/" class="blue-link">ООО «Л-Старт» </a>
    </td>
    <td data-order="14">
        <span class="mt-descr">B.ru</span>
    </td>
    <td>
        <span class="mt-descr">стабильный</span>
    </td>
    <td>
        <span class="mt-descr"></span>
    </td>
    <td>
        <span class="mt-descr">Производство машин и оборудования</span>
    </td>
    <td tabindex="5">
        <span class="mt-descr"><a class="blue-link" target="_blank"
                                  href="/ratings/press-releases/L-Start-RA-071125/">07.11.2025</a></span>
    </td>
</tr>
<tr id="bx_3218110189_1382">
    <td>
        <a href="/ratings/issuers/Sberbank/" class="blue-link">ПАО Сбербанк</a>
    </td>
    <td data-order="0">
        <span class="mt-descr">AAA.ru</span>
    </td>
    <td>
        <span class="mt-descr">стабильный</span>
    </td>
    <td>
        <span class="mt-descr">ESG-AAA</span>
    </td>
    <td>
        <span class="mt-descr">Банки</span>
    </td>
    <td tabindex="5">
        <span class="mt-descr"><a class="blue-link" target="_blank"
                                  href="/ratings/press-releases/Sberbank-RA-101025/">10.10.2025</a></span>
    </td>
</tr>
</tbody>
<tr id="bx_3218110189_2073">
    <td>
        <a href="/ratings/issuers/Barrel/" class="blue-link">ООО «Баррель»</a>
    </td>
    <td data-order="24">
        <span class="mt-descr">рейтинг отозван</span>
    </td>
    <td>
        <span class="mt-descr">прогноз отозван</span>
    </td>
    <td>
        <span class="mt-descr"></span>
    </td>
    <td>
        <span class="mt-descr">Торговля</span>
    </td>
    <td tabindex="5">
        <span class="mt-descr"><a class="blue-link" target="_blank"
                                  href="/ratings/press-releases/Barrel-RA-071025/">07.10.2025</a></span>
    </td>
</tr>
</table>
"""


def test_parses_all_fixture_rows():
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    assert len(rows) == 3


def test_withdrawn_rating_row_has_no_rating_value():
    # NKR shows the literal text 'рейтинг отозван' instead of a letter grade
    # for withdrawn ratings -- this must not become 'ruрейтинг отозван'.
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    barrel = next(r for r in rows if r.slug == "Barrel")
    assert barrel.is_withdrawn is True
    assert barrel.rating is None
    assert barrel.outlook is None


def test_quoted_name_row_parses_correctly():
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    lstart = next(r for r in rows if r.slug == "L-Start")
    assert lstart.name == "ООО «Л-Старт»"
    assert lstart.name_core == "Л-СТАРТ"
    assert lstart.rating == "ruB"
    assert lstart.outlook == "стабильный"
    assert lstart.sector == "Производство машин и оборудования"
    assert lstart.rated_on == "2025-11-07"
    assert lstart.press_release_url == "https://ratings.ru/ratings/press-releases/L-Start-RA-071125/"


def test_no_quotes_name_row_uses_fallback_normalisation():
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    sber = next(r for r in rows if r.slug == "Sberbank")
    assert sber.name == "ПАО Сбербанк"
    # legal-form tokens (ПАО) stripped, core name uppercased
    assert sber.name_core == "СБЕРБАНК"
    assert sber.rating == "ruAAA"


def test_rating_conversion_strips_ru_suffix_and_prefixes_ru():
    assert nkr._to_ru_rating("BB-.ru") == "ruBB-"
    assert nkr._to_ru_rating("CC.ru") == "ruCC"
    assert nkr._to_ru_rating("AAA.ru") == "ruAAA"


def test_normalize_name_matches_moex_style_quoting():
    # MOEX's emitent_title form uses straight quotes + spelled-out legal form
    moex_style = 'Общество с ограниченной ответственностью "Л-Старт"'
    nkr_style = "ООО «Л-Старт» "
    assert nkr.normalize_name(moex_style) == nkr.normalize_name(nkr_style)


def test_match_by_name_finds_exact_core_match():
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    moex_style = 'Общество с ограниченной ответственностью "Л-Старт"'
    match = nkr.match_by_name(moex_style, rows)
    assert match is not None
    assert match.slug == "L-Start"


def test_match_by_name_returns_none_when_no_match():
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    assert nkr.match_by_name("Совершенно Другая Компания", rows) is None


def test_match_by_name_does_not_false_positive_on_substring():
    # "Л-Старт" and "Старт" share a substring -- exact-core matching must
    # not conflate them (the false-positive risk this module's docstring
    # explicitly calls out as the reason it avoids fuzzy/substring matching).
    rows = nkr.parse_issuers(ISSUERS_FIXTURE)
    assert nkr.match_by_name('ООО "Старт"', rows) is None
