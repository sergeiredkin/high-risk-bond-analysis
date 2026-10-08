"""Optional content-addressed dense index for local hybrid retrieval."""
from __future__ import annotations

import json
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

    def search(self, question: str, limit: int = 8) -> list[SearchHit]:
        if not self.records or limit <= 0:
            return []
        query = self.encoder.encode([question])[0]
        scores = self.vectors @ query
        order = scores.argsort()[::-1][:limit]
        return [SearchHit(
            record["chunk_id"], record["doc_sha256"], record["page_no"],
            record["section"], record["text"], float(scores[index]),
            record["source_kind"], record["authority_level"],
        ) for index in order for record in [self.records[int(index)]]]


def build_index(con, directory: Path, encoder: object) -> int:
    """Encode all indexed chunks into a new dense index directory."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("dense retrieval requires optional numpy") from exc
    rows = con.execute(
        """SELECT c.chunk_id,c.doc_sha256,c.page_no,c.section,c.text,
                  d.source_kind,d.authority_level
           FROM rag_chunks c JOIN rag_documents d ON d.doc_sha256=c.doc_sha256
           ORDER BY c.chunk_id"""
    ).fetchall()
    records = [{"chunk_id": row[0], "doc_sha256": row[1], "page_no": row[2],
                "section": row[3] or "", "text": row[4],
                "source_kind": row[5] or "unknown",
                "authority_level": row[6] or "unknown"} for row in rows]
    vectors = (np.asarray(encoder.encode([record["text"] for record in records]), dtype="float32")
               if records else np.empty((0, 0), dtype="float32"))
    if records and (vectors.ndim != 2 or vectors.shape[0] != len(records)):
        raise ValueError("encoder returned an invalid vector matrix")
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = directory.with_name(directory.name + ".tmp")
    if temporary.exists() or directory.exists():
        raise FileExistsError("dense index output already exists; choose a new version path")
    temporary.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(temporary / "vectors.npz", vectors=vectors)
    (temporary / "records.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(directory)
    return len(records)
