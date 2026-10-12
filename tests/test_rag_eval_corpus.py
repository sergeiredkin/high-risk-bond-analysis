import html
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from hybond.rag.eval.metrics import expected_in_hits
from hybond.rag.eval.runner import run
from hybond.rag.golden import load_golden
from hybond.rag.ingest import ingest_file
from hybond.rag.manifest import load_manifest, validate_local_files
from hybond.rag.search import SearchHit
from hybond.store import db

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "eval/corpus/manifest_v2.jsonl"
GOLDEN = ROOT / "eval/golden/ru_bonds_v2.jsonl"
GOLDEN_V3 = ROOT / "eval/golden/ru_bonds_v3_candidate.jsonl"


def test_manifest_has_message_level_provenance_matching_archived_html():
    records = [json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines()]
    documents = {doc.doc_sha256: doc for doc in load_manifest(MANIFEST)}
    disclosure_ids = set()
    for record in records:
        source = (ROOT / record["local_path"]).read_text(encoding="utf-8", errors="replace")
        backurls = set(re.findall(
            r'value="/messages/(\d+)/\?MESS_ID=(\d+)"', source, flags=re.IGNORECASE,
        ))
        assert len(backurls) == 1
        message_id, query_id = next(iter(backurls))
        assert message_id == query_id == record["disclosure_id"]
        document = documents[record["doc_sha256"]]
        assert document.disclosure_id == message_id
        assert document.published_at == record["published_at"]
        assert document.retrieved_at == record["retrieved_at"]
        assert document.source_url_basis == record["source_url_basis"]
        assert message_id not in disclosure_ids
        disclosure_ids.add(message_id)

        parsed_url = urlparse(record["source_url"])
        assert parsed_url.netloc == "e-disclosure.azipi.ru"
        assert parsed_url.path == f"/messages/{message_id}/"
        assert record["source_url_basis"] == "reconstructed_from_archived_backurl"

        visible_text = re.sub(r"<[^>]+>", " ", html.unescape(source))
        published = re.search(r"Дата\s+раскрытия\s*:\s*(\d{2}\.\d{2}\.\d{4})", visible_text)
        assert published
        day, month, year = map(int, published.group(1).split("."))
        assert record["published_at"] == date(year, month, day).isoformat()
        date.fromisoformat(record["retrieved_at"])
        assert record["retrieved_at"] == record["fetched_at"]

    assert len(records) == 30
    assert len(disclosure_ids) == 30


def test_v3_candidate_uses_substantive_questions_and_issuer_group_holdout(tmp_path):
    documents = load_manifest(MANIFEST)
    questions = load_golden(GOLDEN_V3, documents)
    raw_questions = [
        json.loads(line) for line in GOLDEN_V3.read_text(encoding="utf-8").splitlines()
    ]
    assert len(questions) == 34
    assert all(question.answer_type in {"numeric", "date"} for question in questions)
    assert all(question.evidence for question in questions)
    assert all(record["verified_by"] == "agent-source-check" for record in raw_questions)
    assert len({question.question for question in questions}) == len(questions)
    assert len({question.category for question in questions}) == 6

    issuers_by_split = {
        split: {question.issuer_inn for question in questions if question.split == split}
        for split in ("dev", "test")
    }
    assert len(issuers_by_split["dev"]) == 6
    assert len(issuers_by_split["test"]) == 4
    assert not issuers_by_split["dev"] & issuers_by_split["test"]
    assert not {
        (question.question, json.dumps(question.expected, ensure_ascii=False))
        for question in questions if question.split == "dev"
    } & {
        (question.question, json.dumps(question.expected, ensure_ascii=False))
        for question in questions if question.split == "test"
    }

    con = db.connect(tmp_path / "v3-candidate.sqlite")
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
        for question in questions:
            evidence = question.evidence[0]
            chunks = con.execute(
                "SELECT text FROM rag_chunks WHERE doc_sha256=? AND page_no=?",
                (evidence["doc_sha256"], evidence["page"]),
            ).fetchall()
            assert any(evidence["quote"] in row[0] for row in chunks), question.qid
            quote_hit = SearchHit(
                question.qid, evidence["doc_sha256"], evidence["page"], "",
                evidence["quote"], 1.0,
            )
            assert expected_in_hits(question.expected, question.answer_type, [quote_hit]) is True, question.qid

        reports = {
            split: run(con, GOLDEN_V3, MANIFEST, k=5, split=split)
            for split in ("dev", "test")
        }
        assert reports["dev"]["question_count"] == 20
        assert reports["test"]["question_count"] == 14
        assert reports["dev"]["dataset_sha256"] == reports["test"]["dataset_sha256"]
        assert all(report["metrics"]["answer_abstention_accuracy"] is None for report in reports.values())
    finally:
        con.close()


def test_candidate_fixture_corpus_integrity_and_split_isolation(tmp_path):
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
        assert dev["metrics"]["answer_abstention_accuracy"] is None
        assert dev["metrics"]["unanswerable_question_count"] == 8
        assert dev["metrics"]["manual_review_answers"] == 40
        assert len(dev["dataset_sha256"]["golden"]) == 64
        assert dev["dataset_sha256"] == test["dataset_sha256"]
        assert all(isinstance(result["page_recall"], float) for result in dev["results"] + test["results"])
    finally:
        con.close()
