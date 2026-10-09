import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from hybond.rag import ingest
from hybond.rag.eval.runner import run
from hybond.rag.golden import load_golden
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


def test_synthetic_scan_surrogate_is_detected_but_not_indexed(tmp_path, monkeypatch):
    if not shutil.which("pdftotext"):
        pytest.skip("poppler pdftotext is required for PDF fixture extraction")
    monkeypatch.delenv("HYBOND_RAG_OCR", raising=False)
    source = ROOT / "eval/corpus/fixtures_doc_fidelity_v1/cbr/comment_page2_synthetic_scan.pdf"
    pages, tables = _read_document(source)
    assert pages == [""]
    assert tables == [[]]

    con = db.connect(tmp_path / "synthetic-scan.sqlite")
    db.init_db(con)
    try:
        result = ingest_file(con, source, doc_type="synthetic_scan_test")
        assert result["pages"] == 1
        page = con.execute(
            "SELECT is_scanned,ocr_confidence FROM rag_pages WHERE doc_sha256=?",
            (result["doc_sha256"],),
        ).fetchone()
        assert tuple(page) == (1, None)
        assert con.execute(
            "SELECT COUNT(*) FROM rag_chunks WHERE doc_sha256=?",
            (result["doc_sha256"],),
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_pdf_table_candidate_quotes_and_document_disjoint_splits(tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("poppler pdftotext is required for PDF fixture extraction")
    documents = load_manifest(MANIFEST)
    validate_local_files(documents, ROOT)
    golden_path = ROOT / "eval/golden/ru_pdf_tables_candidate_v1.jsonl"
    questions = load_golden(golden_path, documents)
    assert len(questions) == 4
    assert all(question.category == "macro_forecast" for question in questions)
    evidence_by_split = {
        split: {item["doc_sha256"] for question in questions if question.split == split
                for item in question.evidence}
        for split in ("dev", "test")
    }
    assert not evidence_by_split["dev"] & evidence_by_split["test"]

    con = db.connect(tmp_path / "pdf-golden.sqlite")
    db.init_db(con)
    try:
        for document in documents:
            ingest_file(
                con, ROOT / document.local_path, doc_type=document.doc_type,
                source_url=document.source_url, source_kind="cbr_document",
                authority_level="central_bank",
            )
        for question in questions:
            for evidence in question.evidence:
                chunks = con.execute(
                    "SELECT section,text FROM rag_chunks WHERE doc_sha256=? AND page_no=?",
                    (evidence["doc_sha256"], evidence["page"]),
                ).fetchall()
                assert any(evidence["quote"] in row["text"] for row in chunks)
                assert any(row["section"].startswith("table_") for row in chunks)
        for split in ("dev", "test"):
            report = run(con, golden_path, MANIFEST, k=5, split=split)
            assert report["question_count"] == 2
            assert report["metrics"]["manual_review_answers"] == 2
            assert all(isinstance(result["page_recall"], float) for result in report["results"])
    finally:
        con.close()


def test_ocr_is_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("HYBOND_RAG_OCR", raising=False)
    monkeypatch.setattr(ingest.shutil, "which", lambda _tool: pytest.fail("OCR tool lookup should not run"))

    assert ingest._ocr_pdf_page(Path("scan.pdf"), 1) == ("", None)


def test_optional_ocr_parses_local_tesseract_tsv_and_confidence(tmp_path, monkeypatch):
    source = ROOT / "eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424_page1_rasterized.pdf"
    monkeypatch.setenv("HYBOND_RAG_OCR", "1")
    monkeypatch.setattr(ingest.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fake_run(command, **_kwargs):
        if command[0] == "pdftoppm":
            Path(command[-1]).with_suffix(".png").write_bytes(b"test image")
            return SimpleNamespace(stdout="")
        assert command[0] == "tesseract"
        assert command[-1] == "tsv"
        headers = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
                   "left", "top", "width", "height", "conf", "text"]
        tab = chr(9)
        tsv = chr(10).join((
            tab.join(headers),
            tab.join(["5", "1", "1", "1", "1", "1", "0", "0", "10", "10", "95.0", "Ставка"]),
            tab.join(["5", "1", "1", "1", "1", "2", "10", "0", "10", "10", "85.0", "14,0–14,5"]),
        ))
        return SimpleNamespace(stdout=tsv)

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    text, confidence = ingest._ocr_pdf_page(source, 1)

    assert text == "Ставка 14,0–14,5"
    assert confidence == 0.9


def test_ocr_requested_without_local_tools_warns_and_abstains(monkeypatch):
    monkeypatch.setenv("HYBOND_RAG_OCR", "1")
    monkeypatch.setattr(ingest.shutil, "which", lambda _tool: None)

    with pytest.warns(RuntimeWarning, match="tools are unavailable"):
        result = ingest._ocr_pdf_page(Path("scan.pdf"), 1)
    assert result == ("", None)


def test_ingest_stores_ocr_text_confidence_and_scan_provenance(tmp_path, monkeypatch):
    source = ROOT / "eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424_page1_rasterized.pdf"
    monkeypatch.setenv("HYBOND_RAG_OCR", "1")
    monkeypatch.setattr(ingest, "_read_document", lambda _path: ([""], [[]]))
    monkeypatch.setattr(ingest, "_ocr_pdf_page", lambda _path, _page: ("Распознанная ставка 14,5", 0.9))
    con = db.connect(tmp_path / "ocr.sqlite")
    db.init_db(con)
    try:
        result = ingest_file(con, source, doc_type="cbr_document")
        page = con.execute(
            "SELECT is_scanned,ocr_confidence FROM rag_pages WHERE doc_sha256=? AND page_no=1",
            (result["doc_sha256"],),
        ).fetchone()
        assert tuple(page) == (1, 0.9)
        assert con.execute(
            "SELECT text FROM rag_elements WHERE doc_sha256=? AND kind='ocr_text'",
            (result["doc_sha256"],),
        ).fetchone()[0] == "Распознанная ставка 14,5"
        assert con.execute(
            "SELECT COUNT(*) FROM rag_chunks WHERE doc_sha256=?",
            (result["doc_sha256"],),
        ).fetchone()[0] == 0
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
