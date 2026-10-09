# HyBond RAG Production-Readiness Audit

**Audit date:** 2026-10-09
**Scope:** original `docs/rag/RAG_IMPLEMENTATION_PLAN.md` and current implementation

## Executive result

**Not production-ready.** The system remains a P0/P1 retrieval prototype, not a complete cited answering system. A 72-question, 10-issuer agent-checked fixture candidate is now available with document-disjoint dev/test splits, but it is not human-verified, has no table/scan/cross-document coverage, and reports weak held-out retrieval and zero abstention accuracy. A local research UI/API exists, but it does not generate answers. See [`GOLDEN_SET_V2_CANDIDATE.md`](GOLDEN_SET_V2_CANDIDATE.md).

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
| OCR for scanned Cyrillic PDFs | **Partial (experimental)** | opt-in Tesseract hook can store page text and mean word confidence locally, but does not index OCR text or expose it as answer evidence; no system Tesseract/rus package or representative OCR evaluation. A clean raster surrogate has only a smoke result and tests scan detection |
| Tables as structured data | **Partial** | HTML tables are persisted as row/cell JSON plus canonical HTML and separately indexed; PDF rows use a conservative `pdftotext -layout` spacing heuristic and preserve raw strings, not verified numeric semantics. General PDF layout, merged-cell meaning, and a representative human-reviewed table benchmark are still missing |
| Page rendering/visual retrieval | **Missing** | no images, bbox, ColPali/ColQwen path |
| Structured fact extraction | **Partial** | conservative rule extractor now stores INN/ISIN/explicit unpaid amounts; coupons, schedules, covenants, financials, and LLM-assisted extraction remain |
| MOEX/HyBond reconciliation | **Partial** | INN/ISIN matching now marks match/mismatch; monetary facts remain unverifiable and full source-priority reconciliation is pending |
| Numeric SQL answer path | **Partial** | `answer_fact()` routes matched/reconciled facts; current-risk questions now combine dated SQL risk signals with document retrieval, but full numeric schemas and reconciliation remain |
| Query routing | **Partial** | The local API/UI applies deterministic numeric/narrative/mixed/current-risk/out-of-scope routing; the CLI still exposes a lower-level fact-or-retrieval path and local answer generation remains missing |
| Local answer generation | **Missing** | CLI returns retrieved evidence only |
| Citation verification/abstention | **Partial** | retrieved citations now carry exact quotes and are checked against indexed document/page; claim-level generated-answer verification remains |
| Versioned indexes/rollback | **Partial** | dense artifact builds use a temporary directory and atomic rename; rollback bookkeeping exists, but artifact activation is not transactionally coupled to SQLite version state |
| Tracing/access control | **Partial** | local query telemetry logs route, hit count, top-source authority, abstention, and feedback; access control remains intentionally local-only |
| E-disclosure ingestion integration | **Partial** | incremental `docs ingest-dir` is wired into hourly filings for recent local raw files; bounded, explicit-date historical backfill is available manually (see the [README command reference](../../README.md#slice-5-local-document-intelligence--rag)); downloader/source coverage remains |
| HyBond `spravka_vdo.md` reference corpus | **Done (bootstrap)** | indexed as `hybond_reference_spravka`; useful domain guidance, not an authoritative issuer-data source |
| Evaluation CI and eval diff | **Partial** | CI now runs the committed v2 corpus integrity/split-isolation evaluation test; eval-diff checks comparable splits and dataset hashes, but no quality threshold is enabled because the candidate is not human-verified |
| FastAPI/dashboard research interface | **Partial** | local-only stdlib RAG page/API at port 8779; no generated-answer panel or production auth |

## Required order to become production-ready

### RAG-1 — Retrieval correctness gate

1. A 72-question, 10-issuer agent-checked candidate and document-disjoint dev/test split now exist; the benchmark is still not manually verified or frozen.
2. The held-out candidate result is page recall@5 0.833, citation precision 0.653, and abstention accuracy 0.000; all 20 answerable items still require manual scoring, and these results do not pass an acceptance gate.
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
4. Implement parameterized SQL answers for numeric questions.
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

## Important conclusion

Do not describe the current implementation as a production RAG or use it for investment decisions. It currently proves that HyBond can register documents, retrieve text, and produce source identifiers. It does not yet prove that generated answers are complete, numerically correct, visually grounded, or safe under missing evidence.
