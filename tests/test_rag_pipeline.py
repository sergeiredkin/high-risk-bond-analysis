import json
from pathlib import Path
from types import SimpleNamespace

from hybond import pipeline_lock
from hybond.__main__ import cmd_docs
from hybond.rag.ingest import ingest_file
from hybond.rag.search import hybrid_search, reciprocal_rank_fusion, search, snippet
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
