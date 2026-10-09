"""Metrics for page retrieval and extractive answer checks."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation


def normalise(text: str) -> str:
    """Normalize case and whitespace for conservative exact-match checks."""
    return re.sub(r"\s+", " ", text.casefold().strip())


def _numeric_value(value: str) -> Decimal | None:
    """Parse a localized number with grouped spaces and decimal comma."""
    cleaned = value.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    cleaned = cleaned.removesuffix("%").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def expected_in_hits(expected: object, answer_type: str, hits: list) -> bool | None:
    """Check whether a simple expected answer appears in retrieved evidence.

    Returns ``None`` for unsupported/manual-review text answers.
    """
    if answer_type == "none":
        return None
    corpus = normalise("\n".join(hit.text for hit in hits))
    if answer_type in {"numeric", "boolean", "date"}:
        value = expected.get("value") if isinstance(expected, dict) else expected
        if value is None:
            return False
        if answer_type == "numeric":
            target = _numeric_value(str(value))
            if target is None:
                return False
            numbers = re.findall(
                r"(?<!\w)[+-]?\d(?:[\d\u00a0\u202f ]*\d)?(?:[,.]\d+)?%?",
                corpus,
            )
            return any(_numeric_value(number) == target for number in numbers)
        candidates = {normalise(str(value))}
        if answer_type == "date":
            try:
                parsed = date.fromisoformat(str(value))
                candidates.update({
                    parsed.strftime("%-d.%-m.%Y"),
                    parsed.strftime("%d.%m.%Y"),
                    parsed.strftime("%-d.%m.%Y"),
                })
            except ValueError:
                pass
        if answer_type == "boolean":
            markers = ("не исполнил", "не исполнена", "не выплачен", "не выплачено",
                       "неисполнение", "не соблюден") if value is True else (
                           "недостаточно", "не достаточно", "отсутствует", "нельзя",
                           "не следует", "нет", "no", "false"
                       )
            return any(marker in corpus for marker in markers)
        return any(normalise(candidate) in corpus for candidate in candidates)
    if answer_type == "list":
        values = expected if isinstance(expected, list) else expected.get("values", [])
        return all(normalise(str(value)) in corpus for value in values)
    return None


def page_recall(hits: list, evidence: tuple[dict, ...]) -> float:
    """Return 1 when at least one expected document/page was retrieved."""
    if not evidence:
        return 0.0
    expected = {(item["doc_sha256"], item["page"]) for item in evidence}
    actual = {(hit.doc_sha256, hit.page_no) for hit in hits}
    return float(bool(expected & actual))


def citation_precision(hits: list, evidence: tuple[dict, ...]) -> float:
    """Fraction of retrieved pages that are annotated as relevant evidence."""
    if not hits:
        return 0.0
    expected = {(item["doc_sha256"], item["page"]) for item in evidence}
    return sum((hit.doc_sha256, hit.page_no) in expected for hit in hits) / len(hits)


def reciprocal_rank(hits: list, evidence: tuple[dict, ...]) -> float:
    """Return reciprocal rank of the first expected page, or zero."""
    expected = {(item["doc_sha256"], item["page"]) for item in evidence}
    for rank, hit in enumerate(hits, 1):
        if (hit.doc_sha256, hit.page_no) in expected:
            return 1.0 / rank
    return 0.0
