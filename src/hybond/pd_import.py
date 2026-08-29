"""Import Эксперт РА's empirical PD (probability of default) disclosure into
pd_table_history, and serve get_pd() lookups against it.

Source: raexpert_combined.xlsx, sheet "Aggregate_Freq_Raw" -- one row per
(report_date, scale_type, obj_type, category) with 1/2/3-year observed
default frequencies as percentages. This module imports the subset relevant
to hybond's Russian corporate ВДО universe: obj_type='all_except_structured'
(excludes structured/securitised bonds, which this dashboard doesn't cover)
AND scale_type='russia_national' (the sheet also carries a 'belarus' scale
whose category names collide with the Russian ones -- e.g. both have an
'AAA' row for the same report_date/obj_type -- and pd_table_history's
UNIQUE(report_date, rating, obj_type) has no scale_type column to keep them
apart; importing both would let whichever row happens to load second
silently win. Not requested by the task that introduced this table, but
required for the import to be correct rather than nondeterministic).

get_pd() falls back to the hardcoded DEFAULT_PROBS table (compute/
default_probs.py, the same Эксперт РА figures already used for the
mispricing signal) when the DB has no data for a rating -- so nothing
depends on this import ever having been run.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .compute import default_probs
from .store import db

SHEET_NAME = "Aggregate_Freq_Raw"
OBJ_TYPE = "all_except_structured"
SCALE_TYPE = "russia_national"

EXPECTED_HEADER = [
    "report_date", "scale_type", "obj_type", "category",
    "freq_1y", "freq_2y", "freq_3y", "is_detailed",
]

# Detailed (ru-scale) rating -> broad letter category. Includes the plain
# letter categories mapping to themselves so a 'category'-format row (the
# sheet's own pre-2026 rows, before Эксперт РА started publishing ru-scale
# detail) can use the same dict for its mapped_category.
RATING_TO_CATEGORY = {
    'ruAAA': 'AAA', 'AAA': 'AAA',
    'ruAA+': 'AA', 'ruAA': 'AA', 'ruAA-': 'AA', 'AA': 'AA',
    'ruA+': 'A', 'ruA': 'A', 'ruA-': 'A', 'A': 'A',
    'ruBBB+': 'BBB', 'ruBBB': 'BBB', 'ruBBB-': 'BBB', 'BBB': 'BBB',
    'ruBB+': 'BB', 'ruBB': 'BB', 'ruBB-': 'BB', 'BB': 'BB',
    'ruB+': 'B', 'ruB': 'B', 'ruB-': 'B', 'B': 'B',
    'ruCCC': 'CCC', 'CCC': 'CCC',
    'ruCC': 'CC', 'ruC': 'CC', 'CC': 'CC',
}

_HORIZON_COL = {1: "freq_1y", 2: "freq_2y", 3: "freq_3y"}


def _parse_report_date(raw) -> str:
    """'01.07.2026' (sheet display format) -> '2026-07-01' (ISO, sortable).
    Accepts a datetime/date too (openpyxl returns one if the cell is
    formatted as a date rather than plain text)."""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if hasattr(raw, "isoformat") and not isinstance(raw, str):
        return raw.isoformat()
    return date(*reversed([int(p) for p in str(raw).strip().split(".")])).isoformat()


def format_report_date(iso: str) -> str:
    """'2026-07-01' -> '01.07.2026', for CLI/dashboard display."""
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def import_pd_table(xlsx_path: str, con) -> dict:
    """Import the Aggregate_Freq_Raw sheet into pd_table_history.

    Idempotent: re-running against the same file (or a file with overlapping
    report dates) leaves already-imported rows untouched via INSERT OR
    IGNORE against the (report_date, rating, obj_type) unique key.

    Returns: {rows_imported, rows_skipped, report_dates_found} -- dates in
    display (DD.MM.YYYY) format, oldest first. rows_skipped counts BOTH rows
    with no usable frequency data and rows that already existed (same
    unique key) -- the two aren't distinguished in this return value since
    the task's own CLI output only ever surfaces one "skipped" figure.

    Raises: FileNotFoundError if xlsx_path doesn't exist; ValueError if the
    Aggregate_Freq_Raw sheet is missing or its header doesn't match the
    expected columns.
    """
    import openpyxl  # lazy: only this function needs it, not get_pd()/import

    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"PD table source not found: {xlsx_path}")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise ValueError(
            f"Sheet {SHEET_NAME!r} not found in {xlsx_path} "
            f"(sheets present: {wb.sheetnames})")
    ws = wb[SHEET_NAME]
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        raise ValueError(f"Sheet {SHEET_NAME!r} in {xlsx_path} is empty")
    header = [str(h).strip() if h is not None else h for h in header]
    if header != EXPECTED_HEADER:
        raise ValueError(
            f"Sheet {SHEET_NAME!r} has unexpected columns {header}, "
            f"expected {EXPECTED_HEADER}")
    col = {name: i for i, name in enumerate(header)}

    db.init_pd_table_history(con)

    report_dates_iso: set[str] = set()
    rows_imported = 0
    rows_skipped = 0
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        if row[col["obj_type"]] != OBJ_TYPE or row[col["scale_type"]] != SCALE_TYPE:
            continue
        f1, f2, f3 = row[col["freq_1y"]], row[col["freq_2y"]], row[col["freq_3y"]]
        if f1 is None and f2 is None and f3 is None:
            rows_skipped += 1
            continue

        report_date_iso = _parse_report_date(row[col["report_date"]])
        report_dates_iso.add(report_date_iso)
        rating = str(row[col["category"]]).strip()
        rating_format = "detailed" if rating.lower().startswith("ru") else "category"
        mapped_category = RATING_TO_CATEGORY.get(rating)

        cur = con.execute(
            "INSERT OR IGNORE INTO pd_table_history "
            "(report_date, rating, rating_format, mapped_category, "
            " freq_1y, freq_2y, freq_3y, obj_type, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raexpert')",
            (report_date_iso, rating, rating_format, mapped_category,
             None if f1 is None else f1 / 100.0,
             None if f2 is None else f2 / 100.0,
             None if f3 is None else f3 / 100.0,
             OBJ_TYPE))
        if cur.rowcount:
            rows_imported += 1
        else:
            rows_skipped += 1

    con.commit()
    return {
        "rows_imported": rows_imported,
        "rows_skipped": rows_skipped,
        "report_dates_found": [format_report_date(d) for d in sorted(report_dates_iso)],
    }


def get_pd(rating: str, horizon: int = 1, con=None) -> float:
    """Latest empirical PD for `rating` at the given horizon (1/2/3 years),
    as a decimal (0.0-1.0).

    Tries, in order:
      1. An exact `rating` match in pd_table_history, most recent
         report_date (the DB stores the rating string exactly as Эксперт РА
         published it -- 'ruBB-' for a detailed-scale report, plain 'BB'
         for an older category-scale one).
      2. RATING_TO_CATEGORY's broad-category fallback (e.g. 'ruBB+' ->
         'BB'), most recent report_date among rows in that category --
         covers a rating whose own exact tier wasn't published in the
         latest report.
      3. The hardcoded DEFAULT_PROBS table (compute/default_probs.py) --
         used whenever con is None, the table is empty/missing, or the
         rating (and its category) aren't found in it either.

    Never raises for an unrecognised rating -- returns 0.0 as the final,
    last-resort fallback (matches DEFAULT_PROBS's own treatment of a
    top-tier/no-observed-defaults rating, e.g. ruAAA=0.0), so a caller doing
    a straight probability-weighted calculation never has to null-check.
    """
    col = _HORIZON_COL.get(horizon, "freq_1y")
    if con is not None and rating:
        db.init_pd_table_history(con)
        row = con.execute(
            f"SELECT {col} AS v FROM pd_table_history "
            f"WHERE rating = ? AND obj_type = ? AND {col} IS NOT NULL "
            "ORDER BY report_date DESC LIMIT 1",
            (rating, OBJ_TYPE)).fetchone()
        if row is not None and row["v"] is not None:
            return float(row["v"])

        category = RATING_TO_CATEGORY.get(rating) or RATING_TO_CATEGORY.get(
            default_probs._normalise(rating))
        if category:
            row = con.execute(
                f"SELECT {col} AS v FROM pd_table_history "
                f"WHERE mapped_category = ? AND obj_type = ? AND {col} IS NOT NULL "
                "ORDER BY report_date DESC LIMIT 1",
                (category, OBJ_TYPE)).fetchone()
            if row is not None and row["v"] is not None:
                return float(row["v"])

    probs = default_probs.lookup(rating) if rating else None
    if probs is not None:
        key = default_probs._HORIZON_KEYS.get(horizon, "yr1")
        return probs[key]
    return 0.0
