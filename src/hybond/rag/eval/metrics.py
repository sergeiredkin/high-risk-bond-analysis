"""Metrics for page retrieval and extractive answer checks."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation


def normalise(text: str) -> str:
    """Normalize case and whitespace for conservative exact-match checks."""
    return re.sub(r"\s+", " ", text.casefold().strip())


def _numeric_value(value: str) -> Decimal | None:
    """Parse a localized numeric token without discarding its unit."""
    cleaned = value.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _unit(value: str | None) -> str | None:
    """Normalize a small explicit set of evaluation units."""
    if value is None or not value.strip():
        return None
    cleaned = value.strip().casefold().rstrip(".")
    if cleaned in {"руб", "рубль", "рубля", "рублей", "р"}:
        return "RUB"
    if cleaned in {"%", "процент", "процента", "процентов"}:
        return "PERCENT"
    if cleaned in {"юань", "юаня", "юаней", "юани", "cny", "¥"}:
        return "CNY"
    if cleaned in {"шт", "шт.", "штука", "штуки", "штук", "ед", "ед."}:
        return "COUNT"
    return cleaned.upper()


def expected_in_hits(expected: object, answer_type: str, hits: list) -> bool | None:
    """Check whether a simple expected answer appears in retrieved evidence.

    Returns ``None`` for unsupported/manual-review text answers.
    """
    if answer_type == "none":
        return None
    corpus = normalise("\n".join(hit.text for hit in hits))
    if answer_type in {"numeric", "boolean", "date"}:
        value = (expected.get("value") if isinstance(expected, dict)
                 and answer_type != "numeric" else expected)
        if value is None:
            return False
        if answer_type == "numeric":
            unit = value.get("unit") if isinstance(value, dict) else None
            numeric_value = value.get("value") if isinstance(value, dict) else value
            target = _numeric_value(str(numeric_value))
            if target is None:
                return False
            target_unit = _unit(unit)
            tokens = re.finditer(
                r"(?<!\w)(?P<number>[+-]?\d(?:[\d\u00a0\u202f ]*\d)?(?:[,.]\d+)?)(?:\s*\([^)]{1,100}\))?(?P<unit>\s*(?:%|руб(?:ль|ля|лей)?\.?|р\.?|юан(?:ь|я|ей|и)|cny|¥|шт\.?|штука|штуки|штук|ед\.?))?",
                corpus,
            )
            return any(
                _numeric_value(match.group("number")) == target
                and _unit(match.group("unit")) == target_unit
                for match in tokens
            )
        if answer_type == "boolean":
            # The matcher has no access to the question's proposition. A
            # negation substring cannot safely establish truth polarity.
            return None
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
        return any(normalise(candidate) in corpus for candidate in candidates)
    if answer_type == "list":
        values = expected if isinstance(expected, list) else expected.get("values", [])
        return all(normalise(str(value)) in corpus for value in values)
    return None


def page_recall(hits: list, evidence: tuple[dict, ...]) -> float:
    """Return the fraction of required document/pages retrieved at k."""
    if not evidence:
        return 0.0
    expected = {(item["doc_sha256"], item["page"]) for item in evidence}
    actual = {(hit.doc_sha256, hit.page_no) for hit in hits}
    return len(expected & actual) / len(expected)


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
