from pathlib import Path

from hybond.rag.ingest import ingest_file
from hybond.rag.search import hybrid_search, reciprocal_rank_fusion, search, snippet
from hybond.store import db


def test_html_table_rows_and_cells_remain_distinguishable(tmp_path):
    source = tmp_path / "table.html"
    source.write_text(
        "<html><style>hide me</style><body><table>"
        "<tr><th>Показатель</th><th>Значение</th></tr>"
        "<tr><td>Купон</td><td>15,5%</td></tr>"
        "</table><script>ignore me</script></body></html>",
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    indexed = ingest_file(con, source, doc_type="table_fixture")
    page = " ".join(row[0] for row in con.execute(
        "SELECT text FROM rag_chunks WHERE doc_sha256=? ORDER BY chunk_id",
        (indexed["doc_sha256"],),
    ))
    assert "Показатель\tЗначение" in page
    assert "Купон\t15,5%" in page
    assert "hide me" not in page
    assert "ignore me" not in page
    con.close()


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
