"""Regression comparison for reproducible RAG evaluation reports."""
from __future__ import annotations

import json
from pathlib import Path

_METRICS = (
    "page_recall_at_k", "mrr", "citation_precision_at_k",
    "answerable_evidence_match_rate", "abstention_accuracy",
)


def load_report(path: Path) -> dict:
    """Load one JSON evaluation report."""
    return json.loads(path.read_text(encoding="utf-8"))


def compare(baseline: dict, candidate: dict, *, max_drop: float = 0.02) -> dict:
    """Compare compatible eval reports; reject split or corpus mismatches."""
    compatibility_errors = []
    for field in ("split", "k", "question_count", "dataset_sha256"):
        before, after = baseline.get(field), candidate.get(field)
        if before != after and (before is not None or after is not None):
            compatibility_errors.append(field)
    if compatibility_errors:
        return {
            "passed": False,
            "max_drop": max_drop,
            "regressions": [],
            "compatibility_errors": compatibility_errors,
            "metrics": {},
        }
    base = baseline.get("metrics", {})
    current = candidate.get("metrics", {})
    metrics = {}
    regressions = []
    for name in _METRICS:
        before = base.get(name)
        after = current.get(name)
        if before is None or after is None:
            continue
        delta = after - before
        item = {"baseline": before, "candidate": after, "delta": delta,
                "regression": delta < -max_drop}
        metrics[name] = item
        if item["regression"]:
            regressions.append(name)
    return {"passed": bool(metrics) and not regressions, "max_drop": max_drop,
            "regressions": regressions, "compatibility_errors": [], "metrics": metrics}
