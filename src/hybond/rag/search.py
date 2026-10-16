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
    "найди", "найти", "покажи", "укажи", "приведи", "скажи", "расскажи",
    "документ", "документы", "документа", "документах", "документов",
    "сообщение", "сообщения", "сообщений", "фрагмент", "фрагменты",
    "цитата", "цитаты", "источник", "источника", "источники", "страница",
    "страницу", "соответствующие", "каждый", "каждого", "доступные", "доступных",
    "последние", "последний", "официальные", "официальных", "сказано", "говорится",
    "или", "за", "них", "раскрыт", "дат", "информац", "эмитент",
}
_TERM_ALIASES = {
    "цб": ("банк", "россии"),
    "cbr": ("банк", "россии"),
    "cb": ("банк", "россии"),
    "центробанк": ("банк", "россии"),
    "key": ("ключевой",),
    "rate": ("ставка",),
    "forecast": ("прогноз",),
}
_RU_SUFFIXES = (
    "иями", "ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ать",
    "ять", "ить", "еть", "ов", "ев", "ей", "ам", "ям", "ом", "ем",
    "ым", "им", "ах", "ях", "ия", "ие", "ии", "а", "я", "ы", "и",
    "е", "о", "у", "ю", "ь", "й",
)


def _source_date(path: str) -> str:
    """ISO date embedded in a dated source path, used only as a CBR tie-break."""
    match = re.search(r"20\d{2}-\d{2}-\d{2}", path)
    return match.group(0) if match else ""


def _stem(term: str) -> str:
    """Apply a small dependency-free Russian/English suffix stemmer."""
    for suffix in _RU_SUFFIXES:
        if len(term) > len(suffix) + 2 and term.endswith(suffix):
            return term[:-len(suffix)]
    return term


def _terms(question: str) -> list[str]:
    """Return informative normalized terms used by retrieval and reranking."""
    raw = re.findall(r"[\wА-Яа-яЁё-]{2,}", question.casefold())
    terms = []
    for term in raw:
        if term in _STOP_WORDS:
            continue
        for alias in _TERM_ALIASES.get(term, (term,)):
            normalized = _stem(alias)
            if normalized not in _STOP_WORDS:
                terms.append(normalized)
    return list(dict.fromkeys(terms))


def _query_terms(question: str) -> str:
    """Build prefix variants to tolerate Russian inflection and lexical aliases."""
    variants = []
    for term in _terms(question):
        variants.append(term)
        if len(term) >= 6 and re.fullmatch(r"[а-яё]+", term):
            variants.append(term[:-1])
    return " OR ".join(f'"{term.replace(chr(34), "")}"*' for term in dict.fromkeys(variants))


def _terms_match(left: str, right: str) -> bool:
    """Match conservative shared stems when the lightweight stemmer diverges."""
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return left == right or (common >= 6 and common >= min(len(left), len(right)) - 1)


