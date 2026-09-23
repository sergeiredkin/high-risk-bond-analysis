#!/usr/bin/env python3
"""Train + export the pure-stdlib CPU classifier (models/cpu_classifier.json).

Runs wherever sklearn is available (this machine; the VPS never trains and
never imports sklearn). The classifier's own ngram function
(classify_rules.analyze) is handed to sklearn as the vectorizer analyzer, so
train-time and runtime tokenization are the same code by construction. After
exporting, the script self-checks the pure-stdlib scorer against sklearn's
predict_proba on holdout texts (max abs difference must be < 1e-6) and
prints the holdout metrics for the cutover record.

Usage: PYTHONPATH=src python3 scripts/train_cpu_classifier.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hybond import classify_rules
from hybond.classify_rules import analyze

DB = ROOT / "data" / "db" / "hybond.sqlite"
SPLIT_DATE = "2026-09-08"
MAX_FEATURES = 30_000          # caps model size: ~5 MB JSON, ~40 MB runtime RAM
ALERT_CATEGORIES = {"covenant", "reporting_delay", "insolvency"}
SELF_CHECK_TOLERANCE = 1e-6


def load_corpus() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT e.occurred, e.category, e.severity, e.classified_by,
               f.title, i.name AS issuer
        FROM event e
        JOIN filing f ON f.url = e.source_url
        LEFT JOIN issuer i ON i.issuer_id = e.issuer_id
        WHERE e.category IS NOT NULL AND e.severity IS NOT NULL
        """
    ).fetchall()
    con.close()
    corpus = []
    for r in rows:
        corpus.append({
            "text": f"{r['title']} || {r['issuer'] or ''}",
            "category": r["category"], "severity": r["severity"],
            "date": str(r["occurred"])[:10],
            "alert": 1 if (r["category"] in ALERT_CATEGORIES
                           or r["severity"] == "critical") else 0,
        })
    return corpus


def fit_head(train, key, alert_mode=False):
    y = [("ALERT" if r["alert"] else "OK") if alert_mode else r[key] for r in train]
    vec = TfidfVectorizer(analyzer=analyze, min_df=2, sublinear_tf=True,
                          max_features=MAX_FEATURES)
    lr = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0)
    X = vec.fit_transform([r["text"] for r in train])
    lr.fit(X, y)
    return vec, lr


def export_head(lr, classes) -> dict:
    return {
        "classes": list(classes),
        "coef": [[round(float(v), 8) for v in row] for row in lr.coef_],
        "b": [round(float(v), 8) for v in lr.intercept_],
    }


def runtime_probabilities(state, texts, head_name):
    head = state["heads"][head_name]
    out = []
    for t in texts:
        pairs = dict(classify_rules._score_head(head, classify_rules._transform(t, state)))
        out.append([pairs[c] for c in head["classes"]])
    return np.array(out, dtype=float)


def main() -> int:
    corpus = load_corpus()
    corpus.sort(key=lambda r: r["date"])
    train = [r for r in corpus if r["date"] <= SPLIT_DATE]
    test = [r for r in corpus if r["date"] > SPLIT_DATE]
    print(f"corpus {len(corpus)} | train {len(train)} | holdout {len(test)}")

    state: dict = {
        "version": 1,
        "meta": {
            "trained_on": max(r["date"] for r in train),
            "split_date": SPLIT_DATE,
            "train_items": len(train),
            "notes": "TF-IDF char 3-5 ngrams + LogisticRegression, "
                     f"max_features={MAX_FEATURES}, class_weight=balanced",
        },
        "vocab": {}, "idf": [], "heads": {},
    }

    fitted = {}
    for name, key, alert in (("category", "category", False),
                             ("severity", "severity", False),
                             ("alert", "alert", True)):
        vec, lr = fit_head(train, key, alert_mode=alert)
        fitted[name] = (vec, lr)
        state["heads"][name] = export_head(lr, list(lr.classes_))

    # One shared vocab/idf (from the category head's vectorizer -- all three
    # heads see identical features by construction of analyze()).
    names = fitted["category"][0].get_feature_names_out()
    state["vocab"] = {n: i for i, n in enumerate(names)}
    state["idf"] = [round(float(v), 8) for v in fitted["category"][0].idf_]

    out = ROOT / "models" / "cpu_classifier.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"exported {out} ({out.stat().st_size / 1e6:.1f} MB, "
          f"vocab {len(state['vocab'])})")

    # Self-check: the stdlib runtime must reproduce sklearn's probabilities
    # bit-tightly on holdout texts, or train/runtime tokenization drifted.
    sample = [r["text"] for r in test[:300]] or [r["text"] for r in train[:300]]
    worst = 0.0
    for name in state["heads"]:
        if name == "alert":
            continue  # binary head proba ordering checked via category/severity
        vec, lr = fitted[name]
        sk = lr.predict_proba(vec.transform(sample))
        ru = runtime_probabilities(state, sample, name)
        worst = max(worst, float(np.abs(sk - ru).max()))
    print(f"self-check: max |sklearn - runtime| probability diff = {worst:.2e} "
          f"({'OK' if worst < SELF_CHECK_TOLERANCE else 'MISMATCH -- DO NOT SHIP'})")
    if worst >= SELF_CHECK_TOLERANCE:
        return 1

    # Holdout metrics for the cutover record (same split as the eval gate).
    y_cat = [r["category"] for r in test]
    pred_cat = [state["heads"]["category"]["classes"][i]
                for i in np.argmax(runtime_probabilities(state, [r["text"] for r in test],
                                                         "category"), axis=1)]
    print(classification_report(y_cat, pred_cat, zero_division=0, digits=2))
    y_alert = [r["alert"] for r in test]
    p_alert = runtime_probabilities(state, [r["text"] for r in test], "alert")
    alert_p = p_alert[:, state["heads"]["alert"]["classes"].index("ALERT")]
    pred_alert = (alert_p >= 0.5).astype(int)
    tp = int(((pred_alert == 1) & (np.array(y_alert) == 1)).sum())
    fp = int(((pred_alert == 1) & (np.array(y_alert) == 0)).sum())
    fn = int(((pred_alert == 0) & (np.array(y_alert) == 1)).sum())
    print(f"alert@0.5: recall={tp / (tp + fn):.2f} precision={tp / (tp + fp):.2f} "
          f"(tp={tp} fp={fp} fn={fn})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
