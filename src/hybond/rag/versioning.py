"""Index-version bookkeeping and rollback controls."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def config_hash(config: dict) -> str:
    """Return a stable hash for an index configuration."""
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def create_version(con, config: dict) -> int:
    """Create an inactive index version; callers activate after validation."""
    row = con.execute(
        "INSERT INTO rag_index_versions(created_at,config_hash,is_active) VALUES(?,?,0) RETURNING version",
        (datetime.now(UTC).isoformat(), config_hash(config)),
    ).fetchone()
    con.commit()
    return int(row[0])


def activate_version(con, version: int) -> None:
    """Atomically make an existing version active."""
    if not con.execute("SELECT 1 FROM rag_index_versions WHERE version=?", (version,)).fetchone():
        raise ValueError(f"unknown index version: {version}")
    con.execute("UPDATE rag_index_versions SET is_active=0")
    con.execute("UPDATE rag_index_versions SET is_active=1 WHERE version=?", (version,))
    con.commit()


def active_version(con) -> int | None:
    """Return the active index version, if one has been activated."""
    row = con.execute("SELECT version FROM rag_index_versions WHERE is_active=1").fetchone()
    return int(row[0]) if row else None


def rollback(con, version: int) -> None:
    """Activate a previously created version as the rollback target."""
    activate_version(con, version)
