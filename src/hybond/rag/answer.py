"""Safe SQL-backed routing for structured fact questions."""
from __future__ import annotations

import json

_FACT_KEYWORDS = {
    "isin": ("isin", "ISIN"),
    "issuer_inn": ("инн", "ИНН"),
    "unpaid_amount_rub": ("сумм", "размер обязательств", "неисполнен", "руб"),
}


def route_fact_type(question: str) -> str | None:
    """Return a known structured fact type, or None for narrative questions."""
    lowered = question.casefold()
    for fact_type, keywords in _FACT_KEYWORDS.items():
        if fact_type == "unpaid_amount_rub":
            numeric_intent = any(keyword in lowered for keyword in ("сумм", "размер обязатель", "сколько", "руб"))
            risk_context = any(keyword in lowered for keyword in ("неисполн", "не выпла", "обязательств"))
            if numeric_intent and risk_context:
                return fact_type
        elif any(keyword.casefold() in lowered for keyword in keywords):
            return fact_type
    return None


def answer_fact(con, question: str, *, doc_sha256: str | None = None,
                isin: str | None = None, issuer_inn: str | None = None) -> dict | None:
    """Return a reconciled structured fact with provenance, never a generated number."""
    fact_type = route_fact_type(question)
    if fact_type is None:
        return None
    clauses = ["f.fact_type=?", "f.reconcile_status='match'"]
    params: list[str] = [fact_type]
    if doc_sha256:
        clauses.append("f.doc_sha256=?")
        params.append(doc_sha256)
    if isin:
        clauses.append("d.isin=?")
        params.append(isin)
    if issuer_inn:
        clauses.append("d.issuer_inn=?")
        params.append(issuer_inn)
    row = con.execute(
        f"SELECT f.* FROM rag_facts f JOIN rag_documents d ON d.doc_sha256=f.doc_sha256 "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY extracted_at DESC LIMIT 1", params,
    ).fetchone()
    if row is None:
        return {"status": "not_found", "fact_type": fact_type}
    result = dict(row)
    result["value"] = json.loads(result.pop("value_json"))
    result["status"] = "found"
    return result
