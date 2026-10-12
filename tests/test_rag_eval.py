import json
from pathlib import Path

from hybond.rag.eval.metrics import citation_precision, expected_in_hits, page_recall
from hybond.rag.eval.runner import run
from hybond.rag.ingest import ingest_file
from hybond.store import db


def test_numeric_and_boolean_expected_values_match_russian_evidence():
    from hybond.rag.search import SearchHit
    hit = SearchHit("c", "doc", 1, "", "Сумма 67 800 000,00 руб. Эмитент не исполнил обязанность.", 1)
    assert expected_in_hits({"value": 67800000, "unit": "RUB"}, "numeric", [hit]) is True
    spelled_amount = SearchHit(
        "spelled", "doc", 1, "",
        "Сумма 14 606 000 (четырнадцать миллионов шестьсот шесть тысяч) рублей.", 1,
    )
    assert expected_in_hits({"value": 14606000, "unit": "RUB"}, "numeric", [spelled_amount]) is True
    assert expected_in_hits(67800000, "numeric", [hit]) is False
    percent = SearchHit("p", "doc", 1, "", "Коэффициент 100%.", 1)
    rubles = SearchHit("r", "doc", 1, "", "Сумма 100 руб.", 1)
    assert expected_in_hits(100, "numeric", [percent]) is False
    assert expected_in_hits({"value": 100, "unit": "percent"}, "numeric", [percent]) is True
    assert expected_in_hits({"value": 100, "unit": "RUB"}, "numeric", [rubles]) is True
    yuan = SearchHit("y", "doc", 1, "", "Выплата составила 0,98 юаней на облигацию.", 1)
    count = SearchHit("n", "doc", 1, "", "Погашено 3 314 597 штук облигаций.", 1)
    assert expected_in_hits({"value": 0.98, "unit": "CNY"}, "numeric", [yuan]) is True
    assert expected_in_hits({"value": 3314597, "unit": "COUNT"}, "numeric", [count]) is True
    assert expected_in_hits({"value": 0.98, "unit": "RUB"}, "numeric", [yuan]) is False
    assert expected_in_hits(True, "boolean", [hit]) is None
    insufficient = SearchHit("d", "doc", 1, "", "Смотреть только на букву рейтинга недостаточно.", 1)
    assert expected_in_hits(False, "boolean", [insufficient]) is None
    contradictory = SearchHit("e", "doc", 1, "", "Неисполнение обязанности исключено.", 1)
    assert expected_in_hits(True, "boolean", [contradictory]) is None
    fulfilled = SearchHit("f", "doc", 1, "", "Эмитент исполнил обязанность и выплатил купон.", 1)
    assert expected_in_hits(False, "boolean", [fulfilled]) is None
    ambiguous = SearchHit("g", "doc", 1, "", "Нет данных о невыплате.", 1)
    assert expected_in_hits(False, "boolean", [ambiguous]) is None


def test_page_recall_requires_all_pages_for_cross_document_evidence():
    from hybond.rag.search import SearchHit
    evidence = ({"doc_sha256": "doc-a", "page": 1},
                {"doc_sha256": "doc-b", "page": 1})
    one = SearchHit("a", "doc-a", 1, "", "a", 1)
    two = SearchHit("b", "doc-b", 1, "", "b", 1)
    assert page_recall([one], evidence) == 0.5
    assert page_recall([one, two], evidence) == 1.0


def test_citation_precision_counts_only_relevant_retrieved_pages():
    from hybond.rag.search import SearchHit
    hits = [SearchHit("a", "doc-a", 1, "", "a", 1),
            SearchHit("b", "doc-b", 1, "", "b", 1)]
    assert citation_precision(hits, ({"doc_sha256": "doc-a", "page": 1},)) == 0.5


def test_eval_reports_retrieval_empty_rate_without_claiming_answer_abstention(tmp_path):
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
        {"qid": "Q3", "split": "test", "question": "замороженный тестовый вопрос",
         "category": "covenant", "answer_type": "none", "expected": None,
         "evidence": [], "requires": []},
    ]) + "\n", encoding="utf-8")

    report = run(con, golden, manifest, k=3)
    assert report["question_count"] == 2
    assert report["split"] == "dev"
    test_report = run(con, golden, manifest, k=3, split="test")
    assert test_report["question_count"] == 1
    assert test_report["split"] == "test"
    all_report = run(con, golden, manifest, k=3, split="all")
    assert all_report["question_count"] == 3
    assert report["retrieval_mode"] == "fts5"
    assert report["metrics"]["page_recall_at_k"] == 0.5
    assert report["metrics"]["unanswerable_question_count"] == 1
    assert report["metrics"]["unanswerable_retrieval_empty_rate"] == 1.0
    assert report["metrics"]["answer_abstention_accuracy"] is None
    unanswerable = next(row for row in report["results"] if row["answer_type"] == "none")
    assert unanswerable["retrieval_empty"] is True
    assert unanswerable["answer_abstention_correct"] is None

    class DenseFixture:
        def search(self, _question, limit=5, **_filters):
            from hybond.rag.search import SearchHit
            row = con.execute(
                "SELECT chunk_id,doc_sha256,page_no,section,text FROM rag_chunks LIMIT 1"
            ).fetchone()
            return [SearchHit(row[0], row[1], row[2], row[3] or "", row[4], 1.0)][:limit]

    hybrid_report = run(con, golden, manifest, k=3, dense_index=DenseFixture())
    assert hybrid_report["retrieval_mode"] == "hybrid_rrf"
    assert hybrid_report["baseline_metrics"] == report["metrics"]
    assert "citation_precision_at_k" in hybrid_report["metrics"]
    con.close()
