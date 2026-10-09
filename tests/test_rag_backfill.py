from pathlib import Path

import pytest

from hybond.rag.backfill import file_date, select_date_range


def test_date_range_selects_iso_dated_paths_oldest_first(tmp_path: Path):
    root = tmp_path / "edisclosure"
    paths = [root / "2026-07-31" / "msg_b.html",
             root / "2026-07-01" / "msg_a.html",
             root / "2026-08-01" / "msg_c.html",
             root / "unknown" / "msg_d.html"]
    assert file_date(paths[0]) is not None
    assert select_date_range(paths, after="2026-07-01", before="2026-07-31") == [
        paths[1], paths[0]
    ]
    assert select_date_range(paths, after="2026-07-15") == [paths[0], paths[2]]


def test_file_date_uses_nearest_date_directory_not_ancestor(tmp_path: Path):
    path = tmp_path / "2026-07-01" / "archive" / "2025-12-01" / "msg.html"
    assert file_date(path).isoformat() == "2025-12-01"
    assert select_date_range([path], after="2026-07-01", before="2026-07-31") == []


def test_date_range_rejects_invalid_or_reversed_dates(tmp_path: Path):
    with pytest.raises(ValueError):
        select_date_range([], after="2026-08-01", before="2026-07-01")
    with pytest.raises(ValueError):
        select_date_range([], after="not-a-date")
