from pathlib import Path

import pytest

from hybond.rag.dense_index import DenseIndex, build_index
from hybond.rag.ingest import ingest_file
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
    con.close()
