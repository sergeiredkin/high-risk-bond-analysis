from pathlib import Path

from hybond.rag.answer import answer_fact, route_fact_type
from hybond.rag.facts import extract_document_facts
from hybond.rag.ingest import ingest_file
from hybond.rag.reconcile import reconcile_document_facts
from hybond.store import db


def test_narrative_nonpayment_question_is_not_misrouted():
    assert route_fact_type("Какова причина неисполнения обязательства?") is None
    assert route_fact_type("Какова сумма неисполненного обязательства в рублях?") == "unpaid_amount_rub"


def test_numeric_path_requires_reconciled_fact(tmp_path):
    source = tmp_path / "notice.md"
    source.write_text("ISIN: RU000A10AHU1\n", encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("insert into issuer(issuer_id,name) values(?,?)", ("1234567890", "Test"))
    con.execute("insert into bond(secid,isin,issuer_id) values(?,?,?)",
                ("TEST", "RU000A10AHU1", "1234567890"))
    con.commit()
    doc = ingest_file(con, source, doc_type="notice", isin="RU000A10AHU1")
    extract_document_facts(con, doc["doc_sha256"])
    reconcile_document_facts(con, doc["doc_sha256"])
    answer = answer_fact(con, "какой ISIN?", isin="RU000A10AHU1")
    assert answer["status"] == "found"
    assert answer["value"] == "RU000A10AHU1"
    con.close()
