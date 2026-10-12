"""Run reproducible offline retrieval evaluations."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..golden import GoldenQuestion, load_golden
from ..manifest import load_manifest
from ..search import hybrid_search, search
from .metrics import citation_precision, expected_in_hits, page_recall, reciprocal_rank


def _result(question: GoldenQuestion, hits: list, k: int) -> dict:
    """Score one golden question against retrieved evidence."""
    top = hits[:k]
    retrieval_empty = not bool(top)
    answer_check = expected_in_hits(question.expected, question.answer_type, top)
    return {
        "qid": question.qid,
        "category": question.category,
        "answer_type": question.answer_type,
        "hits": len(top),
        "page_recall": page_recall(top, question.evidence),
        "mrr": reciprocal_rank(top, question.evidence),
        "citation_precision": citation_precision(top, question.evidence),
        "answer_evidence_match": answer_check,
        "retrieval_empty": retrieval_empty,
        # This runner grades retrieval, not a generated answer or its abstention.
        "answer_abstention_correct": None,
        "citations": [
            {"doc_sha256": hit.doc_sha256, "page": hit.page_no}
            for hit in top
        ],
    }


def _aggregate(results: list[dict]) -> dict:
    """Aggregate retrieval metrics for one ranked result set."""
    answerable = [r for r in results if r["answer_type"] != "none"]
    scored_answers = [r for r in answerable if r["answer_evidence_match"] is not None]
    unanswerable = [r for r in results if r["answer_type"] == "none"]
    manual_review = sum(r["answer_evidence_match"] is None for r in answerable)
    return {
        "page_recall_at_k": sum(r["page_recall"] for r in results) / len(results) if results else 0.0,
        "citation_precision_at_k": sum(r["citation_precision"] for r in results) / len(results) if results else 0.0,
        "mrr": sum(r["mrr"] for r in results) / len(results) if results else 0.0,
        "answerable_evidence_match_rate": (
            sum(r["answer_evidence_match"] is True for r in scored_answers) / len(scored_answers)
            if scored_answers else None
        ),
        "unanswerable_question_count": len(unanswerable),
        "unanswerable_retrieval_empty_rate": (
            sum(r["retrieval_empty"] for r in unanswerable) / len(unanswerable)
            if unanswerable else None
        ),
        "answer_abstention_accuracy": None,
        "manual_review_answers": manual_review,
        "manual_review_text_answers": manual_review,
    }


def run(con, golden_path: Path, manifest_path: Path, *, k: int = 5,
        split: str = "dev", run_id: str | None = None, dense_index=None) -> dict:
    """Evaluate retrieval against a validated golden set and return JSON data.

    The runner deliberately evaluates retrieval and evidence grounding, not
    subjective narrative quality. Text answers remain marked for manual review.
    """
    if split not in {"dev", "test", "all"}:
        raise ValueError("split must be 'dev', 'test', or 'all'")
    documents = load_manifest(manifest_path)
    questions = load_golden(golden_path, documents)
    if split != "all":
        questions = [question for question in questions if question.split == split]
    if not questions:
        raise ValueError(f"golden set has no questions for split {split!r}")
    results = []
    baseline_results = []
    for question in questions:
        issuer_inn = getattr(question, "issuer_inn", None)
        isin = getattr(question, "isin", None)
        sparse = search(con, question.question, limit=k, issuer_inn=issuer_inn, isin=isin)
        baseline_results.append(_result(question, sparse, k))
        if dense_index is not None:
            candidate_depth = max(k * 8, 32)
            dense = dense_index.search(question.question, limit=candidate_depth,
                                       issuer_inn=issuer_inn, isin=isin)
            hits = hybrid_search(con, question.question, limit=k, dense_hits=dense,
                                 sparse_limit=candidate_depth,
                                 issuer_inn=issuer_inn, isin=isin)
        else:
            hits = sparse
        results.append(_result(question, hits, k))
    return {
        "run_id": run_id or f"eval-{uuid.uuid4().hex[:12]}",
        "created_at": datetime.now(UTC).isoformat(),
        "golden_path": str(golden_path),
        "manifest_path": str(manifest_path),
        "dataset_sha256": {
            "golden": hashlib.sha256(golden_path.read_bytes()).hexdigest(),
            "manifest": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "k": k,
        "split": split,
        "question_count": len(results),
        "retrieval_mode": "hybrid_rrf" if dense_index is not None else "fts5",
        "metrics": _aggregate(results),
        "baseline_metrics": _aggregate(baseline_results) if dense_index is not None else None,
        "results": results,
    }


def write_report(report: dict, path: Path) -> None:
    """Write an evaluation report as stable, human-readable JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
