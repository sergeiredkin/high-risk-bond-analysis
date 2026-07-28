"""Detector grader (stub). Run this in weeks 4-6, once each issuer has ~20
snapshots, to compare how the three detectors actually performed on your real
watchlist. No LLM.

The honest framing (see PLAN.md): with 15-25 issuers and rare change, you will
have few real events to grade against, so this is a DIRECTIONAL read, not a
precision statistic. What it reliably shows early: whether `fixed` is
over-flagging the volatile names relative to `hybrid`. What it cannot yet give:
a clean precision/recall number.

Grading is semi-manual: you mark whether a flag was followed by something that
mattered (a filing, a further sustained spread move, a genuine credit event).
This fills flag_outcome, which is the signal-scoring feedback loop that makes the
platform's predictions improve over cycles.
"""
from __future__ import annotations

from datetime import date

from .store import db


def summarise(con) -> dict:
    """Per-detector counts: total flagged, graded true, graded false, ungraded.
    Precision is reported only when a detector has any graded flags, and always
    with its denominator so you can see how thin it is."""
    rows = con.execute(
        "SELECT f.detector, "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN o.graded_true=1 THEN 1 ELSE 0 END) AS tp, "
        "SUM(CASE WHEN o.graded_true=0 THEN 1 ELSE 0 END) AS fp, "
        "SUM(CASE WHEN o.graded_true IS NULL THEN 1 ELSE 0 END) AS ungraded "
        "FROM flag_log f LEFT JOIN flag_outcome o ON f.flag_id=o.flag_id "
        "GROUP BY f.detector"
    ).fetchall()
    out = {}
    for r in rows:
        graded = r["tp"] + r["fp"]
        out[r["detector"]] = {
            "total": r["total"],
            "true_positives": r["tp"],
            "false_positives": r["fp"],
            "ungraded": r["ungraded"],
            "precision": (round(r["tp"] / graded, 2) if graded else None),
            "precision_denominator": graded,
        }
    return out


def grade_flag(con, flag_id: int, followed_by: str, is_true: bool) -> None:
    """Record the outcome of one flag. `is_true`: did something that mattered
    actually follow?"""
    con.execute(
        "INSERT OR REPLACE INTO flag_outcome(flag_id,followed_by,graded_true,graded_on) "
        "VALUES(?,?,?,?)",
        (flag_id, followed_by, 1 if is_true else 0, date.today().isoformat()),
    )
    con.commit()


def ungraded_flags(con, detector: str | None = None) -> list:
    """The queue of flags still needing a human verdict."""
    return db.flags_for_grading(con, detector)
