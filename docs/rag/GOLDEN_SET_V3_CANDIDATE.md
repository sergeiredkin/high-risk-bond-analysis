# Golden set v3 — substantive issuer facts, agent-checked candidate

**Prepared:** 2026-10-12
**Status:** candidate only; independent human review is deferred. All records remain `agent-source-check`.

## Dataset design

- `eval/golden/ru_bonds_v3_candidate.jsonl` contains 34 answerable questions grounded in the 30-document HTML fixture corpus in `eval/corpus/manifest_v2.jsonl`.
- It replaces issuer-name/title lookups with bond-specific facts: coupon rates and amounts, coupon periods, partial amortization, early-redemption terms, default amounts, listing status, and maturity. Coverage spans six question categories; answers are numeric or dates, and each has an exact page-1 source quote.
- The split is by issuer group, not merely by document: 20 dev questions cover six issuers; 14 test questions cover four entirely unseen issuers. No issuer appears in both splits, and question/answer pairs do not cross the split.
- This is a **positive retrieval candidate only**. It has no unanswerable questions, OCR pages, scanned documents, cross-document reasoning, or source-conflict cases. The v2 candidate remains the older bootstrap/smoke set; its artificial no-answer items are not promoted here.

## Temporary-DB FTS5 diagnostic (before page-context bundling)

Ingested the fixture corpus into a temporary SQLite database and ran the existing retrieval evaluator at `k=5`. No production database was read or changed.

| Split | Questions | Page recall@5 | Citation precision@5 | MRR | Retrieved evidence contains expected numeric/date |
|---|---:|---:|---:|---:|---:|
| Dev | 20 | 1.000 | 0.775 | 1.000 | 0.700 |
| Test (unseen issuers) | 14 | 1.000 | 0.845 | 1.000 | 0.500 |

These are small-candidate diagnostics, not quality claims. Page recall is especially easy on the one-page HTML fixtures; the lower answer-evidence match showed that retrieving a relevant page did not guarantee retrieving the chunk containing the queried value. The evaluator does not generate answers, and answer-abstention accuracy remains null.

## Bounded page-context experiment (dev-only diagnostic)

Sparse and hybrid retrieval now return one result per document/page, with a context bundle containing the top-ranked chunk and up to 15 additional unique chunks from that same page. The bundle is capped at 12,000 characters; dense results are also collapsed to one page before return. This preserves page-level citations while avoiding a single matching chunk hiding the answer elsewhere on the retrieved page.

A follow-up temporary-DB run on the 20-question dev split at `k=5` kept page recall@5 (1.000), citation precision@5 (0.775), and MRR (1.000) unchanged; retrieved evidence containing the expected numeric/date value increased from 0.700 to 1.000. This is a development-set engineering diagnostic, not a quality claim. Because the fixtures are one-page documents, the 1.000 result measures expected-value availability in the returned page bundle, not top-chunk ranking quality; the gain is context expansion, not a demonstrated ranking improvement. The post-change held-out test metric was not extracted or used to tune the implementation; no threshold is proposed. Full-suite regression checks execute the shared evaluation harness but do not assert a test-set score.

## Limits / next steps

- The data is agent-checked against archived local HTML; neither the archive's identity nor the reconstructed AZIPI URLs have been independently verified against the publisher. See the source-provenance note in `GOLDEN_SET_V2_CANDIDATE.md`.
- A human should later verify each question, answer, quote, date, ISIN, units, and issuer holdout before this can become a frozen benchmark. Do not tune on the test split.
- Add realistic, explicitly scoped no-answer cases only when answer-level abstention can be evaluated; add genuine scan/OCR, table, cross-document, and conflicting-source cases separately.
