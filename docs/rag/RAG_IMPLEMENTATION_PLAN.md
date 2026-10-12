# HyBond RAG / Document Intelligence — Planning Phase

**Status:** phased implementation; P0 foundation started
**Date:** 2026-10-09
**Source:** [`00-source-note.md`](00-source-note.md), adapted to the existing HyBond architecture

### Progress

- **P0 foundation complete:** source note copied, manifest/golden validators added, SQLite registry migrations added, and offline tests added.
- **P1 baseline complete:** local text/Markdown/PDF-text ingestion, page-aware chunks, FTS5 retrieval, citations, and `hybond docs ingest/ask` CLI commands.
- **P2 retrieval seam complete:** reciprocal-rank fusion and an optional local dense encoder/index are available; `docs eval --dense-index ... --model-path ...` reports hybrid metrics alongside the FTS5 baseline. Usage and CLI details are in the [README command reference](../../README.md#slice-5-local-document-intelligence--rag). Dense models remain opt-in and are not installed automatically.
- **Corpus bootstrap complete:** 30 e-disclosure documents plus `spravka_vdo.md` are indexed; 23 e-disclosure documents received issuer metadata (INN/ISIN) from document content. The spravka is tagged `hybond_reference_spravka` and is guidance/reference material, not authoritative issuer data. Manifest copies are in `docs/rag/corpus_manifest_v1.jsonl` and `eval/corpus/manifest_v1.jsonl`.
- **Golden bootstrap complete:** 10 draft Russian questions, including abstention cases, are in `eval/golden/ru_bonds_v1.jsonl`. They are explicitly marked for human review and are not yet a frozen benchmark.
- **Evaluation runner hardened:** `docs eval` defaults to dev, requires an explicit `--split test`, hashes golden/manifest inputs, and reports manual-only answer scores as null rather than zero. `eval-diff` rejects split/corpus mismatches; no quality threshold is enabled until human verification.
- **Golden v2 candidate added:** 30 hashed e-disclosure fixtures across 10 issuers and 72 agent-checked questions with document-disjoint dev/test splits. It is not human-reviewed or frozen; held-out FTS5 page recall@5 0.833 and citation precision 0.653. The retrieval-only runner reports a no-answer retrieval-empty diagnostic; it does not measure answer abstention. See `GOLDEN_SET_V2_CANDIDATE.md`.
- **Golden v3 candidate added:** 34 substantive numeric/date questions across six categories, split by issuer group (20 dev / 14 unseen-issuer test). It remains agent-checked and positive-only; temporary-DB FTS5 answer-evidence match is 0.70 dev / 0.50 test despite page recall@5 of 1.00. These are diagnostics, not acceptance results; see `GOLDEN_SET_V3_CANDIDATE.md`.
- **Table fidelity advanced:** HTML tables are separate structured/searchable elements; a conservative `pdftotext -layout` heuristic preserves aligned numeric PDF rows as table chunks, with page-separator handling tested against an official CBR PDF fixture. It remains a layout heuristic, not a numeric fact parser; bounding boxes and semantic merged-cell handling remain incomplete. See `DOCUMENT_FIDELITY_FIXTURES.md`.
- **PDF/OCR progress:** an optional local Tesseract hook stores page text/confidence behind `HYBOND_RAG_OCR=1`; TSV line breaks are preserved, but OCR is not indexed. A clean raster and degraded synthetic CBR scan surrogate have one-page smoke diagnostics (PSM-dependent WER 4.63–22.15%); neither substitutes for a genuine scan or a representative OCR benchmark.
- **Production audit added:** `docs/rag/PRODUCTION_READINESS_AUDIT.md` lists every planned capability, current status, and the remaining gates.
- **Retrieval iteration complete:** issuer/ISIN-scoped search, lightweight Russian stemming, candidate reranking, and better metadata-aware evaluation improved the bootstrap page recall@5 from 0.30 to 0.70 in local evaluation reports.
- **Golden bootstrap expanded:** 15 draft questions now cover issuer disclosures and the spravka reference material.
- **Facts bootstrap complete:** `hybond docs extract` populated provenance-bearing facts; facts remain `unverifiable` until reconciled with authoritative HyBond/MOEX data.
- **Incremental ingestion wired:** `scripts/rag_ingest.sh` and the hourly filings job now hash-deduplicate recent e-disclosure HTML, ingest up to 100 new files per run, extract facts, and reconcile identifiers.
- **Other-source ingestion wired:** `scripts/rag_ingest_sources.sh` continuously processes CBR documents, NKR/NRA rating pages, Nalog financial JSON, and HyBond factlists with source authority levels; use `hybond docs report` for live corpus counts.
- **Source-aware retrieval and index safety added:** citations expose source authority; dense index artifacts are built under a temporary directory and atomically renamed, while SQLite version bookkeeping supports activation/rollback. Cross-store activation still is not transactional.
- **Value telemetry added:** `hybond docs report` shows corpus coverage, fact/chunk counts, and observed query evidence rate. Query logging starts with new CLI queries; explicit human usefulness feedback is still needed.
- **Local interface added:** `dashboard/rag.html` plus `scripts/rag_server.py` provide a localhost-only research page/API on port 8779, with citations and source authority visible.
- **Current-risk router added:** issuer-name scoped questions such as «какие риски у эмитента на данный момент?» now use dated HyBond SQL analytics (health, spreads, events, ratings) rather than expecting a document to contain a literal current-risk sentence.
- **Research filters added:** the local API/UI can restrict retrieval to official/financial sources and a document type (e-disclosure, CBR, Nalog, rating, or HyBond factlist).
- **Query router added:** the local API/UI deterministically separates numeric facts, narrative evidence, mixed questions, current-risk SQL summaries, and out-of-scope requests before retrieval; the CLI still exposes the lower-level fact-or-retrieval path.
- **Citation verification added:** retrieved citations include the exact evidence quote and are checked against the indexed document/page; evaluation now reports citation precision separately from page recall.
- **Evaluation diff safety added:** `hybond docs eval-diff --baseline RUN_A --candidate RUN_B` rejects mismatched split, question count, k, or dataset hashes before comparing quality metrics. CI executes fixture integrity, quote, split-isolation, and offline FTS5 checks; it does not enforce an unverified retrieval threshold.
- **Remaining:** independent human verification and broader financial questions; table/scanned/cross-document/conflict fixtures; a suitable pinned local multilingual model and measured dense-vs-FTS5 comparison; evaluated answer-level abstention; structured facts and verified local answer generation.

## 1. Decision

Build **Slice 5: local document intelligence** as a read-only extension of HyBond.

The first useful product is not a general chatbot. It is a cited research assistant for issuer documents:

- prospectuses and issue decisions;
- RAS/IFRS and issuer reports;
- material-fact notices and e-disclosure documents;
- rating releases and other credit-relevant PDFs.

The system must answer in Russian, cite the document and page, and abstain when evidence is absent. It must not alter existing deterministic screening, pricing, default-probability, portfolio, or dashboard outputs.

## 2. Why this fits the current project

HyBond already has the correct boundaries:

- SQLite is the source of truth and is already growing with issuer, bond, filing, event, financial, rating, and snapshot history.
- The e-disclosure pipeline already discovers filings and stores metadata.
- The factlist assembler is the existing boundary between deterministic data and interpretation.
- Ollama is already used locally for classification.
- Existing tests are offline and extensive.

The RAG layer should therefore **read and enrich existing records**, not create a second bond database or replace the current pipeline.

## 3. Critical design rule: SQL for numbers, RAG for language

Do not ask a language model to calculate or invent:

- coupon rates and schedules;
- maturity, put, call, and amortization dates;
- issue volume;
- debt and financial ratios;
- ratings or rating changes.

Extract these as structured facts with provenance and reconcile them against MOEX/CBR/HyBond tables. Numeric queries use parameterized SQL. RAG is used for narrative evidence:

- risk factors and covenant wording;
- management commentary;
- explanations of a rating action;
- changes across several documents;
- “what changed and why?” questions.

Every generated claim needs a citation. Failed citation validation means: **“В предоставленных документах не найдено.”**

## 4. Recommended architecture for this repository

```text
existing HyBond DB / e-disclosure metadata
                 |
       document registry + hash dedup
                 |
   parse -> OCR fallback -> pages/elements/tables
                 |
      chunks + structured facts + provenance
          |                         |
 SQLite facts/metadata       local search index
          |                         |
 numeric SQL path       hybrid text retrieval
                                  |
                         optional visual retrieval
                                  |
                local answerer + citation verifier
```

### Storage choice

**Phase 1:** keep SQLite for registry, metadata, facts, FTS5, evaluation results, and document-to-issuer relationships. Store PDFs/page images outside SQLite under `data/rag/` using content-addressed paths.

**Phase 2:** a local NumPy dense index is now available as an opt-in retrieval provider; use the existing SearchHit/RRF seam. Do not make dense-model dependencies, Qdrant, PyTorch, OCR, or visual models mandatory for the normal HyBond install.

This controls database growth: the SQLite database keeps searchable metadata and facts, while large binary files and replaceable indexes remain outside the primary database.

## 5. Scope of the first corpus

Start with a deliberately small, high-value corpus:

- 10 issuers from the active watchlist;
- 30–50 documents;
- at least one digital PDF, one scanned PDF, and one table-heavy PDF;
- documents that cover prospectus/issue terms, financial reporting, material facts, and ratings.

Do not index every historical filing immediately. First prove retrieval and citation quality on documents where the answers are known.

## 6. Proposed package layout

```text
src/hybond/rag/
  __init__.py
  config.py
  types.py
  corpus/
    manifest.py
    registry.py
  ingest/
    parse.py
    ocr.py
    tables.py
    render.py
    chunk.py
  index/
    store.py
    text_dense.py
    text_sparse.py
    versioning.py
    visual.py              # later, optional
  retrieve/
    hybrid.py
    rerank.py              # later, optional
    fuse.py
  extract/
    schema.py
    extractor.py
    reconcile.py
  answer/
    router.py
    sql_path.py
    generate.py
    verify.py
  eval/
    golden.py
    metrics.py
    runner.py
    report.py
  cli.py

tests/rag/
  fixtures/
  test_*.py

eval/
  golden/ru_bonds_v1.jsonl
  corpus/manifest_v1.jsonl
```

The package should be importable without heavy RAG dependencies. Optional model tests must be skipped unless explicitly enabled.

## 7. SQLite additions

Add migrations through `init_db()` using the existing retrofit pattern. Proposed tables:

- `rag_documents`: SHA-256 primary key, issuer/ISIN, document type, URL, local path, fetched time, page count, parse status;
- `rag_pages`: document hash, page number, scan/OCR status, confidence, image path;
- `rag_elements`: text/table/figure, page, bounding box, extracted text, table HTML;
- `rag_chunks`: stable chunk ID, document hash, page range, section, text, chunk hash;
- `rag_facts`: fact type, JSON value, source document/page/bbox/quote, confidence, reconciliation status;
- `rag_index_versions`: configuration hash, creation time, active version;
- `rag_eval_runs` and `rag_eval_results`: reproducible evaluation records.

Recommended indexes:

- `(issuer_id, doc_type, fetched_at)` on documents;
- `(doc_sha256, page_no)` on pages/elements;
- `(isin, fact_type, reconcile_status)` on facts;
- FTS5 over chunk text, with the chunk ID as the external key.

Do not store page PNG bytes, PDF blobs, or embedding vectors in the main SQLite file.

## 8. Ingestion and incremental indexing

1. Discover existing document URLs and bodies from e-disclosure records.
2. Download or accept local PDFs only during ingestion; never make network calls at query time.
3. Hash the original bytes with SHA-256.
4. Skip unchanged documents by hash.
5. Parse layout while retaining page numbers, element types, tables, and bounding boxes.
6. OCR only pages without a usable text layer; record OCR confidence.
7. Store tables both as HTML/structured rows and as separately searchable table chunks.
8. Render page images only for pages needed by visual retrieval or citation preview.
9. Build a new index version, validate it, then atomically activate it.
10. Preserve old versions for rollback.

The existing filing metadata should be linked to `rag_documents` rather than duplicated as an unrelated corpus.

## 9. Retrieval plan

### P0/P1 baseline

- SQLite FTS5/BM25 or a small standard-library-compatible lexical search;
- structure-aware chunks with issuer, document type, section, and page metadata;
- local Ollama generation only after retrieval;
- page-level citation output.

### P2 improvement

- multilingual dense embeddings (candidate: BGE-M3, verified at implementation time);
- reciprocal-rank fusion of sparse and dense results;
- optional cross-encoder reranking;
- evaluation comparison against the baseline.

### P3 visual path

Only add ColPali/ColQwen-style visual retrieval if the golden set demonstrates failures on scans, tables, or charts that text retrieval cannot solve. Keep it optional because page-image indexes can be large and the current machine already has a growing production workload.

## 10. Answer routing

Classify each query into:

- `numeric_fact` — SQL over `rag_facts` and existing HyBond tables;
- `narrative` — hybrid retrieval plus local generation;
- `mixed` — SQL facts plus cited narrative context;
- `out_of_scope` — abstain.

The answer object should include:

- answer text;
- answer type;
- citations: document hash/title, page, quote, and bbox when available;
- retrieved evidence IDs;
- confidence/verification status;
- explicit abstention reason when applicable.

The generator must never receive authority to silently overwrite a structured value or reconcile a mismatch.

## 11. Golden set and evaluation

Before tuning models, create `60–100` manually verified Russian questions over the first corpus:

- at least 15% unanswerable;
- at least 20% table questions;
- at least 10% scanned pages;
- at least 10% cross-document questions;
- at least 10 issuers.

Each JSONL item records question, issuer/ISIN, category, expected answer, evidence pages, difficulty, and verifier/date.

Measure:

- page recall@k and MRR;
- citation precision;
- numeric exact match with tolerance;
- date/boolean/list exact match;
- abstention accuracy;
- answer correctness for narrative questions, with manual review as the primary signal.

Freeze the test split. Tune only on dev. Every later retrieval or model change must produce an eval diff.

## 12. Delivery phases

| Phase | Deliverable | Acceptance gate |
|---|---|---|
| P0 | schemas, migrations, manifest validator, fixtures, golden-set format | fixtures validate; no existing tests regress |
| P1 | ingestion, page/elements model, chunker, FTS5 baseline, `docs eval` | baseline records retrieval/citation metrics and no-answer retrieval diagnostics; answer-level abstention requires an answerer |
| P2 | hybrid sparse+dense retrieval and optional reranker | dev page recall@5 improves over P1 without citation regression |
| P3 | visual page retrieval | scan/table/figure subset improves; no broad regression |
| P4 | fact extraction, reconciliation, SQL router | >=95% numeric exact match on answerable dev questions |
| P5 | versioning, rollback, tracing, CI fixture evaluation, FastAPI/CLI hardening | bad index rollback works; fixture regression fails CI |

## 13. CLI proposal

```text
hybond docs ingest --path DIR
hybond docs ingest --from-edisclosure ISIN
hybond docs index [--rebuild] [--visual]
hybond docs extract [--isin ISIN]
hybond docs ask "вопрос" [--isin ISIN] [--show-sources]
hybond docs eval --golden eval/golden/ru_bonds_v1.jsonl --manifest eval/corpus/manifest_v1.jsonl --out data/rag_eval.json
hybond docs eval-diff --baseline data/rag_eval_base.json --candidate data/rag_eval_candidate.json
```

The first implementation should support `--path` and fixture documents before integrating more downloader behavior into the production scrape pipeline.

## 14. Risks and controls

| Risk | Control |
|---|---|
| hallucinated financial number | SQL path, structured facts, reconciliation, citation verifier |
| scanned Russian PDFs | OCR fallback with confidence and scan-tagged evaluation questions |
| growing storage | content hashes, external binary storage, retention/index policy, no blobs in SQLite |
| slow or fragile model dependencies | optional extras; lexical baseline remains usable |
| stale or contradictory documents | fetched dates, document versions, source priority, mismatch status |
| retrieval looks good but answers are wrong | frozen golden set and per-phase eval diff |
| leaking documents to cloud | local-only query path, tests mock models/network |
| production cron contention | indexing is separate, bounded, lock-aware, and never runs inside price/portfolio computation |

## 15. Open decisions before implementation

1. Which 30–50 documents and 10 issuers form the first corpus?
2. Which local vision-language model is available within current VRAM/RAM limits?
3. Is the first release text/table-only, or is there an immediate business need for page-image retrieval?
4. Where should retained PDFs live, and what retention policy is acceptable?
5. Which existing filing bodies are complete enough to reuse, versus requiring PDF ingestion?
6. Which numeric facts are authoritative in current HyBond tables and need reconciliation adapters?
7. Should the first query interface be CLI-only, or should the dashboard later expose a cited research panel?

## 16. Next implementation focus

The source note recommends changing one capability at a time and measuring it against a trustworthy benchmark. Given that the current v2 candidate is not human-verified and no suitable Russian embedding/OCR model is installed, the next implementation focus is document fidelity rather than tuning retrieval or enabling generation.

**Implemented first:** HTML disclosures now keep table text out of prose chunks, index each table separately, and persist structured rows/cells (including `rowspan`/`colspan`) plus canonical table HTML in `rag_elements`. A conservative `pdftotext -layout` path separates aligned numeric rows from prose; official CBR PDF fixtures verify row/page provenance and footnote preservation. The optional local Tesseract hook stores OCR text and confidence only when explicitly enabled; TSV line breaks are preserved but OCR is not retrieved. Clean and deliberately degraded raster surrogates exercise scan detection and local OCR smoke metrics only; neither is a genuine scan. A smoke run over 30 HTML fixtures found 120 tables / 540 cells, including non-financial site tables. `docs ingest-dir --reprocess` enables bounded re-parsing of known hashes after parser changes; it was not run against production data.

**Next:** obtain a genuine scanned issuer PDF and independent question review; expand OCR ground truth across table-heavy pages and scanner defects. Preserve word boxes/line confidence and reconstruct table rows before building a separate, explicitly OCR-labeled retrieval channel with page citations. Evaluate retrieval and numeric-cell errors on a verified dev set before considering any opt-in indexing; keep OCR out of authoritative facts and answer evidence until provenance and quality gates are validated. The synthetic CBR pages and PSM diagnostics are engineering smoke tests, not scan-quality evidence. Do not install system packages, claim production quality, change dashboard behavior, or broadly re-index without approval and measured evidence.
