# Golden set v3 — substantive issuer facts, agent-checked candidate

**Prepared:** 2026-10-12
**Status:** candidate only; independent human review is deferred. All records remain `agent-source-check`.

## Dataset design

- `eval/golden/ru_bonds_v3_candidate.jsonl` contains 34 answerable questions grounded in the 30-document HTML fixture corpus in `eval/corpus/manifest_v2.jsonl`.
- It replaces issuer-name/title lookups with bond-specific facts: coupon rates and amounts, coupon periods, partial amortization, early-redemption terms, default amounts, listing status, and maturity. Coverage spans six question categories; answers are numeric or dates, and each has an exact page-1 source quote.
- The split is by issuer group, not merely by document: 20 dev questions cover six issuers; 14 test questions cover four entirely unseen issuers. No issuer appears in both splits, and question/answer pairs do not cross the split.
- This is a **positive retrieval candidate only**. It has no unanswerable questions, OCR pages, scanned documents, cross-document reasoning, or source-conflict cases. The v2 candidate remains the older bootstrap/smoke set; its artificial no-answer items are not promoted here.

## Temporary-DB FTS5 diagnostic

Ingested the fixture corpus into a temporary SQLite database and ran the existing retrieval evaluator at `k=5`. No production database was read or changed.

| Split | Questions | Page recall@5 | Citation precision@5 | MRR | Retrieved evidence contains expected numeric/date |
|---|---:|---:|---:|---:|---:|
| Dev | 20 | 1.000 | 0.775 | 1.000 | 0.700 |
| Test (unseen issuers) | 14 | 1.000 | 0.845 | 1.000 | 0.500 |

These are small-candidate diagnostics, not quality claims. Page recall is especially easy on the one-page HTML fixtures; the lower answer-evidence match shows that retrieving a relevant page does not guarantee retrieving the chunk containing the queried value. The evaluator does not generate answers, and answer-abstention accuracy remains null.

## Limits / next steps

- The data is agent-checked against archived local HTML; neither the archive's identity nor the reconstructed AZIPI URLs have been independently verified against the publisher. See the source-provenance note in `GOLDEN_SET_V2_CANDIDATE.md`.
- A human should later verify each question, answer, quote, date, ISIN, units, and issuer holdout before this can become a frozen benchmark. Do not tune on the test split.
- Add realistic, explicitly scoped no-answer cases only when answer-level abstention can be evaluated; add genuine scan/OCR, table, cross-document, and conflicting-source cases separately.
