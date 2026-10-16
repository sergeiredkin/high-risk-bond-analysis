"""Bounded, manifest-driven acquisition of issuer disclosure PDFs.

This module deliberately does not crawl disclosure catalogs or automate a
browser. A caller supplies official direct file URLs and the issuer mapping;
only allowlisted disclosure hosts are fetched. Downloads are capped, verified,
content-hashed, and indexed with their source provenance.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

ALLOWED_EXACT_HOSTS = frozenset({"e-disclosure.azipi.ru", "e-disclosure.ru"})
ALLOWED_HOST_SUFFIXES = (".e-disclosure.ru",)
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_PDFS_PER_ARCHIVE = 10
MAX_REDIRECTS = 4
MAX_BATCH = 100
MAX_DELAY_SECONDS = 60.0
_USER_AGENT = "HyBond-document-fetcher/1.0 (bounded issuer disclosure import)"


@dataclass(frozen=True)
class ManifestItem:
    """One reviewed issuer-document URL and its catalog provenance."""

    source_url: str
    issuer_inn: str
    title: str = ""
    published_date: str = ""
    catalog_url: str = ""


def _validate_url(url: str) -> str:
    """Return a normalized HTTPS URL if its host is an approved disclosure site."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    allowed = host in ALLOWED_EXACT_HOSTS or any(
        host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise ValueError("URL must be HTTPS on an approved e-disclosure host")
    return url


def load_manifest(path: Path, *, limit: int = 10) -> list[ManifestItem]:
    """Load and validate bounded JSONL source records.

    Each nonblank line must contain ``source_url`` and a ten-digit
    ``issuer_inn``. Optional ``catalog_url``, ``title`` and ISO
    ``published_date`` preserve discovery context.
    """
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH}")
    items: list[ManifestItem] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"manifest line {line_no}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise TypeError(f"manifest line {line_no}: expected a JSON object")
            url = row.get("source_url")
            inn = str(row.get("issuer_inn") or "")
            if not isinstance(url, str) or not url:
                raise ValueError(f"manifest line {line_no}: source_url is required")
            _validate_url(url)
            if not re.fullmatch(r"\d{10}", inn):
                raise ValueError(f"manifest line {line_no}: issuer_inn must be 10 digits")
            catalog_url = row.get("catalog_url") or ""
            if catalog_url:
                if not isinstance(catalog_url, str):
                    raise ValueError(f"manifest line {line_no}: catalog_url must be a string")
                _validate_url(catalog_url)
            published = str(row.get("published_date") or "")
            if published:
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
                        raise ValueError("not an ISO date")
                    date.fromisoformat(published)
                except ValueError as exc:
                    raise ValueError(
                        f"manifest line {line_no}: published_date must be YYYY-MM-DD"
                    ) from exc
            if url in seen:
                raise ValueError(f"manifest line {line_no}: duplicate source_url")
            seen.add(url)
            items.append(ManifestItem(
                source_url=url, issuer_inn=inn,
                title=str(row.get("title") or ""),
                published_date=published, catalog_url=catalog_url,
            ))
            if len(items) > limit:
                raise ValueError(f"manifest contains more than the --limit value ({limit})")
    return items


