from pathlib import Path

import pytest

from hybond.rag.dense_index import DenseIndex, build_index
from hybond.rag.ingest import ingest_file
from hybond.rag.retrieval import retrieve
from hybond.rag.search import hybrid_search
from hybond.store import db


class FakeEncoder:
    def encode(self, texts):
        return [[1.0, 0.0] if "ликвид" in text else [0.0, 1.0] for text in texts]


def test_dense_index_build_and_search(tmp_path: Path):
    pytest.importorskip("numpy")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    source = tmp_path / "report.md"
    source.write_text("# Ликвидность\n\nЗапас ликвидности достаточный.\f"
                      "# Рейтинг\n\nРейтинг подтверждён.", encoding="utf-8")
    ingest_file(con, source, doc_type="issuer_report", source_kind="issuer_report",
                authority_level="issuer_disclosure")
    out = tmp_path / "dense-v1"
    assert build_index(con, out, FakeEncoder()) == 2
    index = DenseIndex.load(out, FakeEncoder())
    assert index.search("ликвидность", limit=1)[0].page_no == 1
    assert index.search("ликвидность", issuer_inn="wrong") == []
    assert index.search("ликвидность", source_scope="official")
    assert index.search("ликвидность", doc_type="cbr_document") == []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("HYBOND_RAG_DENSE_INDEX", str(out))
    monkeypatch.setenv("HYBOND_RAG_MODEL_PATH", str(tmp_path / "fake-model"))
    monkeypatch.setattr("hybond.rag.retrieval._dense_index", lambda *_: index)
    hybrid = retrieve(con, "вопрос без лексического совпадения", limit=2)
    assert any(hit.chunk_id for hit in hybrid)
    assert retrieve(con, "вопрос", limit=2, issuer_inn="wrong") == []
    monkeypatch.undo()
    hits = index.search("ликвидность", limit=2)
    con.execute("UPDATE rag_documents SET issuer_inn='A'")
    assert all(hit.chunk_id in {item.chunk_id for item in hybrid_search(
        con, "ликвидность", dense_hits=hits, issuer_inn="A"
    )} for hit in hits)
    con.execute("UPDATE rag_documents SET issuer_inn='B'")
    assert hybrid_search(con, "ликвидность", dense_hits=hits, issuer_inn="A") == []
    con.close()


def test_empty_dense_index_is_searchable(tmp_path: Path):
    pytest.importorskip("numpy")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    out = tmp_path / "empty-index"
    assert build_index(con, out, FakeEncoder()) == 0
    assert DenseIndex.load(out, FakeEncoder()).search("query") == []
    con.close()
