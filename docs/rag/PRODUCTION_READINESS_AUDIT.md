# HyBond RAG Production-Readiness Audit

**Audit date:** 2026-10-07
**Scope:** original `docs/rag/RAG_IMPLEMENTATION_PLAN.md` and current implementation

## Executive result

**Not production-ready.** The current system is a useful P0/P1 retrieval prototype with an evaluation runner, but it is not yet a complete cited answering system. The first evaluation is intentionally a baseline and exposes weak retrieval/abstention behavior that must be fixed before adding a UI or trusting answers.

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
| Basic local ingestion | **Partial** | text/HTML and `pdftotext`; no layout parser |
| Incremental indexing | **Partial** | hourly bounded ingestion now hash-deduplicates multiple source directories; no index build/version switch |
| Page-aware chunks and citations | **Partial** | page numbers work; no bounding boxes or reliable PDF elements |
| FTS5/BM25 baseline | **Done** | `rag/search.py` |
| Evaluation runner | **Partial** | retrieval/evidence metrics now include citation precision; no answer-generation evaluation |
| Dense embeddings | **Scaffold only** | optional adapter; no local model/index currently built |
| RRF hybrid retrieval | **Done** | injection seam exists; not validated against dense results |
| Cross-encoder reranking | **Missing** | not implemented |
| OCR for scanned Cyrillic PDFs | **Missing** | blank pages are marked scanned but not OCR'd |
| Tables as structured data | **Missing** | HTML is flattened to text |
| Page rendering/visual retrieval | **Missing** | no images, bbox, ColPali/ColQwen path |
| Structured fact extraction | **Partial** | conservative rule extractor now stores INN/ISIN/explicit unpaid amounts; coupons, schedules, covenants, financials, and LLM-assisted extraction remain |
| MOEX/HyBond reconciliation | **Partial** | INN/ISIN matching now marks match/mismatch; monetary facts remain unverifiable and full source-priority reconciliation is pending |
| Numeric SQL answer path | **Partial** | `answer_fact()` routes matched/reconciled facts; current-risk questions now combine dated SQL risk signals with document retrieval, but full numeric schemas and reconciliation remain |
| Query routing | **Partial** | The local API/UI applies deterministic numeric/narrative/mixed/current-risk/out-of-scope routing; the CLI still exposes a lower-level fact-or-retrieval path and local answer generation remains missing |
| Local answer generation | **Missing** | CLI returns retrieved evidence only |
| Citation verification/abstention | **Partial** | retrieved citations now carry exact quotes and are checked against indexed document/page; claim-level generated-answer verification remains |
| Versioned indexes/rollback | **Partial** | create/activate/rollback bookkeeping now exists; atomic index build/switch is still pending |
| Tracing/access control | **Partial** | local query telemetry logs route, hit count, top-source authority, abstention, and feedback; access control remains intentionally local-only |
| E-disclosure ingestion integration | **Partial** | incremental `docs ingest-dir` is now wired into hourly filings for recent local raw files; downloader/source coverage and historical backfill policy remain |
| HyBond `spravka_vdo.md` reference corpus | **Done (bootstrap)** | indexed as `hybond_reference_spravka`; useful domain guidance, not an authoritative issuer-data source |
| Evaluation CI and eval diff | **Partial** | `hybond docs eval-diff` compares baseline/candidate metrics and exits non-zero on regression; CI wiring remains |
| FastAPI/dashboard research interface | **Partial** | local-only stdlib RAG page/API at port 8779; no generated-answer panel or production auth |

## Required order to become production-ready

### RAG-1 — Retrieval correctness gate

1. Expand the golden set to 60–100 questions across at least 10 issuers.
2. Manually verify every expected value and evidence page.
3. Continue improving Russian stemming/synonym handling and query intent routing; issuer/ISIN scope and basic stemming are now implemented.
4. Implement dense index construction with a pinned locally cached multilingual model.
5. Compare FTS5 vs hybrid retrieval on the frozen dev set.
6. Set acceptance thresholds only after the corpus and questions are manually verified.

**Gate:** page recall@5 and citation precision targets met on dev; frozen test remains untouched.

### RAG-2 — Document fidelity

1. Replace HTML/PDF flattening with layout-aware parsing.
2. Add Cyrillic OCR fallback and confidence fields.
3. Preserve tables as structured rows plus searchable table chunks.
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
6. Expose a local-only API/dashboard panel only after the answer verifier passes.

**Gate:** restart/rebuild/rollback tests pass and production cron cannot be blocked by indexing.

## Important conclusion

Do not describe the current implementation as a production RAG or use it for investment decisions. It currently proves that HyBond can register documents, retrieve text, and produce source identifiers. It does not yet prove that generated answers are complete, numerically correct, visually grounded, or safe under missing evidence.
