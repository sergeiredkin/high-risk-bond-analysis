"""Lightweight, offline-first document ingestion for the RAG baseline."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import warnings
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

from .financial_reports import extract_nalog_reports, replace_nalog_reports
from .manifest import sha256_file
from .registry import add_chunk, upsert_document


def extract_metadata(path: Path, *, text: str | None = None) -> dict[str, str | None]:
    """Extract conservative issuer/ISIN metadata from an HTML or text source.

    Args:
        path: Source file used when pre-extracted text is not provided.
        text: Optional visible document text to avoid parsing the file twice.
    """
    if text is None:
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


class _LayoutHTMLParser(HTMLParser):
    """Extract visible HTML text while retaining row and cell boundaries."""

    _BLOCKS: ClassVar[frozenset[str]] = frozenset({
        "address", "article", "blockquote", "div", "h1", "h2", "h3",
        "h4", "li", "p", "section",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.table_depth = 0
        self.row_cells = 0
        self.tables: list[dict] = []
        self.table_stack: list[dict] = []

    @staticmethod
    def _finish_cell(table: dict) -> None:
        """Append the active cell to its row, tolerating omitted end tags."""
        cell = table.get("cell")
        row = table.get("row")
        if cell is not None and row is not None:
            row.append({
                "tag": cell["tag"],
                "text": " ".join("".join(cell["parts"]).split()),
                "colspan": cell["colspan"],
                "rowspan": cell["rowspan"],
            })
        table["cell"] = None

    @staticmethod
    def _finish_row(table: dict) -> None:
        """Finalize the active row and any implicitly closed final cell."""
        if table.get("cell") is not None:
            _LayoutHTMLParser._finish_cell(table)
        if table.get("row"):
            table["rows"].append(table["row"])
        table["row"] = None

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "table":
            self.table_depth += 1
            self.table_stack.append({"rows": [], "row": None, "cell": None})
            self.tables.append(self.table_stack[-1])
            self.parts.append("\n")
        elif tag == "tr":
            self.row_cells = 0
            if self.table_stack:
                self._finish_row(self.table_stack[-1])
                self.table_stack[-1]["row"] = []
            self.parts.append("\n")
        elif tag in {"td", "th"} and self.table_stack:
            if self.row_cells:
                self.parts.append("\t")
            self.row_cells += 1
            attrs = dict(_attrs)
            self._finish_cell(self.table_stack[-1])
            self.table_stack[-1]["row"] = self.table_stack[-1]["row"] or []
            self.table_stack[-1]["cell"] = {
                "tag": tag,
                "colspan": _positive_int(attrs.get("colspan")),
                "rowspan": _positive_int(attrs.get("rowspan")),
                "parts": [],
            }
        elif tag == "br" or tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"td", "th"} and self.table_stack:
            self._finish_cell(self.table_stack[-1])
        elif tag == "tr":
            if self.table_stack:
                self._finish_row(self.table_stack[-1])
            self.parts.append("\n")
        elif tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            if self.table_stack:
                self._finish_row(self.table_stack[-1])
                self.table_stack.pop()
            self.parts.append("\n")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.table_stack:
            cell = self.table_stack[-1]["cell"]
            if cell is not None:
                cell["parts"].append(data)
        else:
            self.parts.append(data)


def _positive_int(value: str | None) -> int:
    """Return a positive HTML span value, defaulting malformed values to one."""
    try:
        parsed = int(value or "1")
    except ValueError:
        return 1
    return parsed if parsed > 0 else 1


def _table_chunk_text(table: dict) -> str:
    """Format table rows as searchable text without merging them into prose."""
    return "\n".join(
        "\t".join(cell["text"] for cell in row)
        for row in table["rows"]
        if any(cell["text"] for cell in row)
    )


def _table_html(table: dict) -> str:
    """Serialize extracted cell structure to safe, canonical table HTML."""
    rows = []
    for row in table["rows"]:
        cells = []
        for cell in row:
            attrs = "".join(
                f' {name}="{cell[name]}"' for name in ("colspan", "rowspan")
                if cell[name] != 1
            )
            cells.append(f"<{cell['tag']}{attrs}>{escape(cell['text'])}</{cell['tag']}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _extract_pdf_tables(page: str) -> tuple[str, list[dict]]:
    """Separate aligned PDF table rows from prose using pdftotext layout spacing.

    This conservative fallback recognizes rows with at least four aligned cells
    and three numeric cells. It preserves source cell strings but does not
    infer merged-cell semantics, units, or column meaning.
    """
    lines = page.splitlines()
    candidates: list[tuple[int, list[str]]] = []
    for line_no, line in enumerate(lines):
        cells = [cell.strip() for cell in re.split(r"[ \t]{2,}", line.strip()) if cell.strip()]
        numeric_cells = sum(bool(re.search(r"\d", cell)) for cell in cells[1:])
        if len(cells) >= 4 and numeric_cells >= 3:
            candidates.append((line_no, cells))

    groups: list[list[tuple[int, list[str]]]] = []
    for candidate in candidates:
        if not groups or candidate[0] != groups[-1][-1][0] + 1:
            groups.append([])
        groups[-1].append(candidate)

    tables = []
    removed: set[int] = set()
    for group in groups:
        start = group[0][0]
        cell_count = len(group[0][1])
        header_lines = [
            line for line in lines[max(0, start - 4):start]
            if re.fullmatch(r"[\s0-9()A-Za-zА-Яа-я/–—-]+", line)
            and re.search(r"\b20\d{2}\b", line)
        ]
        years = list(dict.fromkeys(
            year for line in header_lines for year in re.findall(r"\b20\d{2}\b", line)
        ))
        if len(group) < 2 and len(years) != cell_count - 1:
            continue
        rows = []
        if len(years) == cell_count - 1:
            rows.append([
                {"tag": "th", "text": label, "colspan": 1, "rowspan": 1}
                for label in ("Показатель", *years)
            ])
        for line_no, cells in group:
            removed.add(line_no)
            rows.append([
                {"tag": "td", "text": cell, "colspan": 1, "rowspan": 1}
                for cell in cells
            ])
        tables.append({"rows": rows})

    prose = "\n".join(line for index, line in enumerate(lines) if index not in removed)
    return prose, tables


def _read_document(path: Path) -> tuple[list[str], list[list[dict]]]:
    """Read page text and structured HTML tables, using pdftotext for PDFs.

    Raises:
        ValueError: If PDF text extraction is unavailable or fails.
    """
    tables: list[dict] = []
    is_pdf = path.suffix.lower() == ".pdf"
    if is_pdf:
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=True, capture_output=True, text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"cannot extract PDF text from {path}") from exc
        text = result.stdout
        # pdftotext emits one final form-feed as a page terminator. Remove only
        # that sentinel so a genuinely blank final page is still represented.
        text = text.removesuffix("\f")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in {".html", ".htm"}:
            parser = _LayoutHTMLParser()
            parser.feed(text)
            parser.close()
            tables = [table for table in parser.tables if table["rows"]]
            text = "".join(parser.parts)
            text = re.sub(r" +", " ", text)
            text = re.sub(r" *\n *", "\n", text)
    raw_pages = text.replace("\r\n", "\n").split("\f")
    if is_pdf:
        pages = []
        tables_by_page = []
        for raw_page in raw_pages:
            prose, page_tables = _extract_pdf_tables(raw_page)
            pages.append(prose.strip())
            tables_by_page.append(page_tables)
        return pages or [""], tables_by_page or [[]]
    pages = [page.strip() for page in raw_pages] or [""]
    tables_by_page = [tables if index == 0 else [] for index in range(len(pages))]
    return pages, tables_by_page


def _ocr_pdf_page(path: Path, page_no: int) -> tuple[str, float | None]:
    """Optionally OCR one blank PDF page with local Tesseract.

    OCR is disabled unless ``HYBOND_RAG_OCR=1``. It requires local
    ``pdftoppm``, ``tesseract``, and the configured language data. Returned
    confidence is the mean word confidence normalized to ``[0, 1]``.
    """
    enabled = os.environ.get("HYBOND_RAG_OCR", "").strip().casefold() in {"1", "true", "yes"}
    if not enabled:
        return "", None
    missing = [tool for tool in ("pdftoppm", "tesseract") if shutil.which(tool) is None]
    if missing:
        warnings.warn(
            f"local RAG OCR requested but tools are unavailable: {', '.join(missing)}",
            RuntimeWarning, stacklevel=2,
        )
        return "", None

    language = os.environ.get("HYBOND_RAG_OCR_LANGUAGE", "rus+eng")
    try:
        with tempfile.TemporaryDirectory(prefix="hybond-rag-ocr-") as tmp:
            image_prefix = Path(tmp) / "page"
            subprocess.run(
                ["pdftoppm", "-f", str(page_no), "-l", str(page_no),
                 "-singlefile", "-r", "200", "-png", str(path), str(image_prefix)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            image_path = image_prefix.with_suffix(".png")
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", language,
                 "--psm", "6", "tsv"],
                check=True, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.warn(f"local RAG OCR failed for {path} page {page_no}: {exc}",
                      RuntimeWarning, stacklevel=2)
        return "", None

    lines: dict[tuple[str, str, str], list[tuple[int, int, str]]] = {}
    confidences: list[float] = []
    for order, row in enumerate(csv.DictReader(io.StringIO(result.stdout), delimiter="\t")):
        word = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf", "-1"))
            left = int(row.get("left", order))
        except (TypeError, ValueError):
            continue
        if word and confidence >= 0:
            line_id = tuple((row.get(field) or "0") for field in
                            ("block_num", "par_num", "line_num"))
            lines.setdefault(line_id, []).append((left, order, word))
            confidences.append(confidence)
    if not confidences:
        return "", None
    text_lines = [
        " ".join(word for _left, _order, word in sorted(words))
        for words in lines.values()
    ]
    return "\n".join(text_lines), round(sum(confidences) / len(confidences) / 100, 4)


def _read_pages(path: Path) -> list[str]:
    """Read a document as page text, retaining a compatibility helper for callers."""
    return _read_document(path)[0]


def _chunks(page: str, max_chars: int = 1800) -> list[tuple[str, str]]:
    """Split pages into bounded paragraph chunks, preserving long records."""
    section = ""
    result: list[tuple[str, str]] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page) if p.strip()]
    buffer = ""
    for paragraph in paragraphs:
        if paragraph.startswith("#"):
            section = paragraph.lstrip("# ").strip()
        if len(paragraph) > max_chars:
            if buffer:
                result.append((section, buffer))
                buffer = ""
            start = 0
            while start < len(paragraph):
                end = min(start + max_chars, len(paragraph))
                if end < len(paragraph):
                    boundary = paragraph.rfind(" ", start + max_chars // 2, end)
                    if boundary > start:
                        end = boundary
                result.append((section, paragraph[start:end]))
                start = end
            continue
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
                source_kind: str = "unknown", authority_level: str = "unknown",
                catalog_url: str | None = None, source_title: str | None = None,
                published_date: str | None = None, identity_check: str | None = None,
                fetched_at: str | None = None) -> dict:
    """Register and index one document idempotently by content hash.

    Returns a summary containing the document hash, page count, and chunk count.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = sha256_file(path)
    pages, tables_by_page = _read_document(path)
    scanned_pages = [not page.strip() for page in pages]
    ocr_confidences: list[float | None] = [None] * len(pages)
    ocr_texts: list[str | None] = [None] * len(pages)
    if path.suffix.lower() == ".pdf":
        for index, is_scanned in enumerate(scanned_pages):
            if is_scanned:
                ocr_text, confidence = _ocr_pdf_page(path, index + 1)
                if ocr_text:
                    ocr_texts[index] = ocr_text
                    ocr_confidences[index] = confidence
    document_text = " ".join(pages)
    metadata = extract_metadata(path, text=document_text)
    source_kind, authority_level = classify_source(document_text, source_kind, authority_level)
    isin = isin or metadata["isin"]
    issuer_inn = issuer_inn or metadata["issuer_inn"]
    if not issuer_inn:
        issuer_inn, _ = infer_issuer(con, document_text)
    upsert_document(con, {
        "doc_sha256": digest, "isin": isin, "issuer_inn": issuer_inn,
        "doc_type": doc_type, "source_url": source_url,
        "catalog_url": catalog_url, "source_title": source_title,
        "published_date": published_date, "identity_check": identity_check,
        "fetched_at": fetched_at,
        "local_path": str(path), "page_count": len(pages), "parse_status": "parsed",
        "source_kind": source_kind, "authority_level": authority_level,
    })
    if doc_type == "nalog_financials":
        replace_nalog_reports(con, digest, extract_nalog_reports(path))
    con.execute("DELETE FROM rag_pages WHERE doc_sha256 = ?", (digest,))
    con.execute("DELETE FROM rag_elements WHERE doc_sha256 = ?", (digest,))
    con.execute("DELETE FROM rag_chunks WHERE doc_sha256 = ?", (digest,))
    con.execute("DELETE FROM rag_chunks_fts WHERE chunk_id LIKE ?", (digest + ":%",))
    chunk_count = 0
    for page_no, page in enumerate(pages, 1):
        scanned = int(scanned_pages[page_no - 1])
        con.execute(
            """INSERT INTO rag_pages(doc_sha256,page_no,is_scanned,ocr_confidence)
               VALUES(?,?,?,?)""",
            (digest, page_no, scanned, ocr_confidences[page_no - 1]),
        )
        if ocr_texts[page_no - 1]:
            con.execute(
                """INSERT INTO rag_elements(doc_sha256,page_no,kind,text)
                   VALUES(?,?,'ocr_text',?)""",
                (digest, page_no, ocr_texts[page_no - 1]),
            )
        for table in tables_by_page[page_no - 1]:
            rows_json = json.dumps(table["rows"], ensure_ascii=False, separators=(",", ":"))
            con.execute(
                """INSERT INTO rag_elements(doc_sha256,page_no,kind,text,table_html)
                   VALUES(?,?,'table',?,?)""",
                (digest, page_no, rows_json, _table_html(table)),
            )
        page_chunks = _chunks(page)
        for ordinal, (section, text) in enumerate(page_chunks):
            chunk_id = f"{digest}:{page_no}:{ordinal}"
            add_chunk(con, {
                "chunk_id": chunk_id, "doc_sha256": digest, "page_no": page_no,
                "section": section, "text": text,
                "chunk_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
            chunk_count += 1
        next_ordinal = len(page_chunks)
        for table_index, table in enumerate(tables_by_page[page_no - 1], 1):
            table_text = _table_chunk_text(table)
            if not table_text:
                continue
            chunk_id = f"{digest}:{page_no}:{next_ordinal}"
            add_chunk(con, {
                "chunk_id": chunk_id, "doc_sha256": digest, "page_no": page_no,
                "section": f"table_{table_index}", "text": table_text,
                "chunk_hash": hashlib.sha256(table_text.encode("utf-8")).hexdigest(),
            })
            chunk_count += 1
            next_ordinal += 1
    con.commit()
    return {"doc_sha256": digest, "pages": len(pages), "chunks": chunk_count}
