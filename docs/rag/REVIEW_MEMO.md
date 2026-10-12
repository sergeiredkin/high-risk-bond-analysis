# Independent RAG checkpoint review memo

**Review requested:** 2026-10-12
**Branch:** `rag/relevance-and-cbr-routing`
**Base at checkpoint:** `941f0f6` (`feat(rag): bundle bounded page context in search`, PR #22)
**Purpose:** independent technical/data-boundary review before any merge or production promotion. This memo is a review brief, not a readiness approval.

## Request to the reviewing model

Please inspect the branch diff against `941f0f6` and report concrete defects, unsafe assumptions, incorrect data interpretation, migration/runtime risks, and missing tests. Prioritize correctness and provenance over feature completeness. Do not modify the live production database, run pipeline/reprocessing commands, or claim production readiness. Do not treat a quote-presence check as relevance validation or agent-checked evaluation data as a human-reviewed benchmark.

## Review scope

The branch contains accumulated RAG changes from the current work, including:

- issuer- and exact-ISIN-scoped retrieval, bounded page context, citation verification and query-scope reporting;
- SQL-backed issuer and bond profiles, current-risk routing, and canonical rating resolution through `hybond.ratings.resolve()`;
- Nalog report metadata, exact-ISIN snapshot data, official payment-filing discovery, and conservative extraction of fields from official coupon-payment notices;
- local research UI/API routes and tests for these paths;
- updates to the RAG implementation plan and production-readiness audit.

Key review targets:

1. `src/hybond/rag/issuer_brief.py` and `bond_brief.py`: SQL scoping, latest-snapshot selection, units, null/stale handling, derived metrics, and source labeling.
2. `src/hybond/rag/current_risk.py`, `router.py`, `scripts/rag_server.py`: route precedence, issuer resolution, canonical rating-source handling, and missing-scope behavior.
3. `src/hybond/rag/payment_notices.py` and `filing_catalog.py`: official-source restriction, section parsing, field-level quote verification, duplicate suppression, and claims that remain issuer-reported rather than independently settled.
4. `src/hybond/rag/financial_reports.py`, `registry.py`, `ingest.py`: schema migration/idempotency, metadata backfill behavior, and safe bounded ingestion.
5. `src/hybond/rag/search.py`, `verify.py`: page-context ordering/limits and what citation verification does—and does not—prove.
6. `dashboard/rag.html`: HTML escaping, external-link handling, missing-field presentation, and labels distinguishing SQL snapshots, primary documents, classifier output, and secondary news.
7. Regression tests in `tests/test_rag_*.py`; run the full suite and inspect whether tests establish behavior rather than implementation text.

## Data and evidence boundaries

- SQLite is the source of truth. SQL-backed metrics are not document quotes; retrieval citations are not proof that a passage answers the question.
- Percent/fraction conventions differ by source. Review every conversion: universe `ytm` is stored as a fraction; `coupon_pct` is already percent; G-spread is basis points; Russian financial statement amounts are in thousands of RUB and the pillar ratios are unitless.
- Financial snapshots may be non-consolidated RСБУ. The profile exposes period, audit/consolidation flags and source-file metadata; do not present those ratios as consolidated group accounts.
- `event` and `market_note` include classifier-derived records. `needs_review`, confidence, source and authority must remain visible; secondary news is not an official issuer disclosure.
- The payment parser checks quotes against indexed page text. The 100% figure is the issuer's disclosure, not independent confirmation that money reached every holder.
- A filing-table title/date/link is metadata only unless its disclosure body is indexed.
- Missing coupon schedules, next-coupon dates, or unindexed source content must remain missing, not inferred.

## Corpus / local-only data

The production SQLite database and raw filing corpus are **not part of this branch**. The database is ignored by Git (`data/`); local paths and media are not staged. A read-only inspection of the primary DB found 82 issuer records, 379 bonds, 67,199 universe snapshots, 6,055 event rows, 2,708 market notes, 6,050 filing metadata rows, and 2,369 indexed documents. Most indexed documents (2,210) are derived HyBond factlists; issuer-disclosure bodies number 72, Nalog JSON documents 48, and CBR documents 24. Filing metadata count must not be mistaken for indexed disclosure-body coverage.

Local-only review artifacts:

- Primary DB: `/home/sergei/Documents/high risk bond analysis/data/db/hybond.sqlite` (read-only for review; never write/rebuild from this task worktree).
- Full temporary SQLite backup: `/tmp/hybond-rag-full-review/data/db/hybond.sqlite`; it includes the primary DB snapshot plus two targeted Segezha payment notices, and the local copy may be modified for review.
- Full-corpus review UI: `http://127.0.0.1:8780/rag.html` (localhost only; backed by the temporary copy).
- Earlier small RAG review copy/UI: `/tmp/hybond-rag-review/data/db/hybond.sqlite`, `http://127.0.0.1:8779/rag.html`.
- The downloaded HTML files are under `/tmp/hybond-rag-review/incoming/edisclosure/2026-10-02/`; they are not included in Git.

Do not commit a SQLite DB, raw filing, `data/dashboard_data.json`, PDF/image, temporary backup, local model, secret, or generated export. No production DB write or broad reprocessing was performed for this checkpoint.

## Reproduction and current validation

From the project checkout:

```bash
PYTHONPATH=src python3 -m pytest -q
ruff check src/hybond/rag scripts/rag_server.py tests/test_rag_*.py
git diff --check
python3 -m compileall -q src/hybond/rag scripts/rag_server.py
```

The current checkpoint passed **1,114 tests**, Ruff, compile, `git diff --check`, and `node --check` on the embedded dashboard JavaScript. The API/UI was also exercised against the temporary full DB copy for issuer profile, exact-ISIN profile, current-risk, and the two payment notices.

## Known limitations / review gates

- The v3 golden set remains an agent-checked, positive-only candidate; no independent human review, frozen benchmark, or justified quality threshold exists.
- OCR retrieval remains disabled and has no genuine scanned issuer-PDF evaluation.
- Sparse/dense/hybrid quality has not been compared on a trustworthy Russian benchmark.
- Existing indexed Nalog sources need bounded reingestion for report-period metadata; broader issuer primary-body coverage and default/payment history remain incomplete.
- The issuer/bond profiles are deterministic SQL summaries, not an LLM answer or investment recommendation. Direct financial statement line-item Q&A, historical issuer/bond trend summaries, and reconciliation against independent authoritative sources remain future work.
- Do not call this production-ready. The project’s `AGENTS.md` says production runs from the primary checkout and task worktrees must not run pipeline commands there. No-mistakes is therefore not to be run from this PR clone.

## Git/promotion boundary

This memo and source/tests/docs are appropriate branch content for independent GitHub review. The production DB, raw corpus, temp backups, and local UI state stay on disk and must not be pushed. The audit memo will reach the primary checkout through the normal GitHub merge/pull path; do not edit the live production checkout from this task worktree.
