import io
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

from hybond.__main__ import cmd_docs
from hybond.rag.issuer_downloads import (
    ManifestItem,
    _get_bytes,
    fetch_manifest,
    load_manifest,
)
from hybond.rag.registry import init_registry
from hybond.store import db

PDF_FIXTURE = Path(__file__).parents[1] / "eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424.pdf"
PDF_FIXTURE_2 = Path(__file__).parents[1] / "eval/corpus/fixtures_doc_fidelity_v1/cbr/comment_26022026.pdf"


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {"Content-Length": str(len(body)), "Content-Type": "application/pdf"}
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def _con():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    return con


def _item(url="https://www.e-disclosure.ru/files/report.zip"):
    return ManifestItem(
        source_url=url,
        issuer_inn="7700000000",
        title="Полугодовая отчетность",
        published_date="2026-09-01",
        catalog_url="https://www.e-disclosure.ru/portal/files.aspx?id=1",
    )


def test_load_manifest_validates_url_issuer_date_and_limit(tmp_path):
    manifest = tmp_path / "sources.jsonl"
    manifest.write_text(json.dumps({
        "source_url": "https://e-disclosure.ru/file.pdf",
        "catalog_url": "https://www.e-disclosure.ru/portal/files.aspx?id=1",
        "issuer_inn": "7700000000", "title": "Report",
        "published_date": "2026-09-01",
    }) + "\n", encoding="utf-8")
    items = load_manifest(manifest)
    assert len(items) == 1
    assert items[0].issuer_inn == "7700000000"
    assert items[0].catalog_url.endswith("id=1")

    manifest.write_text(json.dumps({
        "source_url": "https://evil.example/file.pdf", "issuer_inn": "7700000000",
    }) + "\n", encoding="utf-8")
    try:
        load_manifest(manifest)
    except ValueError as exc:
        assert "approved e-disclosure host" in str(exc)
    else:
        raise AssertionError("unapproved source host was accepted")


