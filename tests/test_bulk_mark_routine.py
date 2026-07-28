"""Tests for scripts/bulk_mark_routine.py's issuer-scoped ROUTINE_RULES.

Regression coverage for the near-miss this replaced: a title-global rule
would have marked Селигдар's major-transaction filings as routine noise
just because Брусника's filings under the same title were routine SPV
financing. Rules must be checked per (issuer_id, title) pair.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond.scrape import edisclosure as ed  # noqa: E402
from hybond.store import db  # noqa: E402
from bulk_mark_routine import ROUTINE_RULES, main, matches_routine_rule  # noqa: E402

BRUSNIKA = "6685151087"
SELIGDAR = "some-seligdar-inn"
SHARED_TITLE = "Совершение подконтрольной эмитенту организацией существенной сделки"
GLOBAL_TITLE = "Выплаченные доходы по эмиссионным ценным бумагам эмитента"


def test_issuer_scoped_rule_applies_only_to_its_issuer():
    assert matches_routine_rule(BRUSNIKA, SHARED_TITLE)
    assert not matches_routine_rule(SELIGDAR, SHARED_TITLE)


def test_global_rule_applies_to_any_issuer():
    assert matches_routine_rule(BRUSNIKA, GLOBAL_TITLE)
    assert matches_routine_rule(SELIGDAR, GLOBAL_TITLE)


def test_unknown_title_never_matches():
    assert not matches_routine_rule(BRUSNIKA, "Незарегистрированное сообщение")


def test_no_rule_widens_shared_title_to_all_issuers():
    """Guards against re-adding the shared title as a (None, title) rule."""
    scoped = [issuer for issuer, title in ROUTINE_RULES if title == SHARED_TITLE]
    assert scoped == [BRUSNIKA]


def test_main_marks_brusnika_but_leaves_seligdar_in_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "routine.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.upsert_issuer(con, BRUSNIKA, "Брусника", None)
    db.upsert_issuer(con, SELIGDAR, "Селигдар", None)

    bru = ed.Filing.make(BRUSNIKA, BRUSNIKA, "2026-07-20", SHARED_TITLE, "u-bru")
    sel = ed.Filing.make(SELIGDAR, SELIGDAR, "2026-07-20", SHARED_TITLE, "u-sel")
    for issuer_id, f in ((BRUSNIKA, bru), (SELIGDAR, sel)):
        db.insert_filing(con, body_hash=f.body_hash, issuer_id=issuer_id,
                          edisclosure_id=issuer_id, published=f.published,
                          title=f.title, url=f.url, fetched_on="2026-07-27")
    con.commit()
    con.close()

    main()

    con = db.connect()
    remaining = db.unclassified_filings(con)
    assert [r["issuer_id"] for r in remaining] == [SELIGDAR]
    events = con.execute("SELECT issuer_id, category FROM event").fetchall()
    assert [(e["issuer_id"], e["category"]) for e in events] == [(BRUSNIKA, "routine")]
    con.close()
