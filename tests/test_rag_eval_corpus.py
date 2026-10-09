import json
from collections import defaultdict
from pathlib import Path

from hybond.rag.eval.runner import run
from hybond.rag.golden import load_golden
from hybond.rag.ingest import ingest_file
from hybond.rag.manifest import load_manifest, validate_local_files
from hybond.store import db

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval/corpus/manifest_v2.jsonl"
GOLDEN = ROOT / "eval/golden/ru_bonds_v2.jsonl"


def test_reviewed_fixture_corpus_integrity_and_split_isolation(tmp_path):
    documents = load_manifest(MANIFEST)
    validate_local_files(documents, ROOT)
    questions = load_golden(GOLDEN, documents)
    records = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]

    assert len(documents) == 30
    assert len({document.issuer_inn for document in documents}) == 10
    assert len(questions) == 72
    assert sum(question.answer_type == "none" for question in questions) / len(questions) >= 0.15
    assert all(question.split in {"dev", "test"} for question in questions)
    assert all(record.get("verified_by") and record.get("verified_at") for record in records)
    for split in ("dev", "test"):
        subset = [question for question in questions if question.split == split]
        assert sum(question.answer_type == "none" for question in subset) / len(subset) >= 0.15

    evidence_by_split = defaultdict(set)
    for question in questions:
        for evidence in question.evidence:
            evidence_by_split[question.split].add(evidence["doc_sha256"])
            assert isinstance(evidence.get("quote"), str) and evidence["quote"]
    assert not evidence_by_split["dev"] & evidence_by_split["test"]

    con = db.connect(tmp_path / "corpus.sqlite")
    db.init_db(con)
    try:
        for document in documents:
            ingest_file(
                con,
                ROOT / document.local_path,
                doc_type=document.doc_type,
                isin=None if document.isin == "unknown" else document.isin,
                issuer_inn=document.issuer_inn,
                source_url=document.source_url,
                source_kind="edisclosure",
                authority_level="issuer_disclosure",
            )

        tables = con.execute(
            "SELECT text,table_html FROM rag_elements WHERE kind='table'"
        ).fetchall()
        assert tables
        assert all(json.loads(row["text"]) and row["table_html"].startswith("<table>") for row in tables)

        for question in questions:
            for evidence in question.evidence:
                rows = con.execute(
                    "SELECT text FROM rag_chunks WHERE doc_sha256=? AND page_no=?",
                    (evidence["doc_sha256"], evidence["page"]),
                ).fetchall()
                page_text = " ".join(row[0] for row in rows).casefold()
                assert evidence["quote"].casefold() in page_text

        dev = run(con, GOLDEN, MANIFEST, k=5, split="dev")
        test = run(con, GOLDEN, MANIFEST, k=5, split="test")
        assert dev["question_count"] == sum(question.split == "dev" for question in questions)
        assert test["question_count"] == sum(question.split == "test" for question in questions)
        assert dev["split"] == "dev"
        assert test["split"] == "test"
        assert dev["metrics"]["answerable_evidence_match_rate"] is None
        assert dev["metrics"]["manual_review_answers"] == 40
        assert len(dev["dataset_sha256"]["golden"]) == 64
        assert dev["dataset_sha256"] == test["dataset_sha256"]
        assert all(isinstance(result["page_recall"], float) for result in dev["results"] + test["results"])
    finally:
        con.close()
