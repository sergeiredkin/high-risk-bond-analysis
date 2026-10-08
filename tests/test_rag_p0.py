import hashlib
import json
from pathlib import Path

import pytest

from hybond.rag.golden import load_golden
from hybond.rag.manifest import load_manifest, validate_local_files


def _manifest_line(path: Path) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "doc_sha256": digest,
        "isin": "RU000A10TEST",
        "issuer_inn": "7700000000",
        "doc_type": "prospectus",
        "source_url": "https://example.test/doc.pdf",
        "fetched_at": "2026-10-07T00:00:00Z",
        "local_path": str(path),
    }


def test_manifest_validates_hash_and_rejects_duplicate(tmp_path):
    document = tmp_path / "doc.pdf"
    document.write_bytes(b"fixture")
    record = _manifest_line(document)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")

    loaded = load_manifest(manifest)
    validate_local_files(loaded)
    manifest.write_text("\n".join(json.dumps(record) for _ in range(2)), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(manifest)


def test_manifest_detects_changed_file(tmp_path):
    document = tmp_path / "doc.pdf"
    document.write_bytes(b"fixture")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(_manifest_line(document)), encoding="utf-8")
    document.write_bytes(b"changed")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        validate_local_files(load_manifest(manifest))


def test_golden_requires_manifest_evidence_reference(tmp_path):
    document = tmp_path / "doc.pdf"
    document.write_bytes(b"fixture")
    manifest = load_manifest_path = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(_manifest_line(document)), encoding="utf-8")
    documents = load_manifest(load_manifest_path)
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({
        "qid": "GS-0001", "split": "dev", "question": "Есть ли оферта?",
        "category": "put_offer", "answer_type": "date", "expected": {"value": "2027-01-01"},
        "evidence": [{"doc_sha256": documents[0].doc_sha256, "page": 2}],
        "requires": ["table"],
    }, ensure_ascii=False), encoding="utf-8")

    assert load_golden(golden, documents)[0].qid == "GS-0001"
    golden.write_text(golden.read_text(encoding="utf-8").replace(documents[0].doc_sha256, "0" * 64), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown document"):
        load_golden(golden, documents)
