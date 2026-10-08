"""Optional local dense retrieval adapter.

The dependency-free HyBond install remains usable with FTS5.  Install the
optional RAG stack and provide a local sentence-transformers model to enable
this adapter; no network access is performed by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class LocalDenseEncoder:
    """Encode text with a locally cached sentence-transformers model."""

    model_path: Path

    def __post_init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "dense retrieval requires the optional local RAG dependencies"
            ) from exc
        if not self.model_path.exists():
            raise FileNotFoundError(self.model_path)
        self._model = SentenceTransformer(str(self.model_path))

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Return normalized embeddings for local text input."""
        return self._model.encode(texts, normalize_embeddings=True).tolist()
