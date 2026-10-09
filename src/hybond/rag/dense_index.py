"""Optional content-addressed dense index for local hybrid retrieval."""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .search import SearchHit


@dataclass
class DenseIndex:
    """A small offline cosine index backed by NumPy arrays."""

    vectors: object
    records: list[dict]
    encoder: object

    @classmethod
    def load(cls, directory: Path, encoder: object) -> DenseIndex:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("dense retrieval requires optional numpy") from exc
        vectors = np.load(directory / "vectors.npz")["vectors"]
        records = json.loads((directory / "records.json").read_text(encoding="utf-8"))
        return cls(vectors, records, encoder)

    def search(self, question: str, limit: int = 8, *, issuer_inn: str | None = None,
               isin: str | None = None, source_scope: str = "all",
               doc_type: str | None = None) -> list[SearchHit]:
        if not self.records or limit <= 0:
            return []
        if source_scope not in {"all", "official"}:
            raise ValueError("source_scope must be 'all' or 'official'")
        eligible = [i for i, record in enumerate(self.records)
                    if (not issuer_inn or issuer_inn == "unknown"
                        or record.get("issuer_inn") == issuer_inn)
                    and (not isin or isin == "unknown" or record.get("isin") == isin)
                    and (not doc_type or doc_type == "all" or record.get("doc_type") == doc_type)
                    and (source_scope != "official" or record.get("authority_level") in {
                        "official", "issuer_disclosure", "financial_source", "rating_source"
                    })]
        if not eligible:
            return []
        import numpy as np

        query = np.asarray(self.encoder.encode([question])[0], dtype="float32")
        query_norm = np.linalg.norm(query)
        if query_norm:
            query = query / query_norm
        vectors = np.asarray(self.vectors, dtype="float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms != 0)
        scores = vectors[eligible] @ query
        order = scores.argsort()[::-1]
        hits = []
        seen_pages: set[tuple[str, int]] = set()
        for index in order:
            record = self.records[eligible[int(index)]]
            page = (record["doc_sha256"], record["page_no"])
            if page in seen_pages:
                continue
            seen_pages.add(page)
            hits.append(SearchHit(
                record["chunk_id"], record["doc_sha256"], record["page_no"],
                record["section"], record["text"], float(scores[index]),
                record["source_kind"], record["authority_level"],
            ))
            if len(hits) >= limit:
                break
        return hits


def build_index(con, directory: Path, encoder: object) -> int:
    """Encode all indexed chunks into a new dense index directory."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("dense retrieval requires optional numpy") from exc
    rows = con.execute(
        """SELECT c.chunk_id,c.doc_sha256,c.page_no,c.section,c.text,
                  d.source_kind,d.authority_level,d.issuer_inn,d.isin,d.doc_type
           FROM rag_chunks c JOIN rag_documents d ON d.doc_sha256=c.doc_sha256
           ORDER BY c.chunk_id"""
    ).fetchall()
    records = [{"chunk_id": row[0], "doc_sha256": row[1], "page_no": row[2],
                "section": row[3] or "", "text": row[4],
                "source_kind": row[5] or "unknown",
                "authority_level": row[6] or "unknown", "issuer_inn": row[7],
                "isin": row[8], "doc_type": row[9]} for row in rows]
    vectors = (np.asarray(encoder.encode([record["text"] for record in records]), dtype="float32")
               if records else np.empty((0, 0), dtype="float32"))
    if records and (vectors.ndim != 2 or vectors.shape[0] != len(records)):
        raise ValueError("encoder returned an invalid vector matrix")
    if records:
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms != 0)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        raise FileExistsError("dense index output already exists; choose a new version path")
    temporary = Path(tempfile.mkdtemp(prefix=f"{directory.name}.", suffix=".tmp",
                                      dir=directory.parent))
    try:
        np.savez_compressed(temporary / "vectors.npz", vectors=vectors)
        (temporary / "records.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(directory)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return len(records)
