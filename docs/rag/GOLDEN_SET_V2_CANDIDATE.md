# Golden set v2 — agent-checked candidate, not frozen

**Prepared:** 2026-10-09
**Status:** candidate only; independent human verification is still required.

## Corpus and questions

- `eval/corpus/manifest_v2.jsonl` references 30 archived AZIPI e-disclosure HTML files from 10 issuers (about 1 MB total), SHA-256 validated against the local files. Each row now carries a disclosure ID, the visible publication date, and the existing retrieval date. Its specific AZIPI `/messages/<id>/` URL is reconstructed from the archived login `backurl`; the endpoint and identity of the archived bytes were not independently re-verified against the official publisher.
- `eval/golden/ru_bonds_v2.jsonl` contains 72 questions: 60 answerable issuer-name/event-title lookups and 12 corpus-bounded no-answer probes.
- The split is document-disjoint: 48 dev questions and 24 test questions. Each split has at least 15% no-answer probes, and no evidence source document occurs in both splits. Evidence entries contain an exact source quote, page, and source hash.
- Entries are marked `agent-source-check`, not `human-verified`. The answerable questions mostly test document identification and event-title retrieval; they do not adequately test financial analysis.

## Offline FTS5 diagnostic

Evaluated by ingesting the committed fixtures into a temporary SQLite database; no production database was read or changed by the evaluation run.

| Split | Questions | Page recall@5 | Citation precision@5 | MRR | Auto answer/evidence match | No-answer queries with empty retrieval |
|---|---:|---:|---:|---:|---:|---:|
| Dev | 48 | 0.833 | 0.604 | 0.701 | n/a (40 manual) | 0.000 |
| Test | 24 | 0.833 | 0.653 | 0.736 | n/a (20 manual) | 0.000 |

All answerable questions are text answers, so the automatic answer/evidence metric is null rather than incorrectly reporting zero; all 60 require manual scoring. The runner evaluates retrieval only: the 12 no-answer probes retrieve hits, so the diagnostic `unanswerable_retrieval_empty_rate` is 0.000. `answer_abstention_accuracy` is null because no generated answer is being graded. These figures do not measure answer abstention; do not calibrate or claim a passing abstention gate from this candidate set.

## Coverage gaps

This bootstrap is **not** the planned evaluation benchmark: it has no manually reviewed financial answers, genuine scanned PDFs, OCR pages, representative financial-table questions, cross-document reasoning, or conflicting-source cases. Its 30-document archive remains HTML-only. A separate official CBR PDF and clean/degraded rasterized test surrogates now exercise table parsing and scan detection; see `DOCUMENT_FIDELITY_FIXTURES.md`. The HTML parser smoke run produced 120 structured table elements / 540 cell records, including site/template tables, and is not a table-quality benchmark. A local multilingual embedding model was not found in the cache. Tesseract is now installed on this host; the surrogate OCR smoke results are documented in `DOCUMENT_FIDELITY_FIXTURES.md`, but no representative OCR comparison exists. Therefore no valid dense-vs-FTS5 or OCR quality comparison is claimed. The cached English MiniLM model is deliberately not used as a Russian retrieval benchmark.

Next: review every question and citation independently, replace generic questions with financial narrative/table questions, add representative table, scan, multi-document, and source-conflict fixtures, then freeze the test split. Do not tune against the test split.
