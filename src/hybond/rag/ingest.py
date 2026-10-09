"""Lightweight, offline-first document ingestion for the RAG baseline."""
from __future__ import annotations

import hashlib
import html
import re
import subprocess
from pathlib import Path

from .manifest import sha256_file
from .registry import add_chunk, upsert_document


def extract_metadata(path: Path) -> dict[str, str | None]:
    """Extract conservative issuer/ISIN metadata from an HTML or text source."""
    text = " ".join(_read_pages(path))
    inn_match = re.search(r"ИНН\s*[:№]?\s*(\d{10})", text, re.IGNORECASE)
    isin_match = re.search(r"\b(RU[0-9A-Z]{10})\b", text)
    issuer_match = re.search(
        r"(?i)(?:общество с ограниченной ответственностью|акционерное общество|ООО|АО)"
        r"[^/\n]{2,180}?\s*\(ИНН\s*[:№]?\s*\d{10}\)", text,
    )
    issuer = issuer_match.group(0).rsplit("(ИНН", 1)[0].strip() if issuer_match else None
    return {"issuer_inn": inn_match.group(1) if inn_match else None,
            "isin": isin_match.group(1) if isin_match else None,
            "issuer_name": issuer}


def _read_pages(path: Path) -> list[str]:
    """Read a text-like document or a PDF with the system pdftotext tool."""
    if path.suffix.lower() == ".pdf":
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=True, capture_output=True, text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"cannot extract PDF text from {path}") from exc
        text = result.stdout
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in {".html", ".htm"}:
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", html.unescape(text))
            text = re.sub(r"[ \t]+", " ", text)
    pages = [page.strip() for page in text.replace("\r\n", "\n").split("\f")]
    return pages or [""]


def _chunks(page: str, max_chars: int = 1800) -> list[tuple[str, str]]:
    """Split a page into paragraph chunks, preserving a markdown heading."""
    section = ""
    result: list[tuple[str, str]] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page) if p.strip()]
    buffer = ""
    for paragraph in paragraphs:
        if paragraph.startswith("#"):
            section = paragraph.lstrip("# ").strip()
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if buffer and len(candidate) > max_chars:
            result.append((section, buffer))
            buffer = paragraph
        else:
            buffer = candidate
    if buffer:
        result.append((section, buffer))
    return result


def classify_source(text: str, requested_kind: str, requested_authority: str) -> tuple[str, str]:
    """Detect secondary articles in e-disclosure feeds, not unrelated corpora."""
    if requested_kind != "edisclosure":
        return requested_kind, requested_authority
    lowered = text.casefold()
    secondary_markers = ("smart-lab", "владислав кофанов", "сделки прошлой недели",
                         "облигации самолета принесли", "max.ru/channel_")
    if any(marker in lowered for marker in secondary_markers):
        return "market_commentary", "secondary_commentary"
    return requested_kind, requested_authority


def infer_issuer(con, text: str) -> tuple[str | None, str | None]:
    """Infer one issuer from known names when a document omits an INN."""
    normalized = text.casefold().replace("ё", "е")
    candidates = []
    for row in con.execute("SELECT issuer_id, name FROM issuer"):
        name = row[1].casefold().replace("ё", "е")
        if len(name) >= 5 and name in normalized:
            candidates.append((len(name), row[0], row[1]))
    if not candidates:
        return None, None
    _, issuer_id, name = max(candidates)
    return issuer_id, name


def ingest_file(con, path: Path, *, doc_type: str = "unknown", isin: str | None = None,
                issuer_inn: str | None = None, source_url: str | None = None,
                source_kind: str = "unknown", authority_level: str = "unknown") -> dict:
    """Register and index one document idempotently by content hash.

    Returns a summary containing the document hash, page count, and chunk count.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = sha256_file(path)
    pages = _read_pages(path)
    metadata = extract_metadata(path)
    source_kind, authority_level = classify_source(" ".join(pages), source_kind, authority_level)
    isin = isin or metadata["isin"]
    issuer_inn = issuer_inn or metadata["issuer_inn"]
    if not issuer_inn:
        issuer_inn, _ = infer_issuer(con, " ".join(pages))
    upsert_document(con, {
        "doc_sha256": digest, "isin": isin, "issuer_inn": issuer_inn,
        "doc_type": doc_type, "source_url": source_url,
        "fetched_at": None, "local_path": str(path),
        "page_count": len(pages), "parse_status": "parsed",
        "source_kind": source_kind, "authority_level": authority_level,
    })
    con.execute("DELETE FROM rag_pages WHERE doc_sha256 = ?", (digest,))
    con.execute("DELETE FROM rag_chunks WHERE doc_sha256 = ?", (digest,))
    con.execute("DELETE FROM rag_chunks_fts WHERE chunk_id LIKE ?", (digest + ":%",))
    chunk_count = 0
    for page_no, page in enumerate(pages, 1):
        scanned = int(not page.strip())
        con.execute(
            "INSERT INTO rag_pages(doc_sha256,page_no,is_scanned) VALUES(?,?,?)",
            (digest, page_no, scanned),
        )
        for ordinal, (section, text) in enumerate(_chunks(page)):
            chunk_id = f"{digest}:{page_no}:{ordinal}"
            add_chunk(con, {
                "chunk_id": chunk_id, "doc_sha256": digest, "page_no": page_no,
                "section": section, "text": text,
                "chunk_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
            chunk_count += 1
    con.commit()
    return {"doc_sha256": digest, "pages": len(pages), "chunks": chunk_count}
