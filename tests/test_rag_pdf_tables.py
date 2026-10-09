import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from hybond.rag import ingest
from hybond.rag.ingest import _extract_pdf_tables, _read_document, ingest_file
from hybond.rag.manifest import load_manifest, validate_local_files
from hybond.rag.search import search
from hybond.store import db

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval/corpus/manifest_doc_fidelity_v1.jsonl"


def test_layout_text_extracts_cbr_numeric_rows_without_flattening_them():
    page = (
        "Прогноз Банка России\n"
        "                         2025             2026             2027\n"
        "                         (факт)\n"
        "Инфляция, декабрь к декабрю             5,6              4,5–5,5          4,0\n"
        "Ключевая ставка, средняя за год          19,2             14,0–14,5        9,0\n"
        "\n"
        "Обычный текст с числом 2026 года.\n"
    )
    prose, tables = _extract_pdf_tables(page)

    assert len(tables) == 1
    assert tables[0]["rows"][0][0]["text"] == "Показатель"
    assert [cell["text"] for cell in tables[0]["rows"][0]] == [
        "Показатель", "2025", "2026", "2027",
    ]
    assert tables[0]["rows"][2][2]["text"] == "14,0–14,5"
    assert "Инфляция, декабрь к декабрю" not in prose
    assert "Обычный текст с числом 2026 года." in prose


def test_pdftotext_terminal_form_feed_is_not_an_extra_page(tmp_path, monkeypatch):
    source = tmp_path / "one-page.pdf"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        ingest.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="one page text\f"),
    )

    pages, tables_by_page = _read_document(source)

    assert pages == ["one page text"]
    assert tables_by_page == [[]]


def test_cbr_pdf_table_fixture_is_structured_and_page_cited(tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("poppler pdftotext is required for PDF fixture extraction")
    documents = load_manifest(MANIFEST)
    validate_local_files(documents, ROOT)
    source_doc = next(doc for doc in documents if "rasterized" not in doc.local_path)
    source = ROOT / source_doc.local_path
    con = db.connect(tmp_path / "cbr-pdf.sqlite")
    db.init_db(con)
    try:
        result = ingest_file(
            con, source, doc_type=source_doc.doc_type,
            source_url=source_doc.source_url, source_kind="cbr_document",
            authority_level="central_bank",
        )
        assert result["pages"] == 1
        tables = con.execute(
            "SELECT page_no,text,table_html FROM rag_elements WHERE doc_sha256=? AND kind='table'",
            (result["doc_sha256"],),
        ).fetchall()
        assert len(tables) == 2
        assert all(row["page_no"] == 1 for row in tables)
        first_rows = json.loads(tables[0]["text"])
        rate_row = next(
            row for row in first_rows
            if row[0]["text"].startswith("Ключевая ставка, в среднем за год")
        )
        assert rate_row[2]["text"] == "14,0–14,51"
        assert tables[0]["table_html"].startswith("<table><tr><th>")
        table_chunks = con.execute(
            "SELECT section,text,page_no FROM rag_chunks WHERE doc_sha256=? AND section LIKE 'table_%'",
            (result["doc_sha256"],),
        ).fetchall()
        assert len(table_chunks) == 2
        assert all(row["page_no"] == 1 for row in table_chunks)
        assert any("Ключевая ставка, в среднем за год" in row["text"] for row in table_chunks)
        prose = " ".join(row[0] for row in con.execute(
            "SELECT text FROM rag_chunks WHERE doc_sha256=? AND section NOT LIKE 'table_%'",
            (result["doc_sha256"],),
        ))
        assert "14,0–14,51" not in prose
        hits = search(con, "ключевая ставка в среднем за год", doc_type="cbr_document")
        assert any(hit.section.startswith("table_") for hit in hits)
    finally:
        con.close()


def test_rasterized_pdf_fixture_is_detected_as_scanned_without_fabricating_text(tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("poppler pdftotext is required for PDF fixture extraction")
    documents = load_manifest(MANIFEST)
    validate_local_files(documents, ROOT)
    scan_doc = next(doc for doc in documents if "rasterized" in doc.local_path)
    con = db.connect(tmp_path / "cbr-scan.sqlite")
    db.init_db(con)
    try:
        result = ingest_file(
            con, ROOT / scan_doc.local_path, doc_type=scan_doc.doc_type,
            source_url=scan_doc.source_url, source_kind="cbr_document",
            authority_level="central_bank",
        )
        assert result["pages"] == 1
        assert con.execute(
            "SELECT is_scanned FROM rag_pages WHERE doc_sha256=? AND page_no=1",
            (result["doc_sha256"],),
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM rag_chunks WHERE doc_sha256=?",
            (result["doc_sha256"],),
        ).fetchone()[0] == 0
    finally:
        con.close()
