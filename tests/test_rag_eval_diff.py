from hybond.rag.eval.diff import compare


def report(**metrics):
    return {"metrics": metrics}


def test_eval_diff_passes_small_changes_and_fails_large_drop():
    baseline = report(page_recall_at_k=0.8, mrr=0.7, citation_precision_at_k=0.8,
                      answerable_evidence_match_rate=0.7,
                      answer_abstention_accuracy=None,
                      unanswerable_retrieval_empty_rate=0.2)
    good = report(page_recall_at_k=0.79, mrr=0.7, citation_precision_at_k=0.8,
                  answerable_evidence_match_rate=0.7,
                  answer_abstention_accuracy=None,
                  unanswerable_retrieval_empty_rate=0.2)
    bad = report(page_recall_at_k=0.7, mrr=0.7, citation_precision_at_k=0.8,
                 answerable_evidence_match_rate=0.7,
                 answer_abstention_accuracy=None,
                 unanswerable_retrieval_empty_rate=0.2)
    assert compare(baseline, good)["passed"]
    result = compare(baseline, bad)
    assert not result["passed"]
    assert "page_recall_at_k" in result["regressions"]


def test_eval_diff_rejects_different_splits_or_corpus_hashes():
    baseline = report(page_recall_at_k=0.8)
    baseline.update({"split": "dev", "k": 5, "question_count": 10,
                     "dataset_sha256": {"golden": "a", "manifest": "b"}})
    candidate = report(page_recall_at_k=0.9)
    candidate.update({"split": "test", "k": 5, "question_count": 10,
                      "dataset_sha256": {"golden": "a", "manifest": "b"}})
    result = compare(baseline, candidate)
    assert not result["passed"]
    assert result["compatibility_errors"] == ["split"]
    candidate["split"] = "dev"
    candidate["dataset_sha256"]["golden"] = "different"
    result = compare(baseline, candidate)
    assert not result["passed"]
    assert result["compatibility_errors"] == ["dataset_sha256"]


def test_eval_diff_fails_when_reports_have_no_comparable_metrics():
    assert not compare(report(), report())["passed"]
