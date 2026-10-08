import json
from pathlib import Path

from hybond.rag.facts import extract_document_facts, query_facts
from hybond.rag.ingest import ingest_file
from hybond.store import db


def test_rule_facts_keep_quotes_and_are_idempotent(tmp_path):
    source = tmp_path / "notice.md"
    source.write_text(
        "ИНН: 4012004991\nISIN: RU000A10AHU1\n"
        "Размер обязательства: 67 800 000,00 руб.", encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    doc = ingest_file(con, source, doc_type="notice")
    first = extract_document_facts(con, doc["doc_sha256"])
    second = extract_document_facts(con, doc["doc_sha256"])

    assert len(first) == len(second) == 3
    amount = query_facts(con, "unpaid_amount_rub", doc_sha256=doc["doc_sha256"])
    assert json.loads(amount[0]["value_json"]) == 67800000.0
    assert "67 800 000" in amount[0]["quote"]
    assert con.execute("select count(*) from rag_facts").fetchone()[0] == 3
    con.close()
