"""Tests for hybond.pd_import: import_pd_table() (Aggregate_Freq_Raw ->
pd_table_history) and get_pd() (latest empirical PD, hardcoded fallback).
No network -- builds a small synthetic workbook rather than depending on
the real raexpert_combined.xlsx."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import pd_import  # noqa: E402
from hybond.compute import default_probs  # noqa: E402
from hybond.store import db  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")

HEADER = ["report_date", "scale_type", "obj_type", "category",
          "freq_1y", "freq_2y", "freq_3y", "is_detailed"]


def _make_workbook(path, rows, sheet_name="Aggregate_Freq_Raw"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


# A realistic small slice: one russia_national/all_except_structured
# detailed-scale report (the shape get_pd()'s exact-match path expects),
# one russia_national/all_except_structured category-scale report (older
# style, no "ru" prefix), one belarus row that MUST be excluded (same
# report_date/category/obj_type as a russia_national row -- collision test),
# one structured_bonds row that MUST be excluded, and one all-None-freq row
# that MUST be skipped.
BASE_ROWS = [
    ("01.07.2026", "russia_national", "all_except_structured", "ruBB", 4.60, 12.31, 19.14, True),
    ("01.07.2026", "russia_national", "all_except_structured", "ruBBB", 1.98, 5.0, 8.0, True),
    ("01.01.2021", "russia_national", "all_except_structured", "BB", 3.0, 7.0, 11.0, False),
    ("01.07.2026", "belarus", "all_except_structured", "ruBB", 99.0, 99.0, 99.0, True),
    ("01.07.2026", "russia_national", "structured_bonds", "ruBB", 50.0, 50.0, 50.0, True),
    ("01.07.2026", "russia_national", "all_except_structured", "ruCC", None, None, None, True),
]


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    return con


# --- import_pd_table ----------------------------------------------------

def test_import_pd_table_happy_path(tmp_path):
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    result = pd_import.import_pd_table(str(xlsx), con)

    # 3 real rows qualify (russia_national + all_except_structured + not
    # all-None): ruBB@01.07.2026, ruBBB@01.07.2026, BB@01.01.2021. The
    # belarus row (scale collision) and structured_bonds row are excluded
    # by the obj_type/scale_type gate; the all-None ruCC row is skipped.
    assert result["rows_imported"] == 3
    assert result["rows_skipped"] == 1
    assert result["report_dates_found"] == ["01.01.2021", "01.07.2026"]

    rows = con.execute("SELECT * FROM pd_table_history ORDER BY report_date, rating").fetchall()
    assert len(rows) == 3
    by_key = {(r["report_date"], r["rating"]): r for r in rows}
    ru_bb = by_key[("2026-07-01", "ruBB")]
    assert ru_bb["rating_format"] == "detailed"
    assert ru_bb["mapped_category"] == "BB"
    assert abs(ru_bb["freq_1y"] - 0.046) < 1e-9
    assert abs(ru_bb["freq_2y"] - 0.1231) < 1e-9
    assert abs(ru_bb["freq_3y"] - 0.1914) < 1e-9
    assert ru_bb["obj_type"] == "all_except_structured"
    assert ru_bb["source"] == "raexpert"

    cat_bb = by_key[("2021-01-01", "BB")]
    assert cat_bb["rating_format"] == "category"
    assert cat_bb["mapped_category"] == "BB"

    # Belarus/structured rows never made it in at all.
    ratings_present = {r["rating"] for r in rows}
    assert "ruCC" not in ratings_present  # the all-None row


def test_import_pd_table_idempotent_reimport(tmp_path):
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    first = pd_import.import_pd_table(str(xlsx), con)
    second = pd_import.import_pd_table(str(xlsx), con)

    assert first["rows_imported"] == 3
    assert second["rows_imported"] == 0
    assert second["rows_skipped"] == 4  # 3 duplicates + the same all-None row
    total = con.execute("SELECT COUNT(*) c FROM pd_table_history").fetchone()["c"]
    assert total == 3  # not doubled


def test_import_pd_table_missing_sheet_raises_value_error(tmp_path):
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", BASE_ROWS, sheet_name="Wrong_Sheet")
    con = _con(tmp_path)
    with pytest.raises(ValueError):
        pd_import.import_pd_table(str(xlsx), con)


def test_import_pd_table_missing_file_raises_file_not_found(tmp_path):
    con = _con(tmp_path)
    with pytest.raises(FileNotFoundError):
        pd_import.import_pd_table(str(tmp_path / "does_not_exist.xlsx"), con)


def test_import_pd_table_malformed_header_raises_value_error(tmp_path):
    path = tmp_path / "bad.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Aggregate_Freq_Raw"
    ws.append(["not", "the", "right", "columns"])
    wb.save(path)
    con = _con(tmp_path)
    with pytest.raises(ValueError):
        pd_import.import_pd_table(str(path), con)


# --- get_pd ---------------------------------------------------------------

def test_get_pd_known_rating_returns_db_value(tmp_path):
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    pd_import.import_pd_table(str(xlsx), con)

    assert abs(pd_import.get_pd("ruBB", 1, con=con) - 0.046) < 1e-9
    assert abs(pd_import.get_pd("ruBB", 2, con=con) - 0.1231) < 1e-9
    assert abs(pd_import.get_pd("ruBB", 3, con=con) - 0.1914) < 1e-9


def test_get_pd_unknown_rating_falls_back_to_category(tmp_path):
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    pd_import.import_pd_table(str(xlsx), con)

    # 'ruBB-' never appears in the DB directly, but maps to category 'BB',
    # which two rows share: the category-format 'BB'@01.01.2021 (0.03) and
    # the detailed 'ruBB'@01.07.2026 (0.046) -- the fallback picks the most
    # recent report_date among category matches, i.e. the 2026 one.
    result = pd_import.get_pd("ruBB-", 1, con=con)
    assert abs(result - 0.046) < 1e-9


def test_get_pd_rating_absent_everywhere_in_db_falls_back_to_hardcoded(tmp_path):
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    pd_import.import_pd_table(str(xlsx), con)

    # 'ruAAA' isn't in the synthetic DB (exact or category) -- hardcoded.
    result = pd_import.get_pd("ruAAA", 1, con=con)
    assert result == default_probs.DEFAULT_PROBS_1YR["ruAAA"]


def test_get_pd_empty_table_uses_hardcoded_fallback(tmp_path):
    con = _con(tmp_path)
    db.init_pd_table_history(con)  # table exists but has zero rows

    result = pd_import.get_pd("ruBB", 1, con=con)
    assert result == default_probs.DEFAULT_PROBS_1YR["ruBB"]


def test_get_pd_no_connection_uses_hardcoded_fallback():
    result = pd_import.get_pd("ruBB", 1, con=None)
    assert result == default_probs.DEFAULT_PROBS_1YR["ruBB"]


def test_get_pd_unrecognised_rating_returns_zero(tmp_path):
    con = _con(tmp_path)
    db.init_pd_table_history(con)
    assert pd_import.get_pd("not-a-real-rating", 1, con=con) == 0.0
    assert pd_import.get_pd("not-a-real-rating", 1, con=None) == 0.0


# --- RATING_TO_CATEGORY ----------------------------------------------------

def test_rating_to_category_all_mappings():
    expected = {
        'ruAAA': 'AAA', 'AAA': 'AAA',
        'ruAA+': 'AA', 'ruAA': 'AA', 'ruAA-': 'AA', 'AA': 'AA',
        'ruA+': 'A', 'ruA': 'A', 'ruA-': 'A', 'A': 'A',
        'ruBBB+': 'BBB', 'ruBBB': 'BBB', 'ruBBB-': 'BBB', 'BBB': 'BBB',
        'ruBB+': 'BB', 'ruBB': 'BB', 'ruBB-': 'BB', 'BB': 'BB',
        'ruB+': 'B', 'ruB': 'B', 'ruB-': 'B', 'B': 'B',
        'ruCCC': 'CCC', 'CCC': 'CCC',
        'ruCC': 'CC', 'ruC': 'CC', 'CC': 'CC',
    }
    assert pd_import.RATING_TO_CATEGORY == expected


def test_rating_to_category_tier_variants_agree():
    # +/base/- variants of a detailed tier must all collapse to the same
    # broad category as each other and as their plain-letter counterpart.
    tiers = [
        (['ruAA+', 'ruAA', 'ruAA-'], 'AA'),
        (['ruA+', 'ruA', 'ruA-'], 'A'),
        (['ruBBB+', 'ruBBB', 'ruBBB-'], 'BBB'),
        (['ruBB+', 'ruBB', 'ruBB-'], 'BB'),
        (['ruB+', 'ruB', 'ruB-'], 'B'),
    ]
    for variants, category in tiers:
        for v in variants:
            assert pd_import.RATING_TO_CATEGORY[v] == category


# --- report_date formatting -----------------------------------------------

def test_parse_and_format_report_date_roundtrip():
    iso = pd_import._parse_report_date("01.07.2026")
    assert iso == "2026-07-01"
    assert pd_import.format_report_date(iso) == "01.07.2026"


def test_report_dates_found_sort_chronologically_not_lexicographically(tmp_path):
    # '01.07.2021' sorts AFTER '01.01.2026' lexicographically (DD.MM.YYYY),
    # but must come first chronologically -- this is exactly the bug the
    # ISO-storage choice in db.py's PD_TABLE_HISTORY_SCHEMA comment guards
    # against.
    rows = [
        ("01.07.2021", "russia_national", "all_except_structured", "BB", 3.0, 7.0, 11.0, False),
        ("01.01.2026", "russia_national", "all_except_structured", "ruBB", 4.0, 8.0, 12.0, True),
    ]
    xlsx = _make_workbook(tmp_path / "raexpert.xlsx", rows)
    con = _con(tmp_path)
    result = pd_import.import_pd_table(str(xlsx), con)
    assert result["report_dates_found"] == ["01.07.2021", "01.01.2026"]
