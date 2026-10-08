from pathlib import Path

import pytest

from hybond.rag.report import coverage_report, log_feedback, log_query
from hybond.store import db


def test_feedback_rejects_unknown_query():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    with pytest.raises(ValueError):
        log_feedback(con, query_id=999, label="useful")
    con.close()


def test_coverage_report_tracks_observed_evidence():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    log_query(con, question="test", route="retrieval", hits=[], abstained=True)
    report = coverage_report(con)
    assert report["queries_logged"] == 1
    assert report["evidence_rate"] == 0.0
    con.close()
