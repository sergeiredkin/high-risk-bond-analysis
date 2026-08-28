"""
Pipeline health logging and rotation.

Usage:
    from hybond.health import log_health, rotate_pipeline_health

    log_health(con, 'moex', 'ingest', 'error',
               'MOEX_EMPTY', 'No bonds returned for snap_date 2026-08-27')

    log_health(con, 'pipeline', 'compute', 'warn',
               'YTM_SANITY_FAIL',
               'ytm=69.69 rejected for АПРИ 2Р10',
               issuer_id='453326003')
"""
from __future__ import annotations

import sqlite3
import sys

from .store import db

VALID_SOURCES = {'moex', 'cbr', 'azipi', 'parser_api',
                  'google_news', 'pipeline', 'dashboard'}
VALID_STAGES = {'ingest', 'compute', 'store', 'export', 'maintenance'}
VALID_LEVELS = {'ok', 'warn', 'error', 'critical'}

# ordering for get_unresolved()'s min_level filter and summary()'s "worst" pick
_LEVEL_RANK = {'ok': 0, 'warn': 1, 'error': 2, 'critical': 3}


def log_health(con, source, stage, level, code,
               detail=None, issuer_id=None) -> int | None:
    """
    Write one health event. Returns inserted row id, or None if the write
    itself failed at the DB layer.
    Validates source/stage/level — raises ValueError on bad input.
    Never raises on DB error (logs to stderr instead) so callers
    are never interrupted by the logging layer itself.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}")
    if stage not in VALID_STAGES:
        raise ValueError(f"stage must be one of {sorted(VALID_STAGES)}, got {stage!r}")
    if level not in VALID_LEVELS:
        raise ValueError(f"level must be one of {sorted(VALID_LEVELS)}, got {level!r}")

    try:
        db.init_pipeline_health(con)
        cur = con.execute(
            "INSERT INTO pipeline_health(source,stage,level,code,detail,issuer_id) "
            "VALUES(?,?,?,?,?,?)",
            (source, stage, level, code, detail, issuer_id),
        )
        con.commit()
        return cur.lastrowid
    except sqlite3.Error as e:
        print(f"hybond.health.log_health: DB error, event not recorded: {e}",
              file=sys.stderr)
        return None


def log_health_once_today(con, source, stage, level, code,
                           detail=None, issuer_id=None) -> int | None:
    """
    Like log_health() but skips if an unresolved entry with the same
    code already exists from today. Returns row id or None if skipped.
    """
    db.init_pipeline_health(con)
    existing = con.execute(
        "SELECT id FROM pipeline_health WHERE code = ? AND resolved = 0 "
        "AND date(recorded_at) = date('now') LIMIT 1",
        (code,),
    ).fetchone()
    if existing:
        return None
    return log_health(con, source, stage, level, code, detail, issuer_id)


def get_unresolved(con, min_level='warn', limit=50) -> list[dict]:
    """
    Return unresolved entries at or above min_level,
    newest first. Levels ordered: ok < warn < error < critical.
    """
    if min_level not in VALID_LEVELS:
        raise ValueError(f"min_level must be one of {sorted(VALID_LEVELS)}, got {min_level!r}")
    db.init_pipeline_health(con)
    levels = [lvl for lvl, rank in _LEVEL_RANK.items() if rank >= _LEVEL_RANK[min_level]]
    placeholders = ",".join("?" * len(levels))
    rows = con.execute(
        f"SELECT * FROM pipeline_health WHERE resolved=0 AND level IN ({placeholders}) "
        "ORDER BY recorded_at DESC LIMIT ?",
        (*levels, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve(con, health_id: int, note: str = '') -> None:
    """Mark a single entry resolved with optional note."""
    db.init_pipeline_health(con)
    con.execute(
        "UPDATE pipeline_health SET resolved=1, resolved_at=datetime('now'), "
        "resolved_note=? WHERE id=?",
        (note, health_id),
    )
    con.commit()


def resolve_code(con, code: str, note: str = '') -> int:
    """
    Mark all unresolved entries with this code as resolved.
    Returns count resolved. Useful for bulk-clearing a class of
    recurring noise once the root cause is fixed.
    """
    db.init_pipeline_health(con)
    cur = con.execute(
        "UPDATE pipeline_health SET resolved=1, resolved_at=datetime('now'), "
        "resolved_note=? WHERE code=? AND resolved=0",
        (note, code),
    )
    con.commit()
    return cur.rowcount


def rotate_pipeline_health(con, days: int = 7) -> dict:
    """
    Delete ok/warn rows older than `days` days,
    and resolved error/critical rows older than `days` days.
    NEVER delete unresolved error/critical rows.

    After deletion, writes a LOG_ROTATED 'ok' entry recording:
    - rows_deleted count
    - unresolved_errors_preserved count
    - rotation timestamp

    Returns dict: {rows_deleted, errors_preserved, rotation_id}
    """
    db.init_pipeline_health(con)
    cutoff = f"-{days} days"

    cur = con.execute(
        "DELETE FROM pipeline_health WHERE level IN ('ok','warn') "
        "AND recorded_at < datetime('now', ?)",
        (cutoff,),
    )
    rows_deleted = cur.rowcount

    cur = con.execute(
        "DELETE FROM pipeline_health WHERE level IN ('error','critical') "
        "AND resolved=1 AND recorded_at < datetime('now', ?)",
        (cutoff,),
    )
    rows_deleted += cur.rowcount

    errors_preserved = con.execute(
        "SELECT COUNT(*) AS n FROM pipeline_health "
        "WHERE level IN ('error','critical') AND resolved=0",
    ).fetchone()["n"]

    con.commit()

    rotation_id = log_health(
        con, 'pipeline', 'maintenance', 'ok', 'LOG_ROTATED',
        f"rows_deleted={rows_deleted} unresolved_errors_preserved={errors_preserved} "
        f"days={days}",
    )

    return {
        "rows_deleted": rows_deleted,
        "errors_preserved": errors_preserved,
        "rotation_id": rotation_id,
    }


def summary(con) -> dict:
    """
    Return health summary for dashboard export:
    {
        'status': 'ok' | 'warn' | 'error' | 'critical',
        'unresolved_counts': {'warn': N, 'error': N, 'critical': N},
        'latest_error': {code, detail, recorded_at} | None,
        'last_rotation': recorded_at | None
    }
    status = worst unresolved level, or 'ok' if none.
    """
    db.init_pipeline_health(con)

    unresolved_counts = {}
    for lvl in ('warn', 'error', 'critical'):
        n = con.execute(
            "SELECT COUNT(*) AS n FROM pipeline_health WHERE level=? AND resolved=0",
            (lvl,),
        ).fetchone()["n"]
        unresolved_counts[lvl] = n

    status = 'ok'
    for lvl in ('warn', 'error', 'critical'):
        if unresolved_counts[lvl] > 0:
            status = lvl

    latest_error_row = con.execute(
        "SELECT code, detail, recorded_at FROM pipeline_health "
        "WHERE level IN ('error','critical') AND resolved=0 "
        "ORDER BY recorded_at DESC LIMIT 1",
    ).fetchone()
    latest_error = dict(latest_error_row) if latest_error_row else None

    last_rotation_row = con.execute(
        "SELECT recorded_at FROM pipeline_health WHERE code='LOG_ROTATED' "
        "ORDER BY recorded_at DESC LIMIT 1",
    ).fetchone()
    last_rotation = last_rotation_row["recorded_at"] if last_rotation_row else None

    return {
        "status": status,
        "unresolved_counts": unresolved_counts,
        "latest_error": latest_error,
        "last_rotation": last_rotation,
    }
