"""Metadata-only discovery of official issuer filing records."""
from __future__ import annotations

import sqlite3
from datetime import date


def is_payment_filing_lookup_question(question: str) -> bool:
    """Detect requests to find payment-related issuer messages, not prove payment."""
    value = question.casefold().replace("ё", "е")
    if any(term in value for term in ("невыплач", "неисполн", "просроч", "дефолт")):
        return False
    asks_for_message = any(term in value for term in ("сообщен", "раскрыт"))
    mentions_payment = any(term in value for term in ("выплат", "купон", "доход"))
    return asks_for_message and mentions_payment


def payment_filing_catalog(con: sqlite3.Connection, issuer_id: str,
                           *, limit: int = 20) -> list[dict]:
    """List official payment/income filing metadata without implying body evidence."""
    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='filing'"
    ).fetchone()
    if not has_table:
        return []
    rows = con.execute(
        """SELECT published,title,url FROM filing
           WHERE issuer_id=? AND url LIKE 'https://e-disclosure.azipi.ru/messages/%'
           ORDER BY published DESC,title,url""",
        (issuer_id,),
    ).fetchall()
    positive_titles = ("выплаченные доходы", "выплата купона", "выплата купонов")
    matches = [row for row in rows
               if any(marker in (row["title"] or "").casefold() for marker in positive_titles)]
    indexed_urls = set()
    indexed_pages: dict[str, list[str]] = {}
    for indexed in con.execute(
        """SELECT d.doc_sha256,d.source_url,c.text FROM rag_documents d
           JOIN rag_chunks c ON c.doc_sha256=d.doc_sha256
           WHERE d.issuer_inn=? AND d.doc_type='edisclosure_html'""",
        (issuer_id,),
    ):
        if indexed["source_url"]:
            indexed_urls.add(indexed["source_url"])
        indexed_pages.setdefault(indexed["doc_sha256"], []).append(indexed["text"])
    indexed_texts = [" ".join(" ".join(parts).casefold().split())
                     for parts in indexed_pages.values()]
    result = []
    for row in matches:
        if row["url"] in indexed_urls:
            continue
        title = " ".join((row["title"] or "").casefold().split())
        published = row["published"] or ""
        dotted_date = ""
        try:
            dotted_date = date.fromisoformat(published).strftime("%d.%m.%Y")
        except ValueError:
            pass
        if any(title in text and (published in text or dotted_date in text)
               for text in indexed_texts):
            continue
        result.append({
            "published": row["published"],
            "title": row["title"],
            "source_url": row["url"],
            "evidence_level": "filing_catalog_metadata_only",
        })
        if len(result) >= limit:
            break
    return result
