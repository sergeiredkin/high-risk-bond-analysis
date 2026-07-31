"""Tests for scrape/nra.py (ra-national.ru /list-of-credit-ratings/ parser).
Parser tested against saved-shape HTML fixtures (real row markup, captured
2026-07-31), not a live fetch. Two table shapes are exercised: one WITH an
ИНН + Статус column (the 2023-2025 tables), one WITHOUT (the 2020-2022
tables) -- column position differs, so the parser must read each table's own
header row rather than assume a fixed layout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.scrape import nra  # noqa: E402

# Table WITH ИНН + Статус columns (real markup shape from wpdtSimpleTable-51,
# the 2025 table) -- one normal row, one withdrawn row (dash placeholder in
# the rating cell, 'Отозван' status).
TABLE_WITH_INN = """
<table id="wpdtSimpleTable-51" class="wpdtSimpleTable wpDataTable" data-rows="2">
<tbody>
<tr class="wpdt-cell-row odd">
    <td>№</td>
    <td>Наименование рейтингуемого лица</td>
    <td>ИНН</td>
    <td>Вид рейтинга</td>
    <td>Присвоен рейтинг</td>
    <td>Прогноз по рейтингу</td>
    <td>Дата публикации</td>
    <td>Статус рейтинга</td>
    <td>Запрошенный (незапрошенный)</td>
</tr>
<tr class="wpdt-cell-row even">
    <td>1</td>
    <td>АО «ТКХ»</td>
    <td>7704484400</td>
    <td>Кредитный рейтинг нефинансовых компаний</td>
    <td>A|ru|</td>
    <td>Стабильный</td>
    <td>30.12.2025</td>
    <td>Присвоен</td>
    <td>Запрошенный</td>
</tr>
<tr class="wpdt-cell-row odd">
    <td>34</td>
    <td>ООО «РЕАТОРГ»</td>
    <td>5036117570</td>
    <td>Кредитный рейтинг нефинансовых компаний</td>
    <td>—</td>
    <td>—</td>
    <td>31.10.2025</td>
    <td>Отозван</td>
    <td>Запрошенный</td>
</tr>
</tbody>
</table>
"""

# Table WITHOUT an ИНН/Статус column (real shape from wpdtSimpleTable-16, the
# 2021 table) -- name-only matching is the only option for these rows.
TABLE_WITHOUT_INN = """
<table id="wpdtSimpleTable-16" class="wpdtSimpleTable wpDataTable" data-rows="1">
<tbody>
<tr class="wpdt-cell-row odd">
    <td>№</td>
    <td>Наименование рейтингуемого лица</td>
    <td>Вид рейтинга</td>
    <td>Присвоен рейтинг</td>
    <td>Прогноз по рейтингу</td>
    <td>Дата публикации</td>
    <td>Запрошенный</td>
</tr>
<tr class="wpdt-cell-row even">
    <td>1</td>
    <td>ООО «Сибнефтехимтрейд»</td>
    <td>Кредитный рейтинг нефинансовых компаний</td>
    <td>B+|ru|</td>
    <td>Стабильный</td>
    <td>25.11.2024</td>
    <td>Запрошенный</td>
</tr>
</tbody>
</table>
"""


def test_parses_row_from_table_with_inn():
    rows = nra.parse_ratings(TABLE_WITH_INN)
    tkh = next(r for r in rows if r.inn == "7704484400")
    assert tkh.name == "АО «ТКХ»"
    assert tkh.name_core == "ТКХ"
    assert tkh.rating == "ruA"
    assert tkh.outlook == "Стабильный"
    assert tkh.rated_on == "2025-12-30"
    assert tkh.is_withdrawn is False
    assert tkh.source_table == "wpdtSimpleTable-51"


def test_withdrawn_row_has_no_rating_value():
    rows = nra.parse_ratings(TABLE_WITH_INN)
    reatorg = next(r for r in rows if r.inn == "5036117570")
    assert reatorg.is_withdrawn is True
    assert reatorg.rating is None
    assert reatorg.outlook is None


def test_parses_row_from_table_without_inn():
    rows = nra.parse_ratings(TABLE_WITHOUT_INN)
    assert len(rows) == 1
    sib = rows[0]
    assert sib.inn is None
    assert sib.name_core == "СИБНЕФТЕХИМТРЕЙД"
    assert sib.rating == "ruB+"
    assert sib.rated_on == "2024-11-25"
    assert sib.source_table == "wpdtSimpleTable-16"


def test_rating_conversion_strips_ru_pipes():
    assert nra._to_ru_rating("A|ru|") == "ruA"
    assert nra._to_ru_rating("BB-|ru|") == "ruBB-"


def test_rating_conversion_normalises_en_dash_to_ascii_hyphen():
    # НРА renders the minus modifier as a Unicode en-dash in some rows
    # (confirmed live: 'BBB–|ru|' for Оил Ресурс) -- must normalise to ASCII
    # '-' or default_probs.rating_rank() silently fails to recognise it.
    assert nra._to_ru_rating("BBB–|ru|") == "ruBBB-"
    from hybond.compute import default_probs
    assert default_probs.rating_rank(nra._to_ru_rating("BBB–|ru|")) is not None


def test_match_by_inn_exact():
    rows = nra.parse_ratings(TABLE_WITH_INN)
    hit = nra.match_by_inn("7704484400", rows)
    assert hit is not None
    assert hit.rating == "ruA"


def test_match_by_inn_none_when_absent():
    rows = nra.parse_ratings(TABLE_WITH_INN)
    assert nra.match_by_inn("0000000000", rows) is None


def test_match_by_name_fallback_for_no_inn_table():
    rows = nra.parse_ratings(TABLE_WITHOUT_INN)
    hit = nra.match_by_name('ООО "Сибнефтехимтрейд"', rows)
    assert hit is not None
    assert hit.rating == "ruB+"


def test_match_prefers_inn_over_name():
    rows = nra.parse_ratings(TABLE_WITH_INN)
    # ИНН present and correct -> must be used even if name given differs in casing/quotes
    hit = nra.match("7704484400", 'ООО "Совершенно другое имя"', rows)
    assert hit is not None
    assert hit.inn == "7704484400"


def test_match_falls_back_to_name_when_inn_not_found():
    rows = nra.parse_ratings(TABLE_WITHOUT_INN)
    hit = nra.match(None, 'ООО "Сибнефтехимтрейд"', rows)
    assert hit is not None
    assert hit.rating == "ruB+"


def test_latest_picked_when_multiple_rows_share_inn():
    older = nra.NraRating(name="X", name_core="X", inn="123", rating="ruBB",
                          outlook="стабильный", rated_on="2023-01-01")
    newer = nra.NraRating(name="X", name_core="X", inn="123", rating="ruB",
                          outlook="негативный", rated_on="2025-06-01")
    hit = nra.match_by_inn("123", [older, newer])
    assert hit.rating == "ruB"
    assert hit.rated_on == "2025-06-01"
