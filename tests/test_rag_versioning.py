from pathlib import Path

import pytest

from hybond.rag.versioning import (
    activate_version,
    active_version,
    create_version,
    rollback,
)
from hybond.store import db


def test_index_versions_activate_and_rollback():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    first = create_version(con, {"retrieval": "fts5", "k": 5})
    second = create_version(con, {"retrieval": "hybrid", "k": 8})
    activate_version(con, first)
    assert active_version(con) == first
    rollback(con, second)
    assert active_version(con) == second
    with pytest.raises(ValueError):
        activate_version(con, 999)
    con.close()
