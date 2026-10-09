"""Date-bounded file selection for deliberate historical ingestion."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

_ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def file_date(path: Path) -> date | None:
    """Read an ISO date from a source path; do not guess from filesystem mtime."""
    match = _ISO_DATE.search(str(path))
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def select_date_range(paths: list[Path], *, after: str | None = None,
                      before: str | None = None) -> list[Path]:
    """Select files with path dates in the inclusive requested range, oldest first."""
    start = date.fromisoformat(after) if after else None
    end = date.fromisoformat(before) if before else None
    if start and end and start > end:
        raise ValueError("--since-date must be on or before --until-date")
    dated = []
    for path in paths:
        published = file_date(path)
        if published is None or (start and published < start) or (end and published > end):
            continue
        dated.append((published, str(path), path))
    return [item[2] for item in sorted(dated)]