def _get_bytes(session, url: str, *, max_bytes: int) -> tuple[bytes, str, str]:
    """Fetch one bounded response, validating every redirect before following."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        response = session.get(
            current, stream=True, allow_redirects=False, timeout=(5, 30),
            headers={"User-Agent": _USER_AGENT,
                     "Accept": "application/pdf, application/zip, application/octet-stream"},
        )
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("redirect response has no Location")
                current = urljoin(current, location)
                _validate_url(current)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise ValueError("response exceeds max-bytes limit")
                except ValueError as exc:
                    if str(exc) == "response exceeds max-bytes limit":
                        raise
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise ValueError("response exceeds max-bytes limit")
            return bytes(payload), response.headers.get("Content-Type", ""), current
        finally:
            response.close()
    raise ValueError("too many redirects")


def _pdf_members(payload: bytes) -> list[tuple[str, bytes]]:
    """Return validated PDFs from a PDF response or a bounded ZIP archive."""
    if payload[:1024].find(b"%PDF-") >= 0:
        return [("document.pdf", payload)]
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise ValueError("response is neither a PDF nor a ZIP containing PDFs")
    results = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [info for info in archive.infolist()
                       if not info.is_dir() and info.filename.casefold().endswith(".pdf")]
            if len(members) > MAX_PDFS_PER_ARCHIVE:
                raise ValueError(f"archive contains more than {MAX_PDFS_PER_ARCHIVE} PDFs")
            for info in members:
                if info.file_size < 1 or info.file_size > MAX_UNPACKED_BYTES:
                    raise ValueError("archive member has an invalid uncompressed size")
                total += info.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise ValueError("archive exceeds total uncompressed size limit")
                content = archive.read(info)
                if content[:1024].find(b"%PDF-") < 0:
                    raise ValueError(f"archive member is not a PDF: {Path(info.filename).name}")
                results.append((Path(info.filename).name, content))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid ZIP archive") from exc
    if not results:
        raise ValueError("ZIP archive contains no PDFs")
    return results


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace one corpus artifact atomically so interrupted writes stay invisible."""
    fd, temp_name = tempfile.mkstemp(prefix=".hybond-download-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _verify_pdf_issuer(path: Path, expected_inn: str, *, allow_unverified: bool) -> str:
    """Reject a conflicting printed INN; optionally allow reports omitting one."""
    from .ingest import extract_metadata

    actual = extract_metadata(path).get("issuer_inn")
    if actual and actual != expected_inn:
        raise ValueError(f"PDF issuer INN mismatch: expected {expected_inn}, found {actual}")
    if not actual and not allow_unverified:
        raise ValueError("PDF has no extractable issuer INN; pass --allow-unverified-identity only after checking the catalog")
    return "verified" if actual else "not_printed_catalog_mapping_required"


def fetch_manifest(
    con, items: list[ManifestItem], out_dir: Path, *, limit: int = 10,
    max_bytes: int = DEFAULT_MAX_BYTES, delay_seconds: float = 2.0,
    allow_unverified_identity: bool = False, session=None, sleeper=time.sleep,
) -> dict:
    """Download and index a bounded set of manifest documents.

    Per-item failures are returned in the report and do not stop later items.
    Every accepted PDF is SHA-256 named and indexed with its official download
    URL, optional catalog URL, title, publication date and fetch timestamp.
    """
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH}")
    if not 1024 <= max_bytes <= MAX_RESPONSE_BYTES:
        raise ValueError(f"max_bytes must be between 1024 and {MAX_RESPONSE_BYTES}")
    if not 0 <= delay_seconds <= MAX_DELAY_SECONDS:
        raise ValueError(f"delay_seconds must be between 0 and {MAX_DELAY_SECONDS}")
    selected = items[:limit]
    owns_session = session is None
    import requests
    if owns_session:
        session = requests.Session()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    last_fetch_at: float | None = None
    for item in selected:
        record = {"source_url": item.source_url, "issuer_inn": item.issuer_inn,
                  "status": "failed", "documents": [], "error": None}
        try:
            _validate_url(item.source_url)
            if not re.fullmatch(r"\d{10}", item.issuer_inn):
                raise ValueError("issuer_inn must be 10 digits")
            if item.catalog_url:
                _validate_url(item.catalog_url)
            if last_fetch_at is not None and delay_seconds:
                remaining = delay_seconds - (time.monotonic() - last_fetch_at)
                if remaining > 0:
                    sleeper(remaining)
            last_fetch_at = time.monotonic()
            payload, content_type, final_url = _get_bytes(
                session, item.source_url, max_bytes=max_bytes,
            )
            pdfs = _pdf_members(payload)
            from .ingest import _read_document, ingest_file

            # Validate every archive member before persisting any of them, so a
            # bad issuer identity cannot leave a partially indexed bundle.
            validated = []
            with tempfile.TemporaryDirectory(prefix="hybond-disclosure-") as temp:
                for ordinal, (member_name, pdf_bytes) in enumerate(pdfs, 1):
                    temp_path = Path(temp) / f"{ordinal}-{Path(member_name).name}"
                    temp_path.write_bytes(pdf_bytes)
                    identity = _verify_pdf_issuer(
                        temp_path, item.issuer_inn,
                        allow_unverified=allow_unverified_identity,
                    )
                    _read_document(temp_path)
                    digest = hashlib.sha256(pdf_bytes).hexdigest()
                    validated.append((member_name, pdf_bytes, identity, digest))

            target_dir = out_dir / item.issuer_inn
            for member_name, pdf_bytes, identity, digest in validated:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{digest}.pdf"
                if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    _atomic_write(target, pdf_bytes)
                fetched_at = datetime.now(UTC).isoformat()
                summary = ingest_file(
                    con, target, doc_type="issuer_disclosure_pdf",
                    issuer_inn=item.issuer_inn, source_url=item.source_url,
                    source_kind="edisclosure", authority_level="issuer_disclosure",
                    source_title=item.title or Path(member_name).name,
                    published_date=item.published_date or None,
                    identity_check=identity, catalog_url=item.catalog_url or None,
                    fetched_at=fetched_at,
                )
                metadata = {
                    "source_url": item.source_url, "final_url": final_url,
                    "catalog_url": item.catalog_url or None,
                    "issuer_inn": item.issuer_inn, "title": item.title,
                    "published_date": item.published_date or None,
                    "fetched_at": fetched_at, "content_type": content_type,
                    "source_member": member_name, "sha256": digest,
                    "identity_check": identity, "doc_type": "issuer_disclosure_pdf",
                }
                sidecar = target.with_suffix(".json")
                sidecar_data = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                _atomic_write(sidecar, sidecar_data)
                record["documents"].append({
                    "path": str(target), "sha256": digest,
                    "pages": summary["pages"], "chunks": summary["chunks"],
                    "identity_check": identity,
                })
            record["status"] = "ingested" if record["documents"] else "failed"
        except (requests.exceptions.RequestException, ValueError, RuntimeError,
                OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            con.rollback()
            record["status"] = "partial" if record["documents"] else "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
        results.append(record)
    if owns_session:
        session.close()
    return {
        "requested": len(selected), "ingested": sum(r["status"] == "ingested" for r in results),
        "failed": sum(r["status"] in {"failed", "partial"} for r in results),
        "results": results,
    }
