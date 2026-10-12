from pathlib import Path

from hybond.rag.ingest import ingest_file
from hybond.rag.verify import verify_citation
from hybond.store import db


def test_page_quote_may_span_adjacent_chunks_without_failing_verification(tmp_path):
    source = tmp_path / "two-chunks.md"
    source.write_text(
        "Левая часть цитаты. " + "x" * 1900 + "\n\nПравая часть цитаты.",
        encoding="utf-8",
    )
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    document = ingest_file(con, source, doc_type="issuer_notice")

    rows = con.execute(
        "SELECT text FROM rag_chunks WHERE doc_sha256=? ORDER BY rowid",
        (document["doc_sha256"],),
    ).fetchall()
    assert len(rows) >= 2
    quote = "Левая часть цитаты. " + "x" * 1900 + "\n\nПравая часть цитаты."
    result = verify_citation(con, {
        "doc_sha256": document["doc_sha256"], "page": 1, "quote": quote,
    })

    assert result == {"valid": True, "reason": "verified"}
    con.close()
