"""Reconcile extracted RAG facts with authoritative HyBond tables."""
from __future__ import annotations


def reconcile_document_facts(con, doc_sha256: str) -> dict[str, int]:
    """Mark identifier facts as match/mismatch and leave unsupported facts open.

    Monetary disclosure facts have no equivalent authoritative table yet, so
    they remain ``unverifiable`` rather than being treated as confirmed.
    """
    facts = con.execute(
        "SELECT id, fact_type, value_json FROM rag_facts WHERE doc_sha256=?",
        (doc_sha256,),
    ).fetchall()
    counts = {"match": 0, "mismatch": 0, "unverifiable": 0}
    for fact in facts:
        status = "unverifiable"
        if fact[1] == "isin":
            value = fact[2].strip('"')
            status = "match" if con.execute(
                "SELECT 1 FROM bond WHERE isin=? LIMIT 1", (value,)
            ).fetchone() else "mismatch"
        elif fact[1] == "issuer_inn":
            value = fact[2].strip('"')
            status = "match" if con.execute(
                "SELECT 1 FROM issuer WHERE issuer_id=? LIMIT 1", (value,)
            ).fetchone() else "mismatch"
        con.execute("UPDATE rag_facts SET reconcile_status=? WHERE id=?", (status, fact[0]))
        counts[status] += 1
    con.commit()
    return counts
