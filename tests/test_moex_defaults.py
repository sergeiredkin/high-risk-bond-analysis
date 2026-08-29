"""Tests for hybond.moex_defaults: import_moex_defaults() (emidocs-default
xlsx -> moex_default), get_watchlist_defaults(), get_defaults_for_issuer().
No network -- builds a small synthetic workbook rather than depending on
the real 20260817-emidocs-default.xlsx."""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import moex_defaults  # noqa: E402
from hybond.store import db  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")

HEADER = ["Краткое наименование эмитента", "ИНН", "ISIN",
          "Вид обязательства", "Состояние", "Плановая дата"]

ISSUER = "9731004688"  # in the issuer table below, matches one xlsx row


def _make_workbook(path, rows, sheet_name="emidocs-default"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


BASE_ROWS = [
    ("ГК Самолёт", ISSUER, "RU000A10CZA1", "Купон", "Технический дефолт",
     datetime(2026, 3, 15)),
    ("ГК Самолёт", ISSUER, "RU000A10BW96", "Купон", "Дефолт",
     datetime(2026, 4, 2)),
    ("Не в вотчлисте", "0000000000", "RU000AOTHER1", "Погашение", "Дефолт",
     datetime(2025, 11, 20)),
]


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.add_issuer_at_tier(con, ISSUER, "ГК Самолёт", watchlist_tier=1)
    con.commit()
    return con


# --- import_moex_defaults -------------------------------------------------

def test_import_happy_path(tmp_path):
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", BASE_ROWS)
    con = _con(tmp_path)

    result = moex_defaults.import_moex_defaults(str(xlsx), con)

    assert result["rows_imported"] == 3
    assert result["rows_skipped"] == 0
    rows = con.execute("SELECT * FROM moex_default ORDER BY planned_date").fetchall()
    assert len(rows) == 3
    assert rows[0]["planned_date"] == "2025-11-20"  # ISO, not the datetime repr
    assert rows[0]["inn"] == "0000000000"


def test_import_idempotent_on_reimport(tmp_path):
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    moex_defaults.import_moex_defaults(str(xlsx), con)

    result2 = moex_defaults.import_moex_defaults(str(xlsx), con)

    assert result2["rows_imported"] == 0
    assert result2["rows_skipped"] == 3
    total = con.execute("SELECT COUNT(*) AS n FROM moex_default").fetchone()["n"]
    assert total == 3  # no duplicates


def test_import_missing_sheet_raises_value_error(tmp_path):
    xlsx = tmp_path / "wrong_sheet.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "some_other_sheet"
    wb.save(xlsx)
    con = _con(tmp_path)

    with pytest.raises(ValueError):
        moex_defaults.import_moex_defaults(str(xlsx), con)


def test_import_missing_file_raises_file_not_found(tmp_path):
    con = _con(tmp_path)
    with pytest.raises(FileNotFoundError):
        moex_defaults.import_moex_defaults(str(tmp_path / "does_not_exist.xlsx"), con)


def test_import_skips_row_with_no_isin_and_no_date(tmp_path):
    rows = BASE_ROWS + [("Пустая строка", "1111111111", None, "Купон", "Дефолт", None)]
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", rows)
    con = _con(tmp_path)

    result = moex_defaults.import_moex_defaults(str(xlsx), con)

    assert result["rows_imported"] == 3
    assert result["rows_skipped"] == 1


def test_import_returns_watchlist_matches(tmp_path):
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", BASE_ROWS)
    con = _con(tmp_path)

    result = moex_defaults.import_moex_defaults(str(xlsx), con)

    matches = result["watchlist_matches"]
    assert len(matches) == 2  # both ГК Самолёт rows; the 0000000000 row is not in issuer table
    assert all(m["inn"] == ISSUER for m in matches)


# --- get_watchlist_defaults ------------------------------------------------

def test_get_watchlist_defaults_excludes_non_watchlist_issuers(tmp_path):
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    moex_defaults.import_moex_defaults(str(xlsx), con)

    matches = moex_defaults.get_watchlist_defaults(con)

    assert len(matches) == 2
    assert all(m["inn"] == ISSUER for m in matches)
    assert not any(m["inn"] == "0000000000" for m in matches)


def test_get_watchlist_defaults_empty_when_no_overlap(tmp_path):
    rows = [("Не в вотчлисте", "0000000000", "RU000AOTHER1", "Погашение", "Дефолт",
              datetime(2025, 11, 20))]
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", rows)
    con = _con(tmp_path)
    moex_defaults.import_moex_defaults(str(xlsx), con)

    assert moex_defaults.get_watchlist_defaults(con) == []


def test_get_watchlist_defaults_before_any_import_is_empty(tmp_path):
    con = _con(tmp_path)
    assert moex_defaults.get_watchlist_defaults(con) == []


# --- get_defaults_for_issuer ------------------------------------------------

def test_get_defaults_for_issuer_returns_only_that_issuer(tmp_path):
    xlsx = _make_workbook(tmp_path / "defaults.xlsx", BASE_ROWS)
    con = _con(tmp_path)
    moex_defaults.import_moex_defaults(str(xlsx), con)

    rows = moex_defaults.get_defaults_for_issuer(con, ISSUER)

    assert len(rows) == 2
    assert rows[0]["planned_date"] == "2026-03-15"  # ordered oldest first
    assert rows[1]["planned_date"] == "2026-04-02"


def test_get_defaults_for_issuer_no_match_is_empty(tmp_path):
    con = _con(tmp_path)
    assert moex_defaults.get_defaults_for_issuer(con, "9999999999") == []
