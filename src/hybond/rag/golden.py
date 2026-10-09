"""Validation for versioned RAG evaluation question sets."""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .manifest import CorpusDocument

_CATEGORIES = {
    "coupon", "coupon_schedule", "maturity", "put_offer", "call_option",
    "amortization", "guarantor", "covenant", "use_of_proceeds", "issue_volume",
    "financials_numeric", "rating_action", "risk_factor", "management_commentary",
    "issuer_disclosure_event", "macro_forecast", "cross_doc", "unanswerable",
}
_ANSWER_TYPES = {"numeric", "date", "text", "list", "boolean", "none"}
_REQUIREMENTS = {"table", "scan", "figure", "cross_doc", "multi_page"}


@dataclass(frozen=True)
class GoldenQuestion:
    """One evaluation question and its expected evidence."""

    qid: str
    split: str
    isin: str | None
    issuer_inn: str | None
    question: str
    category: str
    answer_type: str
    expected: object
    evidence: tuple[dict, ...]
    requires: tuple[str, ...]


def load_golden(path: Path, manifest: Iterable[CorpusDocument] | None = None) -> list[GoldenQuestion]:
    """Load JSONL golden questions and validate their references.

    Raises:
        ValueError: if a question is malformed, duplicated, or references an
            unknown document hash.
    """
    known = {document.doc_sha256 for document in manifest} if manifest is not None else None
    questions: list[GoldenQuestion] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"golden line {line_no}: invalid JSON") from exc
        if not isinstance(raw, dict):
            raise TypeError(f"golden line {line_no}: expected an object")
        required = {"qid", "split", "question", "category", "answer_type", "expected", "evidence", "requires"}
        missing = required - raw.keys()
        if missing:
            raise ValueError(f"golden line {line_no}: missing {sorted(missing)}")
        qid = raw["qid"]
        if not isinstance(qid, str) or not qid or qid in seen:
            raise ValueError(f"golden line {line_no}: qid must be unique and non-empty")
        if raw["category"] not in _CATEGORIES or raw["answer_type"] not in _ANSWER_TYPES:
            raise ValueError(f"golden line {line_no}: unknown category or answer_type")
        if not isinstance(raw["evidence"], list) or not isinstance(raw["requires"], list):
            raise TypeError(f"golden line {line_no}: evidence and requires must be arrays")
        if any(item not in _REQUIREMENTS for item in raw["requires"]):
            raise ValueError(f"golden line {line_no}: unknown requires value")
        for evidence in raw["evidence"]:
            if not isinstance(evidence, dict) or not isinstance(evidence.get("doc_sha256"), str):
                raise TypeError(f"golden line {line_no}: malformed evidence")
            if known is not None and evidence["doc_sha256"] not in known:
                raise ValueError(f"golden line {line_no}: evidence references unknown document")
            if not isinstance(evidence.get("page"), int) or evidence["page"] < 1:
                raise ValueError(f"golden line {line_no}: evidence page must be positive")
        questions.append(GoldenQuestion(
            qid=qid, split=raw["split"], isin=raw.get("isin"),
            issuer_inn=raw.get("issuer_inn"), question=raw["question"],
            category=raw["category"], answer_type=raw["answer_type"],
            expected=raw["expected"], evidence=tuple(raw["evidence"]),
            requires=tuple(raw["requires"]),
        ))
        seen.add(qid)
    return questions
