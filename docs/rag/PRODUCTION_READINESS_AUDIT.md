# HyBond RAG Production-Readiness Audit

**Audit date:** 2026-10-09; follow-up diagnostics: 2026-10-12, 2026-10-15–16
**Scope:** original `docs/rag/RAG_IMPLEMENTATION_PLAN.md` and current implementation

## Executive result

**Not production-ready.** The system remains a P0/P1 retrieval prototype, not a complete cited answering system. The 72-question v2 bootstrap and 34-question v3 substantive candidate are both agent-checked, not human-verified. V3 uses an unseen-issuer test split but remains a small positive-only set; retrieved answer-evidence match is weak, and it has no table/scan/cross-document coverage. The retrieval-only evaluator has no answer-abstention score; its no-answer empty-retrieval diagnostic is not answer safety. A local research UI/API exists, including deterministic SQL profiles and an exact-bond snapshot trend comparison, but it does not generate answers. See the candidate-specific evaluations in [`GOLDEN_SET_V2_CANDIDATE.md`](GOLDEN_SET_V2_CANDIDATE.md) and [`GOLDEN_SET_V3_CANDIDATE.md`](GOLDEN_SET_V3_CANDIDATE.md).

Baseline command:

```bash
PYTHONPATH=src python3 -m hybond docs eval \
  --golden eval/golden/ru_bonds_v1.jsonl \
  --manifest eval/corpus/manifest_v1.jsonl \
  --k 5 --out data/rag_eval_v1.json
```

The current bootstrap metrics are generated locally under `data/` when `--out` is supplied. They are not acceptance results: the draft golden set has not been manually frozen.

## Plan coverage

