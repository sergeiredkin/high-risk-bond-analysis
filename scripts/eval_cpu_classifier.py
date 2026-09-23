#!/usr/bin/env python3
"""Decision-gate evaluation for a pure-CPU classifier (L35, 2026-09-23).

Question: can TF-IDF char-n-grams + LogisticRegression replace the local-GPU
Ollama classifier for the routine work, under the existing needs_review()
gate? Trains on the labeled corpus already in the DB (event JOIN filing on
source_url), time-split so the holdout is the most recent period, and reports
the metrics that matter for the cutover decision:

  - category accuracy + per-class precision/recall (10 classes, imbalanced)
  - severity accuracy
  - binary ALERT head recall/precision (covenant, reporting_delay, insolvency
    + any critical severity -- the misses that are expensive)
  - auto-accept share under the production gate (confidence >= 0.80 and
    severity not in high/critical) vs what the current LLM labels imply

Read-only on the DB. Trains in seconds on CPU. Not wired into production.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import classification_report, confusion_matrix  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402

DB = Path("data/db/hybond.sqlite")
SPLIT_DATE = "2026-09-08"          # train <= SPLIT, test > SPLIT
ALERT_CATEGORIES = {"covenant", "reporting_delay", "insolvency"}
AUTO_CONFIDENCE = 0.80             # == classify_ollama.REVIEW_CONFIDENCE_THRESHOLD


def load_corpus():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT e.occurred, e.category, e.severity, e.confidence AS llm_conf,
               e.classified_by, f.title, i.name AS issuer
        FROM event e
        JOIN filing f ON f.url = e.source_url
        LEFT JOIN issuer i ON i.issuer_id = e.issuer_id
        WHERE e.category IS NOT NULL AND e.severity IS NOT NULL
        """
    ).fetchall()
    con.close()
    corpus = []
    for r in rows:
        text = f"{r['title']} || {r['issuer'] or ''}"
        corpus.append({
            "text": text, "category": r["category"], "severity": r["severity"],
            "date": str(r["occurred"])[:10], "gold": r["classified_by"] in ("", None),
            "alert": 1 if (r["category"] in ALERT_CATEGORIES or r["severity"] == "critical") else 0,
        })
    return corpus


def make_model():
    return Pipeline([
        ("tfidf", TfidfVectorizer(analyzer="char", ngram_range=(3, 5),
                                  min_df=2, sublinear_tf=True, max_features=200_000)),
        ("lr", LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0)),
    ])


def evaluate(head_name, labels_test, proba, classes, alert_mode=False):
    import numpy as np
    pred_idx = proba.argmax(axis=1)
    pred = [classes[i] for i in pred_idx]
    conf = proba.max(axis=1)
    labels = sorted(set(labels_test) | set(classes))
    print(f"\n══ {head_name} ══")
    print(classification_report(labels_test, pred, labels=sorted(set(labels_test)),
                                zero_division=0, digits=2))
    if alert_mode:
        y = np.array([1 if l == "ALERT" else 0 for l in labels_test])
        alert_idx = list(classes).index("ALERT")
        p = proba[:, alert_idx]
        for thr in (0.3, 0.5, 0.7):
            hit = (p >= thr).astype(int)
            tp = int(((hit == 1) & (y == 1)).sum()); fp = int(((hit == 1) & (y == 0)).sum())
            fn = int(((hit == 0) & (y == 1)).sum())
            rec = tp / (tp + fn) if tp + fn else float("nan")
            prec = tp / (tp + fp) if tp + fp else float("nan")
            print(f"  alert@{thr:.1f}: recall={rec:.2f} precision={prec:.2f} (tp={tp} fp={fp} fn={fn})")
    auto = sum(1 for p_, c_, l_ in zip(pred, conf, labels_test)
               if c_ >= AUTO_CONFIDENCE and p_ not in ("high", "critical")
               and p_ == l_)
    print(f"  auto-accept under production gate (conf>={AUTO_CONFIDENCE}, "
          f"not high/critical, correct): {auto}/{len(labels_test)} = {auto/len(labels_test):.0%}")
    return pred


def run_head(name, key, train, test, alert_mode=False):
    y_tr = [("ALERT" if r["alert"] else "OK") if alert_mode else r[key] for r in train]
    y_te = [("ALERT" if r["alert"] else "OK") if alert_mode else r[key] for r in test]
    model = make_model()
    model.fit([r["text"] for r in train], y_tr)
    proba = model.predict_proba([r["text"] for r in test])
    evaluate(name, y_te, proba, model.classes_.tolist(), alert_mode=alert_mode)


def main():
    corpus = load_corpus()
    corpus.sort(key=lambda r: r["date"])
    train = [r for r in corpus if r["date"] <= SPLIT_DATE]
    test = [r for r in corpus if r["date"] > SPLIT_DATE]
    print(f"corpus: {len(corpus)} labeled items | train {len(train)} (<= {SPLIT_DATE}) | "
          f"test {len(test)} (> {SPLIT_DATE})")
    gold_test = [r for r in test if r["gold"]]
    print(f"holdout human-labeled (gold) subset: {len(gold_test)}")

    run_head("CATEGORY (all holdout)", "category", train, test)
    run_head("SEVERITY (all holdout)", "severity", train, test)
    run_head("ALERT binary (all holdout)", "alert", train, test, alert_mode=True)
    if len(gold_test) >= 30:
        g_tr = [r for r in train if r["gold"]]
        print(f"\n── gold-only variant (human-labeled train {len(g_tr)}, test {len(gold_test)}) ──")
        run_head("CATEGORY (gold holdout)", "category", g_tr, gold_test)
        run_head("ALERT binary (gold holdout)", "alert", g_tr, gold_test, alert_mode=True)
    else:
        print(f"\n(gold holdout too small for standalone eval: {len(gold_test)})")


if __name__ == "__main__":
    main()
