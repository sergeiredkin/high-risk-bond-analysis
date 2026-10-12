"""SQLite registry for source documents, pages, chunks, and index versions."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_documents (
    doc_sha256 TEXT PRIMARY KEY,
    isin TEXT,
    issuer_inn TEXT,
    doc_type TEXT NOT NULL,
    source_url TEXT,
    fetched_at TEXT,
    local_path TEXT NOT NULL,
    page_count INTEGER,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    source_kind TEXT NOT NULL DEFAULT 'unknown',
    authority_level TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_pages (
    doc_sha256 TEXT NOT NULL REFERENCES rag_documents(doc_sha256),
    page_no INTEGER NOT NULL,
    is_scanned INTEGER NOT NULL DEFAULT 0,
    ocr_confidence REAL,
    image_path TEXT,
    PRIMARY KEY (doc_sha256, page_no)
);
CREATE TABLE IF NOT EXISTS rag_elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_sha256 TEXT NOT NULL REFERENCES rag_documents(doc_sha256),
    page_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    bbox TEXT,
    text TEXT,
    table_html TEXT
);
CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_sha256 TEXT NOT NULL REFERENCES rag_documents(doc_sha256),
    page_no INTEGER NOT NULL,
    section TEXT,
    text TEXT NOT NULL,
    chunk_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT,
    fact_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    doc_sha256 TEXT NOT NULL REFERENCES rag_documents(doc_sha256),
    page_no INTEGER NOT NULL,
    bbox TEXT,
    quote TEXT,
    confidence REAL,
    reconcile_status TEXT NOT NULL DEFAULT 'unverifiable',
    extracted_at TEXT NOT NULL,
    model TEXT
);
CREATE TABLE IF NOT EXISTS rag_index_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rag_eval_runs (
    run_id TEXT PRIMARY KEY,
    config_hash TEXT NOT NULL,
    index_version INTEGER,
    golden_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_eval_results (
    run_id TEXT NOT NULL REFERENCES rag_eval_runs(run_id),
    qid TEXT NOT NULL,
    page_recall REAL,
    citation_precision REAL,
    answer_score REAL,
    abstained INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, qid)
);
CREATE TABLE IF NOT EXISTS rag_query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    question TEXT NOT NULL,
    route TEXT NOT NULL,
    returned_hits INTEGER NOT NULL DEFAULT 0,
    top_doc_sha256 TEXT,
    top_source_kind TEXT,
    top_authority_level TEXT,
    abstained INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rag_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id INTEGER NOT NULL REFERENCES rag_query_log(id),
    created_at TEXT NOT NULL,
    label TEXT NOT NULL CHECK(label IN ('useful','irrelevant','incorrect','missing_evidence')),
    note TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
    chunk_id UNINDEXED, text, section
);
"""


def init_registry(con: sqlite3.Connection) -> None:
    """Create RAG tables and the lexical search index idempotently."""
    con.executescript(SCHEMA)
    document_cols = {row[1] for row in con.execute("PRAGMA table_info(rag_documents)")}
    for column, declaration in (("source_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
                                ("authority_level", "TEXT NOT NULL DEFAULT 'unknown'")):
        if column not in document_cols:
            con.execute(f"ALTER TABLE rag_documents ADD COLUMN {column} {declaration}")
    # Early P0 builds used a contentless FTS table, which cannot return the
    # external chunk_id. Rebuild that replaceable index in place if detected.
    row = con.execute("SELECT chunk_id FROM rag_chunks_fts LIMIT 1").fetchone()
    if row is not None and row[0] is None:
        con.execute("DROP TABLE rag_chunks_fts")
        con.execute("CREATE VIRTUAL TABLE rag_chunks_fts USING fts5(chunk_id UNINDEXED, text, section)")
        for chunk in con.execute("SELECT chunk_id, text, section FROM rag_chunks"):
            con.execute("INSERT INTO rag_chunks_fts(chunk_id,text,section) VALUES(?,?,?)", tuple(chunk))
    con.execute(
        """UPDATE rag_documents SET source_kind='hybond_factlist',
           authority_level='derived_analysis' WHERE doc_type='hybond_factlist'
           AND (source_kind != 'hybond_factlist' OR authority_level != 'derived_analysis')"""
    )


def upsert_document(con: sqlite3.Connection, document: dict) -> None:
    """Register a document without replacing an existing parsed record."""
    now = datetime.now(UTC).isoformat()
    con.execute(
        """INSERT INTO rag_documents
           (doc_sha256, isin, issuer_inn, doc_type, source_url, fetched_at,
            local_path, page_count, parse_status, source_kind, authority_level, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(doc_sha256) DO UPDATE SET
             isin=excluded.isin, issuer_inn=excluded.issuer_inn,
             source_url=excluded.source_url, fetched_at=excluded.fetched_at,
             local_path=excluded.local_path,
             source_kind=CASE WHEN excluded.source_kind='unknown' THEN rag_documents.source_kind ELSE excluded.source_kind END,
             authority_level=CASE WHEN excluded.authority_level='unknown' THEN rag_documents.authority_level ELSE excluded.authority_level END""",
        (document["doc_sha256"], document.get("isin"), document.get("issuer_inn"),
         document["doc_type"], document.get("source_url"), document.get("fetched_at"),
         document["local_path"], document.get("page_count"),
         document.get("parse_status", "pending"), document.get("source_kind", "unknown"),
         document.get("authority_level", "unknown"), now),
    )


def add_chunk(con: sqlite3.Connection, chunk: dict) -> None:
    """Insert one chunk and its FTS5 row, replacing an identical chunk safely."""
    con.execute(
        """INSERT OR REPLACE INTO rag_chunks
           (chunk_id, doc_sha256, page_no, section, text, chunk_hash)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chunk["chunk_id"], chunk["doc_sha256"], chunk["page_no"],
         chunk.get("section", ""), chunk["text"], chunk["chunk_hash"]),
    )
    con.execute("DELETE FROM rag_chunks_fts WHERE chunk_id = ?", (chunk["chunk_id"],))
    con.execute(
        "INSERT INTO rag_chunks_fts(chunk_id, text, section) VALUES (?, ?, ?)",
        (chunk["chunk_id"], chunk["text"], chunk.get("section", "")),
    )
