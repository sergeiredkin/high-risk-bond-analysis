import json
from pathlib import Path
from types import SimpleNamespace

from hybond import pipeline_lock
from hybond.__main__ import cmd_docs
from hybond.rag.ingest import ingest_file
from hybond.rag.search import (
    hybrid_search,
    reciprocal_rank_fusion,
    search,
    search_scope_summary,
    snippet,
)
from hybond.store import db


def test_html_table_rows_and_cells_remain_distinguishable(tmp_path):
    source = tmp_path / "table.html"
    source.write_text(
        "<html><style>hide me</style><body><p>Описание до таблицы</p><table>"
        "<tr><th>Показатель</th><th>Значение</th></tr>"
        "<tr><td>Купон</td><td>15,5%</td></tr>"
        "<tr><td colspan='2'>Условия выпуска</td></tr>"
        "<tr><td rowspan='invalid'>Начало<td>Окончание</tr>"
        "</table><p>Описание после таблицы</p>"
        "<script>ignore me</script></body></html>",
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    indexed = ingest_file(con, source, doc_type="table_fixture")
    ingest_file(con, source, doc_type="table_fixture")
    page = " ".join(row[0] for row in con.execute(
        "SELECT text FROM rag_chunks WHERE doc_sha256=? ORDER BY chunk_id",
        (indexed["doc_sha256"],),
    ))
    assert "Показатель\tЗначение" in page
    assert "Купон\t15,5%" in page
    assert "hide me" not in page
    assert "ignore me" not in page
    table_chunks = con.execute(
        "SELECT section,text FROM rag_chunks WHERE doc_sha256=? AND section LIKE 'table_%'",
        (indexed["doc_sha256"],),
    ).fetchall()
    prose_chunks = con.execute(
        "SELECT text FROM rag_chunks WHERE doc_sha256=? AND section NOT LIKE 'table_%'",
        (indexed["doc_sha256"],),
    ).fetchall()
    assert len(table_chunks) == 1
    assert table_chunks[0]["section"] == "table_1"
    assert "Купон\t15,5%" in table_chunks[0]["text"]
    assert "Описание до таблицы" in " ".join(row["text"] for row in prose_chunks)
    assert "Купон" not in " ".join(row["text"] for row in prose_chunks)
    elements = con.execute(
        "SELECT kind,text,table_html FROM rag_elements WHERE doc_sha256=?",
        (indexed["doc_sha256"],),
    ).fetchall()
    assert len(elements) == 1
    assert elements[0]["kind"] == "table"
    rows = json.loads(elements[0]["text"])
    assert [cell["text"] for cell in rows[1]] == ["Купон", "15,5%"]
    assert rows[2][0]["text"] == "Условия выпуска"
    assert rows[2][0]["colspan"] == 2
    assert [cell["text"] for cell in rows[3]] == ["Начало", "Окончание"]
    assert rows[3][0]["rowspan"] == 1
    assert "<td colspan=\"2\">Условия выпуска</td>" in elements[0]["table_html"]
    con.close()


def test_ingest_dir_reprocess_is_bounded_and_refreshes_table_elements(tmp_path, monkeypatch, capsys):
    root = tmp_path / "raw"
    root.mkdir()
    for name, value in (("one.html", "Первое"), ("two.html", "Второе")):
        (root / name).write_text(
            f"<table><tr><th>Показатель</th></tr><tr><td>{value}</td></tr></table>",
            encoding="utf-8",
        )
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rag.sqlite")
    monkeypatch.setattr(pipeline_lock, "PIPELINE_LOCK_PATH", tmp_path / "pipeline.lock")
    con = db.connect()
    db.init_db(con)
    for source in root.glob("*.html"):
        ingest_file(con, source, doc_type="table_fixture")
    con.execute("DELETE FROM rag_elements")
    con.commit()
    con.close()

    args = SimpleNamespace(
        docs_cmd="ingest-dir", root=str(root), pattern="*.html", since_date=None,
        until_date=None, since_days=None, limit=1, reprocess=False,
        doc_type="table_fixture", source_kind="unknown", authority_level="unknown",
    )
    assert cmd_docs(args) == 0
    assert json.loads(capsys.readouterr().out)["ingested"] == []

    args.reprocess = True
    assert cmd_docs(args) == 0
    first_batch = json.loads(capsys.readouterr().out)["ingested"]
    assert len(first_batch) == 1
    con = db.connect()
    assert con.execute("SELECT COUNT(*) FROM rag_elements WHERE kind='table'").fetchone()[0] == 1
    con.close()

    assert not (tmp_path / "pipeline.lock").exists()


def test_ingest_is_idempotent_and_search_returns_page_citation(tmp_path):
    source = tmp_path / "report.md"
    source.write_text("# Ликвидность\n\nЭмитент имеет достаточный запас ликвидности.\f"
                      "# Оферта\n\nДата оферты 15 марта 2027 года.", encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    first = ingest_file(con, source, doc_type="issuer_report", isin="RU000TEST",
                         source_kind="issuer_report", authority_level="issuer_disclosure")
    second = ingest_file(con, source, doc_type="issuer_report", isin="RU000TEST")

    assert first == second
    assert con.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0] == 2
    assert tuple(con.execute("SELECT source_kind, authority_level FROM rag_documents").fetchone()) == ("issuer_report", "issuer_disclosure")
    hits = hybrid_search(con, "даты оферты", limit=3)
    assert hits and hits[0].page_no == 2
    assert search(con, "дата оферты", issuer_inn="does-not-exist") == []
    assert search(con, "дата оферты", doc_type="issuer_report")
    assert search(con, "дата оферты", doc_type="cbr_document") == []
    assert len(snippet("x" * 2000, "дата", max_chars=100)) <= 100
    con.close()


def test_search_scope_summary_reports_exact_index_filter_coverage(tmp_path):
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    source = tmp_path / "official.md"
    source.write_text("Отчёт финансового периода за 2025 год.", encoding="utf-8")
    ingest_file(con, source, doc_type="nalog_financials", issuer_inn="1234567890",
                isin="RU000A123456", source_kind="nalog", authority_level="financial_source")

    summary = search_scope_summary(
        con, issuer_inn="1234567890", isin="RU000A123456",
        source_scope="official", doc_type="nalog_financials",
    )
    assert summary["indexed_documents_in_scope"] == 1
    assert summary["filters"]["issuer_name"] == "Example Co"
    assert summary["by_source"] == [{
        "doc_type": "nalog_financials", "source_kind": "nalog",
        "authority_level": "financial_source", "count": 1,
    }]
    assert search_scope_summary(con, isin="RU000A000000")["indexed_documents_in_scope"] == 0
    con.close()


def test_search_bundles_bounded_page_context_for_page_citations(tmp_path):
    source = tmp_path / "page.md"
    source.write_text(
        "# Уведомление\n\nРазмер выплаты по купону составляет 12 400 рублей.\n\n"
        + "Справочная часть " + "x" * 1900 + " конец второго блока.\n\n"
        + "Дополнительное условие: дата окончания 2031 года.",
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    ingest_file(con, source, doc_type="issuer_notice")

    hits = search(con, "размер выплаты купону", limit=3)
    assert len(hits) == 1
    assert "12 400 рублей" in hits[0].text
    assert "дата окончания 2031 года" in hits[0].text
    assert len(hits[0].text) <= 12_000

    from hybond.rag.search import SearchHit
    rows = con.execute(
        "SELECT chunk_id,doc_sha256,page_no,section,text FROM rag_chunks"
    ).fetchall()
    dense_hits = [SearchHit(row[0], row[1], row[2], row[3] or "", row[4], 1.0) for row in rows]
    hybrid_hits = hybrid_search(con, "размер выплаты купону", limit=3, dense_hits=dense_hits)
    assert len(hybrid_hits) == 1
    assert "дата окончания 2031 года" in hybrid_hits[0].text
    con.close()


def test_long_single_paragraph_is_chunked_and_later_financial_fact_is_retrievable(tmp_path):
    source = tmp_path / "financials.json"
    source.write_text(
        '{"organization":{"name":"Example Co","notes":"' + "x" * 24_000
        + '"},"financial_data":{"period":"2025 год","выручка":42000000}}',
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    document = ingest_file(con, source, doc_type="nalog_financials")

    chunk_lengths = [row[0] for row in con.execute(
        "SELECT length(text) FROM rag_chunks WHERE doc_sha256=?",
        (document["doc_sha256"],),
    )]
    assert len(chunk_lengths) > 10
    assert max(chunk_lengths) <= 1800
    hits = search(con, "Найди выручку за 2025 год", limit=3, doc_type="nalog_financials")
    assert hits
    excerpt = snippet(hits[0].text, "выручка 2025 год")
    assert "42000000" in excerpt
    from hybond.rag.verify import verify_citation
    assert verify_citation(con, {
        "doc_sha256": hits[0].doc_sha256, "page": hits[0].page_no, "quote": excerpt,
    })["valid"] is True
    con.close()


def test_company_scope_words_do_not_count_as_document_evidence(tmp_path):
    unrelated = tmp_path / "board.md"
    unrelated.write_text(
        "Сегежа Групп: проведение заседания совета директоров и повестка дня.",
        encoding="utf-8",
    )
    relevant = tmp_path / "coupon.md"
    relevant.write_text(
        "Сегежа Групп. Выплата купона по облигациям состоится 19 октября; ставка купона 21 процентов.",
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("9703024202", "Сегежа Групп"))
    con.commit()
    ingest_file(con, unrelated, doc_type="edisclosure_html", issuer_inn="9703024202")
    relevant_doc = ingest_file(con, relevant, doc_type="edisclosure_html", issuer_inn="9703024202")

    hits = search(
        con, "Найди купонную выплату по облигациям Сегежа Групп",
        issuer_inn="9703024202", doc_type="edisclosure_html",
    )
    assert hits
    assert {hit.doc_sha256 for hit in hits} == {relevant_doc["doc_sha256"]}
    con.close()


def test_english_central_bank_alias_retrieves_russian_cbr_document(tmp_path):
    source = tmp_path / "cbr.md"
    source.write_text(
        "Банк России: прогноз ключевой ставки в среднем за год на 2026 год — 14 процентов.",
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    ingest_file(con, source, doc_type="cbr_document", source_kind="cbr",
                authority_level="official")

    hits = search(con, "CB key rate forecast", limit=3,
                  source_scope="official", doc_type="cbr_document")
    assert hits
    assert "14 процентов" in hits[0].text
    con.close()


def test_cbr_forecast_query_prefers_latest_forecast_source_over_materials_index(tmp_path):
    older_path = tmp_path / "2026-02-13" / "forecast.md"
    latest_path = tmp_path / "2026-07-24" / "forecast.md"
    older_path.parent.mkdir()
    latest_path.parent.mkdir()
    older_path.write_text("Ключевая ставка 2026: прогноз 13 процентов.", encoding="utf-8")
    latest_path.write_text(
        "Ключевая ставка по годам: 2026 год 14 процентов, 2027 год 9 процентов.",
        encoding="utf-8",
    )
    index = tmp_path / "materials_index.md"
    index.write_text("Банк России прогноз ключевой ставки. " * 30, encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    ingest_file(con, older_path, doc_type="cbr_document", source_kind="cbr",
                authority_level="official")
    latest = ingest_file(con, latest_path, doc_type="cbr_document", source_kind="cbr",
                         authority_level="official")
    ingest_file(con, index, doc_type="cbr_document", source_kind="cbr",
                authority_level="official")

    hits = search(con, "CB key rate forecast", limit=3, doc_type="cbr_document")
    assert hits
    assert hits[0].doc_sha256 == latest["doc_sha256"]
    assert "14 процентов" in hits[0].text
    con.close()


def test_snippet_chooses_window_with_most_query_evidence():
    text = (
        "Сегежа регистрационные сведения. " * 20
        + "Финансовая отчетность: выручка 12 млрд рублей за 2025 год. Сегежа."
    )
    result = snippet(
        text,
        "Сегежа финансовая отчетность выручка 2025 год",
        max_chars=120,
    )
    assert "12 млрд рублей" in result
    assert "2025 год" in result


def test_fts_empty_query_and_rrf_deduplicate(tmp_path):
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    assert hybrid_search(con, "", limit=5) == []
    from hybond.rag.search import SearchHit
    one = SearchHit("a", "doc", 1, "", "a", 1)
    two = SearchHit("b", "doc", 2, "", "b", 1)
    fused = reciprocal_rank_fusion([one, two], [two, one])
    assert [hit.chunk_id for hit in fused] == ["a", "b"]
    con.close()