| Planned capability | Status | Evidence / gap |
|---|---|---|
| SHA-256 manifest and deduplication | **Done** | `rag/manifest.py`, registry |
| SQLite document/page/chunk registry | **Done** | `rag/registry.py`; migrated production DB |
| Source registry and authority levels | **Done (bootstrap)** | source_kind/authority_level stored for CBR, ratings, Nalog, factlists, e-disclosure, and reference material |
| Basic local ingestion | **Partial** | text/HTML and `pdftotext`; HTML tables are separate structured/searchable chunks; aligned numeric PDF rows are heuristically separated into table chunks. PDF bounding boxes and general layout elements are absent |
| Incremental indexing | **Partial** | hourly bounded ingestion now hash-deduplicates multiple source directories; no index build/version switch |
| Page-aware chunks and citations | **Partial** | page numbers work; no bounding boxes or reliable PDF elements |
| FTS5/BM25 baseline | **Done** | `rag/search.py` |
| Evaluation runner | **Partial** | dev is the default split; frozen test is opt-in; reports hash the golden/manifest and eval-diff rejects split/corpus mismatches. The v2 candidate is agent-checked, not human-reviewed; narrative answer scoring remains manual |
| Dense embeddings | **Partial** | optional local encoder/index is integrated into CLI/API retrieval via `HYBOND_RAG_DENSE_INDEX` + `HYBOND_RAG_MODEL_PATH`, with issuer/source/type filtering and RRF; no pinned model or quality comparison yet |
| RRF hybrid retrieval | **Partial** | CLI/API fuse filtered local dense hits with FTS results; retrieval quality has not been compared against dense results |
| Cross-encoder reranking | **Missing** | not implemented |
| OCR for scanned Cyrillic PDFs | **Partial (experimental)** | opt-in Tesseract hook stores page text/mean confidence and preserves TSV line breaks, but does not index OCR text or expose it as answer evidence; this host has Tesseract 5.3.4/rus, but other deployments are not assumed to. No representative OCR evaluation; clean/degraded synthetic raster surrogates have smoke results only |
| Tables as structured data | **Partial** | HTML tables are persisted as row/cell JSON plus canonical HTML and separately indexed; PDF rows use a conservative `pdftotext -layout` spacing heuristic and preserve raw strings, not verified numeric semantics. General PDF layout, merged-cell meaning, and a representative human-reviewed table benchmark are still missing |
| Page rendering/visual retrieval | **Missing** | no images, bbox, ColPali/ColQwen path |
| Structured fact extraction | **Partial** | conservative rule extractor stores INN/ISIN/explicit unpaid amounts; exact-ISIN queries can expose selected current `universe_snapshot` fields, but coupon rate may be null and next-coupon dates are not inferred; schedules, covenants, and financial extraction remain |
| MOEX/HyBond reconciliation | **Partial** | INN/ISIN matching now marks match/mismatch; monetary facts remain unverifiable and full source-priority reconciliation is pending |
| Numeric SQL answer path | **Partial** | `answer_fact()` routes matched/reconciled facts; exact-ISIN snapshot answers show requested current fields without inventing missing values. `issuer_brief_summary()` and `bond_brief_summary()` provide dated SQL profiles; `bond_history_summary()` compares exact-ISIN YTM, G-spread, and clean-price snapshots over a bounded window and withholds yield/spread deltas if calculation source changes. These are source-attributed DB values, not document quotes; issuer-wide trends, financial-statement line-item Q&A, full-corpus history coverage, and full reconciliation remain |
| Query routing | **Partial** | The local API/UI applies deterministic numeric/narrative/mixed/current-risk/issuer-brief/bond-brief/bond-trend/out-of-scope routing; current-risk and profile ratings use `ratings.resolve()`. The CLI still exposes a lower-level fact-or-retrieval path and local answer generation remains missing |
| Local answer generation | **Missing** | CLI returns retrieved evidence only |
| Citation verification/abstention | **Partial** | retrieved citations carry exact quotes and are checked against indexed document/page. This verifies quote presence, not relevance; claim-level generated-answer verification remains |
| Versioned indexes/rollback | **Partial** | dense artifact builds use a temporary directory and atomic rename; rollback bookkeeping exists, but artifact activation is not transactionally coupled to SQLite version state |
| Tracing/access control | **Partial** | local query telemetry logs route, hit count, top-source authority, abstention, and feedback; access control remains intentionally local-only |
| E-disclosure ingestion integration | **Partial** | incremental `docs ingest-dir` handles recent local raw files; `docs fetch-manifest` now downloads/indexes explicitly reviewed, allowlisted direct PDF/ZIP URLs with size/rate/identity checks and source metadata. It does not discover filings, bypass the e-disclosure.ru WAF, or run on a schedule; no-bodies remains the hourly scrape mode. See the [README command reference](../../README.md#slice-5-local-document-intelligence--rag). |
| HyBond `spravka_vdo.md` reference corpus | **Done (bootstrap)** | indexed as `hybond_reference_spravka`; useful domain guidance, not an authoritative issuer-data source |
| Evaluation CI and eval diff | **Partial** | CI now runs the committed v2 corpus integrity/split-isolation evaluation test; eval-diff checks comparable splits and dataset hashes, but no quality threshold is enabled because the candidate is not human-verified |
| Dashboard research interface | **Partial** | one-shell `Документы` page and same-origin app server are implemented; standalone localhost RAG page remains the fallback. Production systemd is still serving the static dashboard and has not been switched over. No generated answers or production auth. A tailnet cutover must preserve the existing dashboard-only-on-loopback/Tailscale boundary and be explicitly tested. |

## Required order to become production-ready

### RAG-1 — Retrieval correctness gate

1. The v2 candidate has 72 questions and document-disjoint splits; v3 adds 34 substantive positive questions with an unseen-issuer test. Neither candidate is manually verified or frozen.
2. V2 held-out page recall@5 is 0.833 and citation precision 0.653. V3's initial test diagnostic was page recall@5 1.000 and answer-evidence match 0.500 over 14 questions. A later bounded page-context experiment raised dev answer-evidence availability from 0.700 to 1.000 without changing other dev metrics; because fixtures are one-page, this is context expansion, not a ranking improvement. Its post-change test score was not reviewed or used to tune. Neither result establishes answer correctness. V2's 0.000 no-answer retrieval-empty rate is a retrieval diagnostic, not answer-abstention accuracy. No candidate passes an acceptance gate.
3. No suitable cached multilingual embedding model was found; the cached English MiniLM model is not used for Russian quality claims.
4. Compare FTS5 vs hybrid retrieval only after a suitable local model and human-reviewed benchmark are available.
5. Set thresholds only after the corpus/questions have been independently verified.

**Gate:** page recall@5 and citation precision targets met on dev; frozen test remains untouched.

### RAG-2 — Document fidelity

1. Replace HTML/PDF flattening with layout-aware parsing.
2. Add Cyrillic OCR fallback and confidence fields.
3. Extend structured-table preservation to PDFs and evaluate table structure on representative financial documents.
4. Render pages and store element bounding boxes.
5. Add fixtures for digital, scanned, table-heavy, and malformed documents.

**Gate:** every citation can identify source, page, and element/quote where available.

### RAG-3 — Financial truth layer

1. Implement fact schemas and extraction for coupon, schedule, maturity, put/call, amortization, guarantor, covenant, proceeds, volume, and financial metrics.
2. Reconcile facts against existing HyBond/MOEX data.
3. Store mismatches, never overwrite silently.
4. Extend parameterized SQL answers to financial-statement line items and issuer-wide time trends; exact-bond market snapshot trends are implemented but need full-corpus coverage review.
5. Add tolerance-aware numeric/date evaluation.

**Gate:** >=95% exact-match on answerable numeric dev questions and all mismatches visible.

### RAG-4 — Answering and safety

1. Add query router: numeric, narrative, mixed, out-of-scope.
2. Add local Ollama/VL generation with Russian prompts.
3. Require claim-level citations.
4. Verify every citation against retrieved text/page/bbox.
5. Abstain on missing or contradictory evidence.
6. Add prompt-injection defenses for document text.

**Gate:** no uncited generated claim can leave the answer service.

### RAG-5 — Operations

1. Build immutable index versions and atomic activation/rollback.
2. Add incremental ingestion jobs with locks and resource limits.
3. Keep binaries outside SQLite; define retention and backup policy.
4. Add tracing, latency/token metrics, and error audit logs.
5. Add CLI eval-diff and CI regression gates.
6. The local-only research API/dashboard exists; add generated answers only after the answer verifier passes.

**Gate:** restart/rebuild/rollback tests pass and production cron cannot be blocked by indexing.

## 2026-10-12 local query diagnostics

A temporary copy of the local corpus was used for a follow-up pass; no production DB was written. The review UI continues to return evidence, not generated answers. Changes include issuer-aware snippet windows, bounded chunks for long Nalog JSON records, an exact-ISIN SQL snapshot panel that labels absent fields instead of inventing them, and CB/CBR/ЦБ alias handling. Forecast queries scoped to CBR now prefer dated direct `forecast.pdf` sources over material indexes, with publication date used only to break ties.

Observed outcomes are diagnostic, not benchmark results: the CBR query without a company now returns the 2026-07-24 forecast PDF and its quoted rate range; exact-ISIN coupon queries show the stored 91-day period but leave coupon rate and next payment date unavailable when the snapshot has no value. Q2 default/non-payment wording still has no matching evidence. The two official Segezha «Выплаченные доходы» bodies were fetched into the temporary corpus and parsed: both report full coupon payment on 2026-10-02 for series 003P-10R (ISIN RU000A10E747) and 003P-06R (ISIN RU000A10CB66); each displayed field is backed by a page-verified quote. This confirms two issues on the same date/period, not payment messages across different dates, and it is the issuer's disclosure rather than independent settlement confirmation. An all-source offer query found secondary commentary, not an official disclosure. Nalog report metadata now identifies 2025–2021 periods, publication dates, corrections, and official report URLs for the two reviewed issuers; each returned metadata field is checked against the indexed JSON. The statement contents still need review, and existing indexed Nalog documents need bounded reingestion before this metadata is available for them. The current-risk and issuer/bond profile routes now use canonical rating resolution (MOEX CCI before legacy manual rows); the Segezha sample resolves to MOEX `ruB`. Rating/event provenance and legacy edge cases still need independent scrutiny.

### Existing DB coverage and structured profiles (read-only inspection, 2026-10-12)

The production DB contains 82 issuer records, 379 bonds, 67,199 universe snapshots, 6,055 events, 2,708 market notes, and 6,050 filing metadata rows. Its 2,369 indexed documents comprise 2,210 derived HyBond factlists, 72 issuer e-disclosures, 48 Nalog financial JSON documents, 24 CBR documents, 12 secondary-commentary documents, and three reference/rating documents. The large SQL history is enough for useful dated issuer/bond profiles; it is not equivalent to broad primary-document coverage. Filing rows are metadata, not disclosure bodies.

The new issuer and exact-ISIN profile routes were exercised against a temporary SQLite backup, not the live DB. For Segezha, the profile returned the 2026-10-12 market snapshot (9 bonds), canonical MOEX-resolved `ruB`, 2025 RСБУ ratios, recent events/rating changes, and clearly separated secondary news leads and filing metadata. The sample RСБУ statement is audited but non-consolidated; ratios are raw calculated metrics, not a generated credit opinion. The exact-ISIN profile returned current available market fields and explicitly left absent coupon dates uncalculated. No production DB writes were made.

These targeted examples do not repair the candidate golden set, prove query relevance broadly, or justify a quality gate. V3 remains agent-checked; no independent review, OCR readiness, generated answers, or production-readiness claim is established.

## 2026-10-15–16 bounded acquisition follow-up

A clean-room, manifest-driven `hybond docs fetch-manifest` path was added. It is deliberately a downloader/indexer, not a catalog crawler: direct HTTPS file/catalog URLs must be supplied in a reviewed JSONL manifest and match approved e-disclosure hosts. Per-run limits, streamed byte caps, redirect-host checks, PDF/ZIP validation, SHA-256 naming, rate spacing, optional printed-INN verification, and per-item failure output are enforced. SQLite records download URL, catalog URL, title, publication date, identity-check status, fetch time, document type, and hash; the raw PDF has a JSON sidecar. It is not scheduled and has no browser/WAF automation.

In a separate read-only comparison, the unlicensed `grind-t/e-disclosure-py` repository retrieved recent Segezha, Binnopharm, and Poliplast interim IFRS PDFs (three issuers; Segezha supplied Russian and English copies). HyBond's existing PDF parser indexed the four resulting PDFs in an in-memory DB (96 pages, 331 chunks); a revenue query returned a relevant Segezha page/quote. The repository was executed only in `/tmp` for evaluation and was not adopted or copied. This is a small positive sample, not a source-coverage or quality benchmark.

A control fetch of the Nalog URL recorded for Segezha (ИНН 9703024202, 2025) returned ООО «Карат» because that upstream URL used the organization ID in `/download/bfo/pdf/{id}` and supplied the report `detail_id` only as a query parameter. The endpoint is keyed by report `detail_id`: direct downloads via `/download/bfo/pdf/{detail_id}` returned PDFs whose filename and first-page INN matched Segezha, Seligdar (1402047184), and APRI (7453326003). `rag/financial_reports.py` now constructs that canonical URL, preserves the upstream URL and construction method, and derives corrected links for existing indexed metadata on read; no production DB was written. Re-indexing raw Nalog JSON will persist the corrected canonical URL and reported URL separately. The new e-disclosure manifest importer rejects a printed issuer-INN mismatch and requires an explicit flag for PDFs that omit an INN, after the catalog mapping has been checked.

## 2026-10-16 unified research app follow-up

The main dashboard now has a `Документы` route that embeds the existing cited research interface. `scripts/hybond_app_server.py` serves the built dashboard and the RAG API on one origin, avoiding cross-origin browser calls; the original `scripts/rag_server.py` remains a localhost-only fallback. The combined server defaults to loopback. Production systemd configuration was not changed: it still serves the static dashboard, so the integrated route is not live there. A future cutover must update the working directory to the production checkout (the RAG database path is relative), retain the two explicit loopback/Tailscale binds, test with a disposable DB, and confirm that exposing the RAG API to the same tailnet clients as the dashboard is acceptable. This is UI/service integration, not a readiness upgrade: answers are still evidence excerpts and deterministic SQL profiles, not generated conclusions.

## Important conclusion

Do not describe the current implementation as a production RAG or use it for investment decisions. It currently proves that HyBond can register documents, retrieve text, and produce source identifiers. It does not yet prove that generated answers are complete, numerically correct, visually grounded, or safe under missing evidence.