def test_dry_run_validates_manifest_without_opening_database_or_network(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "sources.jsonl"
    manifest.write_text(json.dumps({
        "source_url": "https://www.e-disclosure.ru/file.pdf", "issuer_inn": "7700000000",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(db, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("dry-run must not open the database")))
    args = SimpleNamespace(docs_cmd="fetch-manifest", dry_run=True,
                           manifest=str(manifest), limit=10)
    assert cmd_docs(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["planned"] == 1


def test_bounded_redirect_rejects_unapproved_host_before_following():
    session = FakeSession([FakeResponse(status=302, headers={"Location": "https://evil.example/file.pdf"})])
    try:
        _get_bytes(session, "https://www.e-disclosure.ru/start", max_bytes=1024 * 1024)
    except ValueError as exc:
        assert "approved e-disclosure host" in str(exc)
    else:
        raise AssertionError("redirect to an unapproved host was followed")
    assert session.urls == ["https://www.e-disclosure.ru/start"]


def test_pdf_download_ingests_hash_and_source_metadata(tmp_path):
    body = PDF_FIXTURE.read_bytes()
    session = FakeSession([FakeResponse(body)])
    con = _con()
    report = fetch_manifest(
        con, [_item("https://www.e-disclosure.ru/report.pdf")], tmp_path / "raw",
        allow_unverified_identity=True, session=session, delay_seconds=0,
    )
    assert report["ingested"] == 1
    assert report["failed"] == 0
    artifact = report["results"][0]["documents"][0]
    row = con.execute(
        "SELECT doc_type,issuer_inn,source_url,catalog_url,source_title,published_date,identity_check,fetched_at "
        "FROM rag_documents WHERE doc_sha256=?", (artifact["sha256"],),
    ).fetchone()
    assert tuple(row[:7]) == (
        "issuer_disclosure_pdf", "7700000000", "https://www.e-disclosure.ru/report.pdf",
        "https://www.e-disclosure.ru/portal/files.aspx?id=1",
        "Полугодовая отчетность", "2026-09-01",
        "not_printed_catalog_mapping_required",
    )
    assert row["fetched_at"]
    sidecar = Path(artifact["path"]).with_suffix(".json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    assert metadata["sha256"] == artifact["sha256"]
    assert metadata["identity_check"] == "not_printed_catalog_mapping_required"
    con.close()


def test_zip_download_extracts_and_indexes_pdf_members(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("rus/report.pdf", PDF_FIXTURE.read_bytes())
        bundle.writestr("eng/report.pdf", PDF_FIXTURE_2.read_bytes())
        bundle.writestr("readme.txt", "ignored")
    session = FakeSession([FakeResponse(archive.getvalue(), headers={
        "Content-Length": str(len(archive.getvalue())), "Content-Type": "application/zip",
    })])
    con = _con()
    report = fetch_manifest(
        con, [_item()], tmp_path / "raw", allow_unverified_identity=True,
        session=session, delay_seconds=0,
    )
    assert report["ingested"] == 1
    assert report["failed"] == 0
    assert len(report["results"][0]["documents"]) == 2
    assert con.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 2
    con.close()


def test_identity_mismatch_rejects_without_index_or_corpus_file(tmp_path, monkeypatch):
    from hybond.rag import issuer_downloads

    monkeypatch.setattr(issuer_downloads, "_verify_pdf_issuer",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            ValueError("PDF issuer INN mismatch: expected 7700000000, found 7811089147")))
    session = FakeSession([FakeResponse(PDF_FIXTURE.read_bytes())])
    con = _con()
    out_dir = tmp_path / "raw"
    report = fetch_manifest(
        con, [_item("https://www.e-disclosure.ru/report.pdf")], out_dir,
        session=session, delay_seconds=0,
    )
    assert report["failed"] == 1
    assert "INN mismatch" in report["results"][0]["error"]
    assert con.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 0
    assert not list(out_dir.rglob("*.pdf"))
    con.close()


def test_http_error_and_size_cap_are_reported_without_ingestion(tmp_path):
    con = _con()
    denied = fetch_manifest(
        con, [_item("https://www.e-disclosure.ru/report.pdf")], tmp_path / "denied",
        session=FakeSession([FakeResponse(status=403)]), delay_seconds=0,
    )
    assert denied["failed"] == 1
    assert "HTTP 403" in denied["results"][0]["error"]

    oversized = fetch_manifest(
        con, [_item("https://www.e-disclosure.ru/report.pdf")], tmp_path / "large",
        max_bytes=1024, session=FakeSession([FakeResponse(PDF_FIXTURE.read_bytes())]),
        delay_seconds=0,
    )
    assert oversized["failed"] == 1
    assert "max-bytes" in oversized["results"][0]["error"]
    assert con.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 0
    con.close()


def test_registry_migrates_existing_rag_documents_columns():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE rag_documents (
        doc_sha256 TEXT PRIMARY KEY, isin TEXT, issuer_inn TEXT,
        doc_type TEXT NOT NULL, source_url TEXT, fetched_at TEXT,
        local_path TEXT NOT NULL, page_count INTEGER, parse_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE rag_financial_reports (
        doc_sha256 TEXT NOT NULL, report_id TEXT NOT NULL, period TEXT NOT NULL,
        actual_bfo_date TEXT, published_date TEXT, correction_number INTEGER,
        correction_date TEXT, form_code TEXT, source_url TEXT,
        PRIMARY KEY (doc_sha256, report_id)
    )""")
    init_registry(con)
    columns = {row[1] for row in con.execute("PRAGMA table_info(rag_documents)")}
    assert {"catalog_url", "source_title", "published_date", "identity_check",
            "source_kind", "authority_level"} <= columns
    report_columns = {row[1] for row in con.execute("PRAGMA table_info(rag_financial_reports)")}
    assert {"source_url_reported", "source_url_method"} <= report_columns
    con.close()