def _page_context(con: sqlite3.Connection, hit: SearchHit, *, max_chunks: int = 16,
                  max_chars: int = 12_000) -> SearchHit:
    """Return a bounded, source-ordered window around the best page chunk."""
    rowid = con.execute("SELECT rowid FROM rag_chunks WHERE chunk_id=?", (hit.chunk_id,)).fetchone()
    if rowid is None:
        return hit
    rows_meta = con.execute(
        "SELECT rowid,length(text) FROM rag_chunks WHERE doc_sha256=? AND page_no=? ORDER BY rowid",
        (hit.doc_sha256, hit.page_no),
    ).fetchall()
    position = next((index for index, row in enumerate(rows_meta) if row[0] == rowid[0]), 0)
    start = max(0, min(position - max_chunks // 2, len(rows_meta) - max_chunks))
    end = min(len(rows_meta), start + max_chunks)
    while sum(row[1] for row in rows_meta[start:end]) > max_chars and end - start > 1:
        if position - start >= end - 1 - position:
            start += 1
        else:
            end -= 1
    rows = con.execute(
        "SELECT text FROM rag_chunks WHERE doc_sha256=? AND page_no=? ORDER BY rowid LIMIT ? OFFSET ?",
        (hit.doc_sha256, hit.page_no, end - start, start),
    ).fetchall()
    context = "\n\n".join(row[0].strip() for row in rows if row[0].strip())[:max_chars]
    return SearchHit(hit.chunk_id, hit.doc_sha256, hit.page_no, hit.section, context,
                     hit.score, hit.source_kind, hit.authority_level)


def search_scope_summary(con: sqlite3.Connection, *, issuer_inn: str | None = None,
                         isin: str | None = None, source_scope: str = "all",
                         doc_type: str | None = None) -> dict:
    """Describe indexed documents covered by the exact API filters."""
    if source_scope not in {"all", "official"}:
        raise ValueError("source_scope must be 'all' or 'official'")
    filters = ["1=1"]
    params: list = []
    if issuer_inn and issuer_inn != "unknown":
        filters.append("issuer_inn=?")
        params.append(issuer_inn)
    if isin and isin != "unknown":
        filters.append("isin=?")
        params.append(isin.upper())
    if doc_type and doc_type != "all":
        filters.append("doc_type=?")
        params.append(doc_type)
    if source_scope == "official":
        filters.append("authority_level IN (?,?,?,?)")
        params.extend(("official", "issuer_disclosure", "financial_source", "rating_source"))
    where = " AND ".join(filters)
    rows = con.execute(
        f"""SELECT doc_type,source_kind,authority_level,COUNT(*) AS count
            FROM rag_documents WHERE {where}
            GROUP BY doc_type,source_kind,authority_level
            ORDER BY doc_type,source_kind,authority_level""",
        params,
    ).fetchall()
    issuer_name = None
    if issuer_inn and issuer_inn != "unknown":
        issuer = con.execute("SELECT name FROM issuer WHERE issuer_id=?", (issuer_inn,)).fetchone()
        issuer_name = issuer[0] if issuer else None
    source_counts = [dict(row) for row in rows]
    return {
        "filters": {
            "issuer_inn": issuer_inn if issuer_inn != "unknown" else None,
            "issuer_name": issuer_name,
            "isin": isin.upper() if isin and isin != "unknown" else None,
            "source_scope": source_scope,
            "doc_type": doc_type if doc_type and doc_type != "all" else None,
        },
        "indexed_documents_in_scope": sum(row["count"] for row in rows),
        "by_source": source_counts,
    }


def search(con: sqlite3.Connection, question: str, limit: int = 8,
           *, issuer_inn: str | None = None, isin: str | None = None,
           source_scope: str = "all", doc_type: str | None = None) -> list[SearchHit]:
    """Retrieve top chunks with SQLite FTS5 and optional issuer scope."""
    query = _query_terms(question)
    if not query:
        return []
    terms = _terms(question)
    cbr_forecast_intent = (
        doc_type == "cbr_document"
        and "прогноз" in terms and "ставк" in terms
        and any(term.startswith("ключев") for term in terms)
    )
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
    candidate_limit = max(limit * 8, 128 if cbr_forecast_intent else 32)
    params.append(candidate_limit)
    rows = con.execute(
        f"""SELECT c.chunk_id, c.doc_sha256, c.page_no, c.section, c.text,
                  bm25(rag_chunks_fts) AS score, d.source_kind, d.authority_level, d.local_path
           FROM rag_chunks_fts f JOIN rag_chunks c ON c.chunk_id = f.chunk_id
           JOIN rag_documents d ON d.doc_sha256 = c.doc_sha256
           WHERE {' AND '.join(filters)} ORDER BY score LIMIT ?""",
        params,
    ).fetchall()
    scoped_terms: set[str] = set(_terms(isin or ""))
    if issuer_inn and issuer_inn != "unknown":
        issuer = con.execute("SELECT name FROM issuer WHERE issuer_id=?", (issuer_inn,)).fetchone()
        if issuer:
            scoped_terms.update(_terms(issuer[0]))
    ranking_terms = [
        term for term in terms
        if not any(_terms_match(term, entity_term) for entity_term in scoped_terms)
    ]
    if not ranking_terms:
        return []
    phrase = " ".join(ranking_terms)
    ranked: list[tuple[SearchHit, str]] = []
    for row in rows:
        text = f"{row[3] or ''} {row[4]}"
        if cbr_forecast_intent:
            text += f" {row[8] or ''}"
        text_terms = set(_terms(text))
        overlap = sum(
            any(_terms_match(term, text_term) for text_term in text_terms)
            for term in set(ranking_terms)
        )
        if not overlap:
            continue
        # Multi-term questions need more than one coincidental word.
        if len(set(ranking_terms)) >= 3 and overlap < 2:
            continue
        coverage = overlap / max(len(set(ranking_terms)), 1)
        phrase_bonus = 0.25 if phrase and phrase in " ".join(_terms(row[4])) else 0.0
        authority_bonus = {"official": 0.06, "issuer_disclosure": 0.05,
                           "rating_source": 0.04, "financial_source": 0.03,
                           "derived_analysis": 0.01, "reference": 0.0}.get(row[7], 0.0)
        metadata_bonus = 0.0
        if cbr_forecast_intent:
            source_path = (row[8] or "").casefold()
            filename = source_path.rsplit("/", 1)[-1]
            if filename.startswith("forecast."):
                metadata_bonus += 0.5
            elif "materials_index" in filename:
                metadata_bonus -= 0.3
            if (row[3] or "").startswith("table_"):
                metadata_bonus += 0.2
        score = coverage + phrase_bonus + authority_bonus + metadata_bonus + max(float(row[5]) * -0.001, 0.0)
        ranked.append((SearchHit(row[0], row[1], row[2], row[3] or "", row[4], score,
                                 row[6] or "unknown", row[7] or "unknown"), row[8] or ""))
    if cbr_forecast_intent:
        ranked.sort(key=lambda item: (
            (item[1].casefold().rsplit("/", 1)[-1].startswith("forecast.")),
            _source_date(item[1]),
            item[0].score,
        ), reverse=True)
    else:
        ranked.sort(key=lambda item: item[0].score, reverse=True)
    ranked = [item[0] for item in ranked]
    # Several adjacent chunks often come from the same page. Return one bounded
    # page-context bundle per document/page rather than only its top chunk.
    unique: list[SearchHit] = []
    seen_pages: set[tuple[str, int]] = set()
    for hit in ranked:
        key = (hit.doc_sha256, hit.page_no)
        if key not in seen_pages:
            seen_pages.add(key)
            unique.append(hit)
        if len(unique) >= limit:
            break
    return [_page_context(con, hit) for hit in unique]


def snippet(text: str, question: str, max_chars: int = 900,
            *, exclude_terms: tuple[str, ...] = ()) -> str:
    """Return the evidence window matching query terms, excluding scope labels."""
    if len(text) <= max_chars:
        return text
    lowered = text.casefold()
    terms = _terms(question)
    excluded = {term for value in exclude_terms for term in _terms(value)}
    focused_terms = [
        term for term in terms
        if not any(_terms_match(term, excluded_term) for excluded_term in excluded)
    ]
    if focused_terms:
        terms = focused_terms
    starts = {0}
    for term in terms:
        position = lowered.find(term)
        while position >= 0:
            starts.add(max(0, position - max_chars // 4))
            position = lowered.find(term, position + 1)

    def window_score(start: int) -> tuple[int, int, int]:
        window = lowered[start:start + max_chars]
        distinct = sum(term in window for term in terms)
        occurrences = sum(window.count(term) for term in terms)
        return distinct, occurrences, -start

    start = max(starts, key=window_score)
    return text[start:start + max_chars].strip()


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
                  source_scope: str = "all", doc_type: str | None = None,
                  sparse_limit: int | None = None) -> list[SearchHit]:
    """Fuse sparse results with optional dense results using reciprocal rank fusion.

    The dense provider is deliberately injected. This keeps the default install
    offline and dependency-free while allowing BGE-M3 or another local model to
    be added in P2 without changing the answer layer.
    """
    sparse_hits = search(con, question, sparse_limit or limit, issuer_inn=issuer_inn, isin=isin,
                         source_scope=source_scope, doc_type=doc_type)
    if not dense_hits:
        return sparse_hits
    filters = []
    params: list = []
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
    ids = [hit.chunk_id for hit in dense_hits]
    placeholders = ",".join("?" for _ in ids)
    clauses = [f"c.chunk_id IN ({placeholders})", *filters]
    allowed = {row[0] for row in con.execute(
        f"SELECT c.chunk_id FROM rag_chunks c JOIN rag_documents d "
        f"ON d.doc_sha256=c.doc_sha256 WHERE {' AND '.join(clauses)}", [*ids, *params]
    )}
    filtered_dense = [hit for hit in dense_hits if hit.chunk_id in allowed]
    fused = reciprocal_rank_fusion(sparse_hits, filtered_dense, limit=max(limit * 8, limit))
    unique: list[SearchHit] = []
    seen_pages: set[tuple[str, int]] = set()
    for hit in fused:
        key = (hit.doc_sha256, hit.page_no)
        if key not in seen_pages:
            seen_pages.add(key)
            unique.append(hit)
        if len(unique) >= limit:
            break
    return [_page_context(con, hit) for hit in unique]
