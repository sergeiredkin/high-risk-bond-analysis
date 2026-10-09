from hybond.rag.ingest import classify_source
from hybond.rag.registry import init_registry, upsert_document
import sqlite3


def test_secondary_commentary_is_not_called_issuer_disclosure():
    assert classify_source("Владислав Кофанов: сделки прошлой недели", "edisclosure", "issuer_disclosure") == (
        "market_commentary", "secondary_commentary"
    )
    assert classify_source("Сообщение эмитента о выплате купона", "edisclosure", "issuer_disclosure") == (
        "edisclosure", "issuer_disclosure"
    )
    # A factlist may quote secondary sources, but its corpus authority stays derived.
    assert classify_source("Smart-Lab mentioned in a bond factlist", "hybond_factlist", "derived_analysis") == (
        "hybond_factlist", "derived_analysis"
    )


def test_registry_repairs_authority_of_existing_factlists():
    con = sqlite3.connect(":memory:")
    init_registry(con)
    upsert_document(con, {
        "doc_sha256": "legacy-factlist", "doc_type": "hybond_factlist",
        "local_path": "factlist.json", "source_kind": "market_commentary",
        "authority_level": "secondary_commentary",
    })
    init_registry(con)
    assert tuple(con.execute(
        "SELECT source_kind, authority_level FROM rag_documents WHERE doc_sha256='legacy-factlist'"
    ).fetchone()) == ("hybond_factlist", "derived_analysis")
