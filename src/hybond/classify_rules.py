"""Pure-stdlib CPU classifier — the no-Ollama engine for a 1 GB VPS.

Contract is IDENTICAL to classify_ollama.classify_filing(): returns
{category, severity, confidence, summary} using the same taxonomy, and
confidence feeds needs_review() the same way. The model itself is exported
by scripts/train_cpu_classifier.py to models/cpu_classifier.json (trained
on the labeled event corpus with sklearn); THIS module is the VPS runtime
and deliberately imports json/re/math only — no numpy, no sklearn, no
Ollama, no network.

Decision audit (scripts/eval_cpu_classifier.py, 2026-09-23, 399-item
time-split holdout): the binary alert head carries the expensive-miss
protection (recall 0.88 / precision 0.94); the category head is strong
triage (routine precision 1.00, rating_action 1.00) but not LLM-grade on
every class — so a rules-engine result below the 0.65 confidence gate is
written with needs_review=1 instead of being silently skipped: on a VPS
with no GPU there is no second engine to retry with, and the queue must
keep draining (stuck-forever items were the 2026-09-22 failure mode).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "cpu_classifier.json"
NGRAM_RANGE = (3, 5)

# Short per-category credit takeaway appended to the templated summary --
# drier than the LLM's prose but factual, and the event feed tolerates it.
_CATEGORY_TAKEAWAY = {
    "routine": "плановое раскрытие, прямого кредитного сигнала нет",
    "coupon_event": "событие по купону — сверить график выплат и риск пропуска",
    "offer_put_call": "оферта/амортизация — сверить график с позицией",
    "covenant": "кредитный сигнал по ковенантам — проверить условия выпуска",
    "rating_action": "рейтинговое действие — пересмотреть оценку вероятности дефолта",
    "governance": "корпоративное событие — оценить влияние на кредитный профиль",
    "insolvency": "сигнал банкротства/реструктуризации — критично для держателей",
    "operations": "операционное событие — оценить влияние на денежный поток",
    "capital_action": "событие структуры капитала — оценить долговую нагрузку",
    "reporting_delay": "задержка отчётности — риск-маркер прозрачности эмитента",
}

_state: dict | None = None


def model_label() -> str:
    """Versioned label for classified_by (e.g. 'cpu-rules-2026-09-08') --
    derived from the exported model's train cutoff."""
    return f"cpu-rules-{load_state()['meta']['trained_on']}"


def load_state(path: str | Path | None = None) -> dict:
    """Load (and memoize) the exported model. Explicit path bypasses the
    memoize so tests can score several models without reimport."""
    global _state
    if path is None and _state is not None:
        return _state
    with open(path or MODEL_PATH, encoding="utf-8") as f:
        state = json.load(f)
    if path is None:
        _state = state
    return state


def analyze(text: str) -> list[str]:
    """Tokenization shared verbatim with train time: sklearn is handed THIS
    callable as its analyzer, so train/runtime cannot drift. Whitespace runs
    collapse to one space; overlapping lowercase char n-grams of 3..5."""
    t = re.sub(r"\s+", " ", text.lower())
    lo, hi = NGRAM_RANGE
    return [t[i:i + n] for n in range(lo, hi + 1)
            for i in range(len(t) - n + 1)]


def _transform(text: str, state: dict) -> dict[int, float]:
    """sklearn TfidfVectorizer(sublinear_tf=True) semantics in pure python:
    count -> 1+ln(count) -> * idf -> L2 normalize. Empty (no known ngram)
    documents score as the all-zeros vector."""
    vocab, idf = state["vocab"], state["idf"]
    counts: dict[int, int] = {}
    for g in analyze(text):
        idx = vocab.get(g)
        if idx is not None:
            counts[idx] = counts.get(idx, 0) + 1
    if not counts:
        return {}
    vec = {i: (1.0 + math.log(c)) * idf[i] for i, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in vec.values()))
    return {i: v / norm for i, v in vec.items()} if norm else vec


def _score_head(head: dict, vec: dict[int, float]) -> list[tuple[str, float]]:
    classes = head["classes"]
    if not vec:
        uniform = 1.0 / len(classes)
        return [(c, uniform) for c in classes]
    dec = [head["b"][ci] + sum(v * coef[i] for i, v in vec.items())
           for ci, coef in enumerate(head["coef"])]
    if len(classes) == 2 and len(dec) == 1:
        # binary LR exports ONE margin row: P(classes[1]) = sigmoid(margin)
        z = max(-30.0, min(30.0, dec[0]))
        p1 = 1.0 / (1.0 + math.exp(-z))
        return [(classes[0], 1.0 - p1), (classes[1], p1)]
    if len(classes) == 2:  # multinomial-shaped binary: sigmoid of the margin gap
        z = max(-30.0, min(30.0, dec[1] - dec[0]))
        p1 = 1.0 / (1.0 + math.exp(-z))
        return [(classes[0], 1.0 - p1), (classes[1], p1)]
    peak = max(dec)  # multinomial LR: softmax of the margins
    exps = [math.exp(d - peak) for d in dec]
    total = sum(exps)
    return [(c, e / total) for c, e in zip(classes, exps)]


def classify_filing(title: str, url: str, issuer_name: str) -> dict | None:
    """Same contract as classify_ollama.classify_filing(). Never returns
    None for a loadable model -- the rules engine has no transient failures,
    and callers rely on the queue draining (see module docstring)."""
    state = load_state()
    text = f"{title} || {issuer_name or ''}"
    vec = _transform(text, state)
    issuer = (issuer_name or "").strip() or "Эмитент"
    short_title = title.strip()[:140]
    if not vec:
        # OOV fallback: nothing in the text is in the model's vocabulary, so
        # it cannot be scored -- route it to the review queue (confidence 0.0
        # trips the 0.65 gate). A uniform distribution would put P(ALERT) at
        # exactly 0.5 and falsely fire the escalation.
        return {
            "category": "routine", "severity": "noise", "confidence": 0.0,
            "summary": (f"{issuer}: {short_title} — модель не распознала текст, "
                        f"требуется ручная проверка"),
        }
    # key=cp[1] -- max() over (class, proba) tuples otherwise compares the
    # CLASS STRINGS first and returns the alphabetically-largest class
    # whenever probabilities are close (caught live: 'high' > 'critical').
    category, p_cat = max(_score_head(state["heads"]["category"], vec),
                          key=lambda cp: cp[1])
    severity, p_sev = max(_score_head(state["heads"]["severity"], vec),
                          key=lambda cp: cp[1])
    alert_p = dict(_score_head(state["heads"]["alert"], vec)).get("ALERT", 0.0)

    # Coherence guards -- the three heads predict independently, so cross-head
    # contradictions get resolved conservatively instead of reaching the feed.
    if category == "routine" and severity in ("high", "critical"):
        severity = "medium"          # routine disclosures are never critical
    if severity == "noise" and category != "routine":
        severity = "low"             # 'noise' is only ever a routine severity
    if alert_p >= 0.5 and severity in ("noise", "low", "medium"):
        severity = "high"            # alert head escalates into review

    issuer = (issuer_name or "").strip() or "Эмитент"
    short_title = title.strip()[:140]
    summary = (f"{issuer}: {short_title} — "
               f"{_CATEGORY_TAKEAWAY.get(category, _CATEGORY_TAKEAWAY['routine'])}")
    return {
        "category": category,
        "severity": severity,
        "confidence": round(min(p_cat, p_sev), 4),
        "summary": summary,
    }
