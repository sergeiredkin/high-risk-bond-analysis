"""Sparse and hybrid retrieval for the first local RAG implementation."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchHit:
    """A retrieved chunk with page-level provenance."""

    chunk_id: str
    doc_sha256: str
    page_no: int
    section: str
    text: str
    score: float
    source_kind: str = "unknown"
    authority_level: str = "unknown"


_STOP_WORDS = {
    "и", "в", "во", "на", "по", "с", "со", "о", "об", "от", "до",
    "для", "из", "к", "как", "что", "это", "ли", "у", "а", "но",
    "такое", "какой", "какая", "каково", "какова", "можно", "есть",
}
_RU_SUFFIXES = (
    "иями", "ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ать",
    "ять", "ить", "еть", "ов", "ев", "ей", "ам", "ям", "ом", "ем",
    "ым", "им", "ах", "ях", "ия", "ие", "ии", "а", "я", "ы", "и",
    "е", "о", "у", "ю", "ь", "й",
)


def _stem(term: str) -> str:
    """Apply a small dependency-free Russian/English suffix stemmer."""
    for suffix in _RU_SUFFIXES:
        if len(term) > len(suffix) + 2 and term.endswith(suffix):
            return term[:-len(suffix)]
    return term


def _terms(question: str) -> list[str]:
    """Return informative normalized terms used by retrieval and reranking."""
    raw = re.findall(r"[\wА-Яа-яЁё-]{2,}", question.casefold())
    return list(dict.fromkeys(_stem(term) for term in raw if term not in _STOP_WORDS))


def _query_terms(question: str) -> str:
    """Build a prefix FTS5 query to tolerate Russian inflection."""
    return " OR ".join(f'"{term.replace(chr(34), "")}"*' for term in _terms(question))


def search(con: sqlite3.Connection, question: str, limit: int = 8,
           *, issuer_inn: str | None = None, isin: str | None = None,
           source_scope: str = "all", doc_type: str | None = None) -> list[SearchHit]:
    """Retrieve top chunks with SQLite FTS5 and optional issuer scope."""
    query = _query_terms(question)
    if not query:
        return []
    filters = ["rag_chunks_fts MATCH ?"]
    params: list = [query]
    if issuer_inn and issuer_inn != "unknown":
        filters.append("d.issuer_inn = ?")
        params.append(issuer_inn)
    if isin and isin != "unknown":
        filters.append("d.isin = ?")
        params.append(isin)
    if doc_type and doc_type != "all":
        filters.append("d.doc_type = ?")
        params.append(doc_type)
    if source_scope == "official":
        filters.append("d.authority_level IN (?, ?, ?, ?)")
        params.extend(("official", "issuer_disclosure", "financial_source", "rating_source"))
    elif source_scope not in {"all", "official"}:
        raise ValueError("source_scope must be 'all' or 'official'")
    # Retrieve a wider candidate set, then rerank by stemmed term coverage.
    candidate_limit = max(limit * 8, 32)
    params.append(candidate_limit)
    rows = con.execute(
        f"""SELECT c.chunk_id, c.doc_sha256, c.page_no, c.section, c.text,
                  bm25(rag_chunks_fts) AS score, d.source_kind, d.authority_level
           FROM rag_chunks_fts f JOIN rag_chunks c ON c.chunk_id = f.chunk_id
           JOIN rag_documents d ON d.doc_sha256 = c.doc_sha256
           WHERE {' AND '.join(filters)} ORDER BY score LIMIT ?""",
        params,
    ).fetchall()
    terms = _terms(question)
    phrase = " ".join(terms)
    ranked = []
    for row in rows:
        text_terms = set(_terms(f"{row[3] or ''} {row[4]}"))
        overlap = len(set(terms) & text_terms)
        if not overlap:
            continue
        # Multi-term questions need more than one coincidental word. A
        # one-term question (ISIN, G-spread, YTM) remains searchable.
        if len(set(terms)) >= 3 and overlap < 2:
            continue
        coverage = overlap / max(len(set(terms)), 1)
        phrase_bonus = 0.25 if phrase and phrase in " ".join(_terms(row[4])) else 0.0
        authority_bonus = {"official": 0.06, "issuer_disclosure": 0.05,
                           "rating_source": 0.04, "financial_source": 0.03,
                           "derived_analysis": 0.01, "reference": 0.0}.get(row[7], 0.0)
        score = coverage + phrase_bonus + authority_bonus + max(float(row[5]) * -0.001, 0.0)
        ranked.append(SearchHit(row[0], row[1], row[2], row[3] or "", row[4], score,
                                row[6] or "unknown", row[7] or "unknown"))
    ranked.sort(key=lambda hit: hit.score, reverse=True)
    # Several adjacent chunks often come from the same page. Return one best
    # evidence window per document/page to avoid flooding the researcher.
    unique: list[SearchHit] = []
    seen_pages: set[tuple[str, int]] = set()
    for hit in ranked:
        key = (hit.doc_sha256, hit.page_no)
        if key not in seen_pages:
            seen_pages.add(key)
            unique.append(hit)
        if len(unique) >= limit:
            break
    return unique


def snippet(text: str, question: str, max_chars: int = 900) -> str:
    """Return a compact evidence window centred on a query term."""
    if len(text) <= max_chars:
        return text
    lowered = text.casefold()
    positions = [lowered.find(term) for term in _terms(question) if lowered.find(term) >= 0]
    start = max(0, (min(positions) if positions else 0) - max_chars // 4)
    return (text[start:start + max_chars]).strip()


def reciprocal_rank_fusion(*ranked_lists: list[SearchHit], limit: int = 8, k: int = 60) -> list[SearchHit]:
    """Fuse independently ranked hit lists without making score scales comparable."""
    scores: dict[str, float] = {}
    by_id: dict[str, SearchHit] = {}
    for hits in ranked_lists:
        for rank, hit in enumerate(hits, 1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            by_id[hit.chunk_id] = hit
    return [
        SearchHit(by_id[chunk].chunk_id, by_id[chunk].doc_sha256, by_id[chunk].page_no,
                  by_id[chunk].section, by_id[chunk].text, score,
                  by_id[chunk].source_kind, by_id[chunk].authority_level)
        for chunk, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def hybrid_search(con: sqlite3.Connection, question: str, limit: int = 8,
                  dense_hits: list[SearchHit] | None = None,
                  *, issuer_inn: str | None = None, isin: str | None = None,
                  source_scope: str = "all", doc_type: str | None = None) -> list[SearchHit]:
    """Fuse sparse results with optional dense results using reciprocal rank fusion.

    The dense provider is deliberately injected. This keeps the default install
    offline and dependency-free while allowing BGE-M3 or another local model to
    be added in P2 without changing the answer layer.
    """
    sparse_hits = search(con, question, limit, issuer_inn=issuer_inn, isin=isin,
                         source_scope=source_scope, doc_type=doc_type)
    return reciprocal_rank_fusion(sparse_hits, dense_hits or [], limit=limit) if dense_hits else sparse_hits
