"""Validation helpers for the versioned RAG corpus manifest."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED = {"doc_sha256", "isin", "issuer_inn", "doc_type", "source_url", "fetched_at", "local_path"}


@dataclass(frozen=True)
class CorpusDocument:
    """One locally available source document in the RAG corpus."""

    doc_sha256: str
    isin: str
    issuer_inn: str
    doc_type: str
    source_url: str
    fetched_at: str
    local_path: str
    disclosure_id: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    source_url_basis: str | None = None


def _parse_record(raw: object, line_no: int) -> CorpusDocument:
    """Validate and convert one JSONL manifest record.

    Raises:
        ValueError: if the record is malformed or missing required fields.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"manifest line {line_no}: expected an object")
    missing = _REQUIRED - raw.keys()
    if missing:
        raise ValueError(f"manifest line {line_no}: missing {sorted(missing)}")
    values = {key: raw[key] for key in _REQUIRED}
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError(f"manifest line {line_no}: required fields must be non-empty strings")
    optional_names = ("disclosure_id", "published_at", "retrieved_at", "source_url_basis")
    optional = {key: raw.get(key) for key in optional_names}
    if any(value is not None and (not isinstance(value, str) or not value.strip())
           for value in optional.values()):
        raise ValueError(f"manifest line {line_no}: optional provenance fields must be non-empty strings")
    for key in ("published_at", "retrieved_at"):
        if optional[key] is not None:
            try:
                date.fromisoformat(optional[key])
            except ValueError as exc:
                raise ValueError(f"manifest line {line_no}: {key} must be an ISO date") from exc
    digest = values["doc_sha256"].lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"manifest line {line_no}: doc_sha256 must be 64 lowercase hex characters")
    return CorpusDocument(**{**values, **optional, "doc_sha256": digest})


def load_manifest(path: Path) -> list[CorpusDocument]:
    """Load and validate a JSONL corpus manifest.

    Blank lines are ignored. Duplicate document hashes are rejected so an
    index cannot silently associate one binary with conflicting metadata.
    """
    documents: list[CorpusDocument] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest line {line_no}: invalid JSON") from exc
        document = _parse_record(raw, line_no)
        if document.doc_sha256 in seen:
            raise ValueError(f"manifest line {line_no}: duplicate doc_sha256 {document.doc_sha256}")
        seen.add(document.doc_sha256)
        documents.append(document)
    return documents


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of a file without loading it all in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_local_files(documents: Iterable[CorpusDocument], root: Path | None = None) -> None:
    """Ensure manifest paths exist and match their declared hashes.

    Raises:
        FileNotFoundError: if a referenced file does not exist.
        ValueError: if a file's digest differs from the manifest.
    """
    for document in documents:
        path = Path(document.local_path)
        if root is not None and not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256_file(path)
        if actual != document.doc_sha256:
            raise ValueError(f"{path}: sha256 mismatch ({actual} != {document.doc_sha256})")
