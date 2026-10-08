"""Configured retrieval entry point; dense mode is opt-in and local-only."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .dense import LocalDenseEncoder
from .dense_index import DenseIndex
from .search import hybrid_search


@lru_cache(maxsize=1)
def _dense_index(index_path: str, model_path: str) -> DenseIndex:
    """Load the configured local model and index once per process."""
    return DenseIndex.load(Path(index_path), LocalDenseEncoder(Path(model_path)))


def retrieve(con, question: str, limit: int = 8, *, issuer_inn: str | None = None,
             isin: str | None = None, source_scope: str = "all",
             doc_type: str | None = None):
    """Use lexical search by default, hybrid RRF when a local dense index is configured."""
    index_path = os.environ.get("HYBOND_RAG_DENSE_INDEX")
    model_path = os.environ.get("HYBOND_RAG_MODEL_PATH")
    if not index_path and not model_path:
        return hybrid_search(con, question, limit, issuer_inn=issuer_inn, isin=isin,
                             source_scope=source_scope, doc_type=doc_type)
    if not index_path or not model_path:
        raise RuntimeError("set both HYBOND_RAG_DENSE_INDEX and HYBOND_RAG_MODEL_PATH")
    dense = _dense_index(index_path, model_path)
    dense_hits = dense.search(question, limit=limit, issuer_inn=issuer_inn, isin=isin,
                              source_scope=source_scope, doc_type=doc_type)
    return hybrid_search(con, question, limit, dense_hits=dense_hits,
                         issuer_inn=issuer_inn, isin=isin,
                         source_scope=source_scope, doc_type=doc_type)
