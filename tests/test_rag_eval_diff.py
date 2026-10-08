from hybond.rag.eval.diff import compare


def report(**metrics):
    return {"metrics": metrics}


def test_eval_diff_passes_small_changes_and_fails_large_drop():
    baseline = report(page_recall_at_k=0.8, mrr=0.7, citation_precision_at_k=0.8,
                      answerable_evidence_match_rate=0.7, abstention_accuracy=1.0)
    good = report(page_recall_at_k=0.79, mrr=0.7, citation_precision_at_k=0.8,
                  answerable_evidence_match_rate=0.7, abstention_accuracy=1.0)
    bad = report(page_recall_at_k=0.7, mrr=0.7, citation_precision_at_k=0.8,
                 answerable_evidence_match_rate=0.7, abstention_accuracy=1.0)
    assert compare(baseline, good)["passed"]
    result = compare(baseline, bad)
    assert not result["passed"]
    assert "page_recall_at_k" in result["regressions"]
