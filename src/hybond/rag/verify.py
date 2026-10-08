"""Citation and evidence verification helpers."""
from __future__ import annotations

import sqlite3


def verify_citation(con: sqlite3.Connection, citation: dict) -> dict:
    """Verify that a citation points to an indexed document/page and optional quote."""
    doc_sha256 = citation.get("doc_sha256")
    page = citation.get("page")
    if not doc_sha256 or not isinstance(page, int):
        return {"valid": False, "reason": "missing_document_or_page"}
    rows = con.execute(
        "SELECT text FROM rag_chunks WHERE doc_sha256=? AND page_no=?",
        (doc_sha256, page),
    ).fetchall()
    if not rows:
        return {"valid": False, "reason": "page_not_indexed"}
    quote = citation.get("quote")
    if quote and not any(quote.strip() in row[0] for row in rows):
        return {"valid": False, "reason": "quote_not_found"}
    return {"valid": True, "reason": "verified"}
