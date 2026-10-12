"""Conservative rule-based fact extraction for the SQL answer path."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

_FACT_PATTERNS = {
    "isin": re.compile(r"\b(RU[0-9A-Z]{10})\b"),
    "issuer_inn": re.compile(r"ИНН\s*[:№]?\s*(\d{10})", re.IGNORECASE),
    "unpaid_amount_rub": re.compile(
        r"(?:размер обязательства[^:]*:\s*|обязательств[^\d]{0,50})"
        r"([\d\s]+(?:[,\.]\d{1,2})?)\s*руб", re.IGNORECASE),
}


def _value(number: str) -> float:
    """Parse Russian thousands/decimal formatting into a float."""
    clean = number.replace(" ", "").replace("\u00a0", "")
    if "," in clean:
        clean = clean.replace(".", "").replace(",", ".")
    return float(clean)


def _quote(text: str, start: int, end: int) -> str:
    """Return a bounded provenance quote around a match."""
    return text[max(0, start - 100):min(len(text), end + 100)].strip()


def extract_document_facts(con, doc_sha256: str, *, model: str = "rules") -> list[dict]:
    """Extract high-confidence identifiers and explicit monetary amounts.

    Facts are stored as ``unverifiable`` until reconciled with an authoritative
    HyBond/MOEX source. Re-running replaces facts from the same extractor.
    """
    rows = con.execute(
        "SELECT page_no, text FROM rag_chunks WHERE doc_sha256=? ORDER BY page_no, chunk_id",
        (doc_sha256,),
    ).fetchall()
    if not rows:
        return []
    extracted: list[dict] = []
    now = datetime.now(UTC).isoformat()
    con.execute("DELETE FROM rag_facts WHERE doc_sha256=? AND model=?", (doc_sha256, model))
    for row in rows:
        text = row[1]
        for fact_type, pattern in _FACT_PATTERNS.items():
            for match in pattern.finditer(text):
                raw = match.group(1)
                value = _value(raw) if fact_type == "unpaid_amount_rub" else raw
                fact = {
                    "isin": None, "fact_type": fact_type,
                    "value_json": json.dumps(value, ensure_ascii=False),
                    "doc_sha256": doc_sha256, "page_no": row[0],
                    "bbox": None, "quote": _quote(text, match.start(), match.end()),
                    "confidence": 0.9, "reconcile_status": "unverifiable",
                    "extracted_at": now, "model": model,
                }
                con.execute(
                    """INSERT INTO rag_facts
                       (isin,fact_type,value_json,doc_sha256,page_no,bbox,quote,
                        confidence,reconcile_status,extracted_at,model)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(fact.values()),
                )
                extracted.append(fact)
    con.commit()
    return extracted


def query_facts(con, fact_type: str, *, doc_sha256: str | None = None,
                isin: str | None = None) -> list[dict]:
    """Return stored structured facts using parameterized filters."""
    clauses = ["fact_type=?"]
    params: list[str] = [fact_type]
    if doc_sha256:
        clauses.append("doc_sha256=?")
        params.append(doc_sha256)
    if isin:
        clauses.append("isin=?")
        params.append(isin)
    rows = con.execute(
        f"SELECT * FROM rag_facts WHERE {' AND '.join(clauses)} ORDER BY extracted_at DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]
