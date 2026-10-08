"""Operational coverage and value reports for the local RAG corpus."""
from __future__ import annotations


def log_query(con, *, question: str, route: str, hits: list, abstained: bool) -> None:
    """Record anonymized retrieval usage and top-source provenance."""
    top = hits[0] if hits else None
    cursor = con.execute(
        """INSERT INTO rag_query_log
           (created_at,question,route,returned_hits,top_doc_sha256,
            top_source_kind,top_authority_level,abstained)
           VALUES (datetime('now'),?,?,?,?,?,?,?)""",
        (question, route, len(hits), top.doc_sha256 if top else None,
         top.source_kind if top else None, top.authority_level if top else None,
         int(abstained)),
    )
    con.commit()
    return cursor.lastrowid


def log_feedback(con, *, query_id: int, label: str, note: str | None = None) -> None:
    """Store explicit usefulness feedback for a logged query."""
    allowed = {"useful", "irrelevant", "incorrect", "missing_evidence"}
    if label not in allowed:
        raise ValueError(f"label must be one of: {', '.join(sorted(allowed))}")
    if con.execute("SELECT 1 FROM rag_query_log WHERE id=?", (query_id,)).fetchone() is None:
        raise ValueError("query_id does not exist")
    con.execute(
        "INSERT INTO rag_feedback(query_id,created_at,label,note) VALUES (?,datetime('now'),?,?)",
        (query_id, label, note),
    )
    con.commit()


def coverage_report(con) -> dict:
    """Return ingestion coverage and observed retrieval-value indicators."""
    docs = con.execute(
        """SELECT COALESCE(source_kind,'unknown'), COALESCE(authority_level,'unknown'),
                  COUNT(*) FROM rag_documents GROUP BY 1,2 ORDER BY 1,2"""
    ).fetchall()
    queries = con.execute("SELECT COUNT(*) FROM rag_query_log").fetchone()[0]
    answered = con.execute(
        "SELECT COUNT(*) FROM rag_query_log WHERE returned_hits > 0 AND abstained=0"
    ).fetchone()[0]
    return {
        "documents": sum(row[2] for row in docs),
        "documents_by_source": [
            {"source_kind": row[0], "authority_level": row[1], "count": row[2]}
            for row in docs
        ],
        "queries_logged": queries,
        "queries_with_evidence": answered,
        "evidence_rate": answered / queries if queries else None,
        "facts": con.execute("SELECT COUNT(*) FROM rag_facts").fetchone()[0],
        "chunks": con.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0],
    }
