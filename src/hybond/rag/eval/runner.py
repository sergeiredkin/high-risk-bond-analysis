"""Run reproducible offline retrieval evaluations."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..golden import GoldenQuestion, load_golden
from ..manifest import load_manifest
from ..search import hybrid_search, search
from .metrics import citation_precision, expected_in_hits, page_recall, reciprocal_rank


def _result(question: GoldenQuestion, hits: list, k: int) -> dict:
    """Score one golden question against retrieved evidence."""
    top = hits[:k]
    is_unanswerable = question.answer_type == "none"
    abstained = not bool(top)
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
        "abstained": abstained,
        "abstention_correct": abstained if is_unanswerable else None,
        "citations": [
            {"doc_sha256": hit.doc_sha256, "page": hit.page_no}
            for hit in top
        ],
    }


def _aggregate(results: list[dict]) -> dict:
    """Aggregate retrieval metrics for one ranked result set."""
    answerable = [r for r in results if r["answer_type"] != "none"]
    abstention = [r for r in results if r["answer_type"] == "none"]
    return {
        "page_recall_at_k": sum(r["page_recall"] for r in results) / len(results) if results else 0.0,
        "citation_precision_at_k": sum(r["citation_precision"] for r in results) / len(results) if results else 0.0,
        "mrr": sum(r["mrr"] for r in results) / len(results) if results else 0.0,
        "answerable_evidence_match_rate": (
            sum(r["answer_evidence_match"] is True for r in answerable) / len(answerable)
            if answerable else 0.0
        ),
        "abstention_accuracy": (
            sum(r["abstention_correct"] is True for r in abstention) / len(abstention)
            if abstention else 0.0
        ),
        "manual_review_text_answers": sum(
            r["answer_evidence_match"] is None for r in answerable
        ),
    }


def run(con, golden_path: Path, manifest_path: Path, *, k: int = 5,
        run_id: str | None = None, dense_index=None) -> dict:
    """Evaluate retrieval against a validated golden set and return JSON data.

    The runner deliberately evaluates retrieval and evidence grounding, not
    subjective narrative quality. Text answers remain marked for manual review.
    """
    documents = load_manifest(manifest_path)
    questions = load_golden(golden_path, documents)
    results = []
    baseline_results = []
    for question in questions:
        issuer_inn = getattr(question, "issuer_inn", None)
        isin = getattr(question, "isin", None)
        sparse = search(con, question.question, limit=k, issuer_inn=issuer_inn, isin=isin)
        baseline_results.append(_result(question, sparse, k))
        if dense_index is not None:
            dense = dense_index.search(question.question, limit=k, issuer_inn=issuer_inn, isin=isin)
            hits = hybrid_search(con, question.question, limit=k, dense_hits=dense,
                                 issuer_inn=issuer_inn, isin=isin)
        else:
            hits = sparse
        results.append(_result(question, hits, k))
    return {
        "run_id": run_id or f"eval-{uuid.uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "golden_path": str(golden_path),
        "manifest_path": str(manifest_path),
        "k": k,
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
