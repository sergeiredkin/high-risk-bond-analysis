import json
from pathlib import Path

from hybond.rag.eval.metrics import citation_precision
from hybond.rag.eval.runner import run
from hybond.rag.ingest import ingest_file
from hybond.store import db


def test_citation_precision_counts_only_relevant_retrieved_pages():
    from hybond.rag.search import SearchHit
    hits = [SearchHit("a", "doc-a", 1, "", "a", 1),
            SearchHit("b", "doc-b", 1, "", "b", 1)]
    assert citation_precision(hits, ({"doc_sha256": "doc-a", "page": 1},)) == 0.5


def test_eval_reports_page_recall_and_abstention(tmp_path):
    source = tmp_path / "doc.md"
    source.write_text("# Купон\n\nДата выплаты 15.03.2027.", encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    indexed = ingest_file(con, source, doc_type="report", isin="RU000TEST", issuer_inn="1234567890")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({
        "doc_sha256": indexed["doc_sha256"], "isin": "RU000TEST",
        "issuer_inn": "1234567890", "doc_type": "report",
        "source_url": "file:///doc.md", "fetched_at": "2026-10-07",
        "local_path": str(source),
    }) + "\n", encoding="utf-8")
    golden = tmp_path / "golden.jsonl"
    golden.write_text("\n".join(json.dumps(row) for row in [
        {"qid": "Q1", "split": "dev", "question": "дата выплаты",
         "category": "coupon", "answer_type": "date", "expected": "2027-03-15",
         "evidence": [{"doc_sha256": indexed["doc_sha256"], "page": 1}], "requires": []},
        {"qid": "Q2", "split": "dev", "question": "несуществующий ковенант",
         "category": "covenant", "answer_type": "none", "expected": None,
         "evidence": [], "requires": []},
    ]) + "\n", encoding="utf-8")

    report = run(con, golden, manifest, k=3)
    assert report["question_count"] == 2
    assert report["metrics"]["page_recall_at_k"] == 0.5
    assert report["metrics"]["abstention_accuracy"] == 1.0
    con.close()
