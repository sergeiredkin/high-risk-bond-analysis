"""Tests for classify_rules.py -- the pure-stdlib CPU classifier (no-Ollama
engine for the 1 GB VPS). Contract parity with classify_ollama.classify_filing()
is the point: same taxonomy, same dict shape, same needs_review() gating.
Content QUALITY over the full corpus is measured by
scripts/eval_cpu_classifier.py + train_cpu_classifier.py's holdout report --
these tests pin the contract and the guard/escalation mechanics on a
hermetic synthetic model (no network, no trained-artifact dependency)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import classify_rules
from hybond.classify_ollama import (
    VALID_CATEGORIES,
    VALID_SEVERITIES,
    needs_review,
)
from hybond.classify_rules import MODEL_PATH, classify_filing, load_state


def test_model_file_exists_and_has_heads():
    assert MODEL_PATH.exists(), "run scripts/train_cpu_classifier.py to export the model"
    state = load_state()
    assert set(state["heads"]) == {"category", "severity", "alert"}
    assert len(state["vocab"]) > 1000
    assert len(state["idf"]) == len(state["vocab"])
    for head in state["heads"].values():
        # binary heads export ONE margin row for two classes (sklearn
        # LogisticRegression convention); multiclass heads one row per class
        assert len(head["coef"]) in (1, len(head["classes"]))
        assert len(head["b"]) in (1, len(head["classes"]))


def test_classify_filing_contract_parity():
    result = classify_filing("Эмитент пропустил выплату купона по серии 001",
                             "http://x", "Тест ОАО")
    assert result is not None
    assert set(result) == {"category", "severity", "confidence", "summary"}
    assert result["category"] in VALID_CATEGORIES
    assert result["severity"] in VALID_SEVERITIES
    assert 0.0 <= result["confidence"] <= 1.0
    assert len(result["summary"]) > 5


def test_classify_filing_deterministic():
    a = classify_filing("Совет директоров утвердил дивиденды", "http://x", "Тест ОАО")
    b = classify_filing("Совет директоров утвердил дивиденды", "http://x", "Тест ОАО")
    assert a == b


def test_max_picks_highest_probability_not_alphabetical_class():
    """Regression: max() over (class, proba) tuples compares the CLASS STRINGS
    first -- 'high' > 'critical' alphabetically, so an almost-uniform severity
    distribution returned 'high' at p=0.08 over 'critical' at p=0.30."""
    head = {"classes": ["critical", "high"],
            "coef": [[0.0], [0.0]], "b": [0.30, 0.08]}
    winner = max(classify_rules._score_head(head, {0: 1.0}), key=lambda cp: cp[1])
    assert winner[0] == "critical"  # class must win on probability, not name


def _synthetic_state():
    """Hand-built model: ngram 'qqq' in the text drives insolvency + ALERT;
    everything else resolves to routine + noise. Exercises the coherence
    guards and the alert escalation deterministically."""
    return {
        "vocab": {"qqq": 0}, "idf": [1.0],
        "heads": {
            "category": {"classes": ["routine", "insolvency"],
                          "coef": [[0.0], [1.0]], "b": [0.2, -0.2]},
            "severity": {"classes": ["noise", "high"],
                          "coef": [[0.0], [1.0]], "b": [1.0, -0.2]},
            "alert": {"classes": ["OK", "ALERT"],
                       "coef": [[0.0]], "b": [0.8]},
        },
    }


def test_coherent_baseline_without_alert(monkeypatch):
    monkeypatch.setattr(classify_rules, "load_state", lambda path=None: _synthetic_state())
    r = classify_filing("Обычный заголовок", "http://x", "Тест ОАО")
    assert r["category"] == "routine"
    assert r["severity"] == "noise"          # coherent: routine+noise stays


def test_alert_fires_escalation_and_coherence_guards(monkeypatch):
    monkeypatch.setattr(classify_rules, "load_state", lambda path=None: _synthetic_state())
    r = classify_filing("Заголовок с qqq триггером", "http://x", "Тест ОАО")
    # alert head fired (ALERT) -> severity escalates up to high -> review;
    # 'noise' never rides on a non-routine category (guard order matters)
    assert r["category"] == "insolvency"
    assert r["severity"] == "high"
    assert needs_review(r["severity"], r["confidence"]) is True


def test_auto_classify_pass_wires_rules_engine(tmp_path):
    """End-to-end wiring: the real queue -> classify_filing -> event written
    with a rules:* label and the filing marked classified (no GPU involved)."""
    import argparse

    from hybond.__main__ import _auto_classify_pass
    from hybond.store import db

    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_filing(con)
    db.init_market_note(con)
    db.upsert_issuer(con, "1", "Rules Test Issuer")
    con.execute("UPDATE issuer SET watchlist_tier=1 WHERE issuer_id='1'")
    con.execute(
        "INSERT INTO filing(body_hash, issuer_id, edisclosure_id, published, "
        "title, url, fetched_on, classified) VALUES("
        "'hash-rules-1','1',NULL,'2026-09-23',"
        "'Эмитент пропустил выплату купона по серии 003',"
        "'http://src/rules-test','2026-09-23',0)")
    con.commit()

    args = argparse.Namespace(source="filing", engine="rules", limit=None,
                              dry_run=False, priority_only=False)
    stats = _auto_classify_pass(con, args)

    assert stats["processed"] == 1 and stats["failed"] == 0
    event = con.execute("SELECT classified_by, category FROM event "
                        "WHERE source_url='http://src/rules-test'").fetchone()
    assert event is not None and event["classified_by"].startswith("rules:")
    assert event["category"] in VALID_CATEGORIES
    assert con.execute("SELECT classified FROM filing "
                       "WHERE body_hash='hash-rules-1'").fetchone()["classified"] == 1
    con.close()
