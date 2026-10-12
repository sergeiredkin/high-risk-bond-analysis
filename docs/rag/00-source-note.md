The most useful thing a local multimodal RAG system can do for you is answer questions over documents you already work with, where the layout matters (tables, scans, charts, logs) and the data can't go to the cloud. That last part is also what makes it sellable. Here's how I'd approach it.

What to build (pick one first)

1. Bond document intelligence (feeds HyBond). This is my recommended starting point. Prospectuses, issuer RAS/IFRS reports and rating releases are mostly PDFs, often scanned, with dense tables. The scope is small, the documents are Russian-language (a real test of OCR and embeddings), and you already know the right answers, so evaluation is honest.

2. Drilling report assistant (feeds the drilling copilot). Norwegian daily drilling reports, well-completion reports and mud logs are public and truly multimodal: text, tables and log images. This one is harder, but it's the strongest portfolio piece against the SLB and Baker Hughes framing you're aiming for.

3. On-prem compliance assistant for КИИ/187-ФЗ clients. This uses ФСТЭК orders, internal policies and audit evidence. Here "local" is a hard requirement rather than a preference, which makes it the most directly monetizable. It's mostly text, though, so it teaches you less about the multimodal part.

All three share the same core: ingestion, index, retrieval, citations and evaluation. Build that core once as a typed Python package and plug in a different corpus for each domain. That shared core is what gives you room to expand later.

Production architecture

Ingestion is most of the battle.

Parse layout with Docling or a similar layout-aware parser.
Use OCR that handles Cyrillic well, and keep page numbers and bounding boxes for every element.
Store tables as tables (HTML or Markdown plus a structured copy), never as flattened text.

Run two retrieval paths in parallel:

Text path: parsed chunks, hybrid BM25 plus dense embeddings (BGE-M3 is a solid multilingual choice for Russian), then a cross-encoder reranker.
Visual path: page-image retrieval with a ColPali or ColQwen-style late-interaction model. It catches charts, stamps and badly parsed pages that the text path misses.
Fuse the results and rerank them together.

Storage:

Qdrant handles multivector, ColPali-style indexes natively. pgvector is enough if you stay text-only.
Plan storage early: page-image indexes are large, so use pooling or quantization.

Generation:

Run a Qwen-VL-class vision-language model locally through vLLM (for throughput) or Ollama (for convenience).
A GPU with about 24 GB of VRAM comfortably runs a 7–8B VL model plus the embedding models.
Require citations down to page and bounding box, so every answer can be checked against the source.

The rule that matters most for finance: don't use RAG for numbers. Extract coupons, covenants, debt levels and maturities into a structured table at ingestion time, and answer numeric questions with SQL. Use RAG for the narrative parts: risk factors, covenant wording, management commentary. Hallucinated numbers are the main way finance RAG fails, and this design removes most of that risk.

Production plumbing that tutorials skip:

Incremental re-indexing with document hashes, and versioned indexes so you can roll back.
Self-hosted tracing, for example Langfuse.
Per-document access control, which КИИ clients will ask about.
A fallback answer of "not found in the documents" instead of a guess.
Learning path

Each step ends in a working deliverable:

Build the golden set first. Write 50–100 real questions over 30–50 bond documents, with expected answers and source pages. Everything after this gets measured against it.
Baseline text RAG. Parse, chunk, embed and generate, then measure recall@k and answer correctness. Expect it to be mediocre on tables.
Add hybrid search and a reranker, then measure again. This step usually gives the biggest jump.
Add the visual retrieval path. Check specifically which questions it fixes: it should help on tables, scans and charts.
Add structured extraction and SQL for numeric questions.
Production hardening: tracing, incremental indexing, a FastAPI service, and evaluation in CI so any regression fails the build.
Port to a second corpus (drilling reports). If the core needs little change, your abstraction is right.

The habit that separates production RAG from demos is changing one thing at a time and re-running the evaluation every time. Model and library choices in this area change every few months. A good golden set and evaluation harness keep their value, and they let you swap components without guessing.

If useful, I can draft the module layout and the golden-set schema for the HyBond version, as a spec your Claude Code instance can implement.

Yes do it

Here's the spec, written so you can paste it straight into the Claude Code instance. It builds on what HyBond already has: SQLite as the source of truth, Ollama for local inference, the e-disclosure scraper for documents, and the rule of honest absence over fabricated answers.

Spec: HyBond Slice 5 — Document Intelligence (local multimodal RAG)

All work must satisfy the Definition of Done in CONTRIBUTING.md. Do not commit; Sergei commits after review.

Goal

Answer questions over issuer documents (prospectuses, issue decisions, RAS/IFRS reports, issuer reports, material-fact notices, rating releases) with page-level citations, fully on-prem. Numeric facts are answered from structured, extracted tables, not by free generation. If the evidence isn't in the corpus, the system says so.

Non-goals (v1)
No personalized investment advice. Outputs stay within the screener/analytics framing.
No cloud APIs, no network calls at query time.
No changes to the deterministic outputs of Slices 1, 2 or 4. Slice 5 only reads from them.
Package layout

Adjust the root path to the repo's existing package convention.

hybond/rag/
  __init__.py
  config.py            # pydantic settings: model names, paths, k values, thresholds
  types.py             # typed dataclasses: Document, Page, Element, Chunk, Hit, Answer, Citation, Fact
  corpus/
    manifest.py        # corpus manifest load/validate (hash, issuer/bond, source URL, fetch date, optional disclosure ID and publication/retrieval provenance)
    registry.py        # SQLite tables: rag_documents, rag_pages, rag_elements; sha256 dedup
  ingest/
    parse.py           # layout-aware parse -> pages + elements (text|table|figure) with bbox
    ocr.py             # Cyrillic OCR fallback for scanned pages; per-page ocr_confidence
    tables.py          # table -> HTML + normalized rows; never flattened to prose
    render.py          # page -> PNG at fixed DPI (for the visual path + citation previews)
    chunk.py           # structure-aware chunking: section headers carried into each chunk; tables = own chunks
  index/
    store.py           # VectorStore protocol + Qdrant local-mode implementation (on-disk, no server)
    text_dense.py      # dense embeddings (default BGE-M3, configurable)
    text_sparse.py     # BM25 (SQLite FTS5 with Russian stemming, or rank_bm25)
    visual.py          # late-interaction page embeddings (ColQwen-family, configurable), pooled/quantized
    versioning.py      # index_version table; build into new version, atomic switch, rollback
  retrieve/
    hybrid.py          # BM25 + dense fusion (RRF)
    rerank.py          # cross-encoder rerank (default bge-reranker-v2-m3, configurable)
    fuse.py            # merges text and visual hits at page level; dedups by (doc, page)
  extract/
    schema.py          # Fact types: coupon_rate, coupon_schedule, maturity, put_offer, call_option,
                       #   amortization, guarantor, covenant, use_of_proceeds, issue_volume
    extractor.py       # LLM-assisted extraction -> Fact with provenance (doc, page, bbox, quote)
    reconcile.py       # deterministic cross-check vs MOEX ISS / existing tables; flags mismatches
  answer/
    router.py          # classify query: numeric_fact | narrative | mixed | out_of_scope
    sql_path.py        # numeric questions -> parameterized SQL over facts + existing tables
    generate.py        # VL model via Ollama; Russian prompts; must cite (doc, page) per claim
    verify.py          # citation validator: cited page exists in retrieved set, quoted span found;
                       #   failure -> abstain ("в документах не найдено")
  eval/
    golden.py          # golden-set load + JSON Schema validation
    metrics.py         # page recall@k, MRR, numeric evidence match, citation precision; answer abstention is scored only with an answerer.
    runner.py          # runs a config against a golden split; writes eval_runs + per-question results
    report.py          # diff two runs: which questions improved or regressed
  cli.py               # registers `hybond docs ...` subcommands
tests/rag/
  fixtures/            # 3–5 small PDFs (1 digital, 1 scanned, 1 table-heavy), tiny golden set
  test_*.py
eval/
  golden/ru_bonds_v1.jsonl
  corpus/manifest_v1.jsonl
Dependencies

Put heavy dependencies (torch, embedding and visual models, parser, OCR, qdrant-client) behind an optional extra, pip install -e .[rag]. The core test suite must keep passing without that extra. Tests that need models are marked @pytest.mark.rag_models and skipped by default. Pin model names and revisions in config.py. Verify current model versions at implementation time; don't hard-code from memory.

New SQLite tables
Table	Purpose
rag_documents	doc_sha256 (PK), isin, issuer_inn, doc_type, source_url, fetched_at, page_count, parse_status
rag_pages	doc_sha256, page_no, is_scanned, ocr_confidence, image_path
rag_elements	id, doc_sha256, page_no, kind (text/table/figure), bbox, text, table_html
rag_facts	id, isin, fact_type, value_json, doc_sha256, page_no, bbox, quote, confidence, reconcile_status (match/mismatch/unverifiable), extracted_at, model
rag_index_versions	version, created_at, config_hash, is_active
rag_eval_runs / rag_eval_results	run_id, config_hash, index_version, golden_version, and per-question metrics
CLI
hybond docs ingest   [--from-edisclosure ISIN|--path DIR]   # parse + register, idempotent by sha256
hybond docs index    [--visual] [--rebuild]                  # builds new index version
hybond docs extract  [--isin ...]                            # structured facts + reconcile
hybond docs ask      "вопрос" [--isin ...] [--show-sources]
hybond docs eval     --golden ru_bonds_v1 --split dev [--config ...]
hybond docs eval-diff RUN_A RUN_B
Hard rules
Every answer carries citations (doc, page, and bbox where available). If the citation check in verify.py fails, the system abstains. It never returns an uncited claim.
Numeric questions go through sql_path. The model only formats a number that was retrieved; it never produces one.
An extracted fact that disagrees with MOEX ISS is stored with mismatch status and surfaced, never silently overwritten.
No network calls in tests. Model calls in unit tests are mocked.
Indexing is incremental by sha256. A rebuild writes a new version and switches over atomically.
Golden set schema (eval/golden/*.jsonl, one question per line)
json
{
  "qid": "GS-0001",
  "split": "dev",
  "isin": "RU000A10XXXX",
  "issuer_inn": "7700000000",
  "question": "Есть ли у выпуска оферта и на какую дату?",
  "category": "put_offer",
  "answer_type": "date",
  "expected": {"value": "2027-03-15", "aliases": []},
  "tolerance": null,
  "evidence": [
    {"doc_sha256": "ab12…", "page": 14, "modality": "table", "bbox": null}
  ],
  "requires": ["table"],
  "difficulty": "medium",
  "verified_by": "sergei",
  "verified_at": "2026-10-10",
  "notes": ""
}

Enums:

category: coupon, coupon_schedule, maturity, put_offer, call_option, amortization, guarantor, covenant, use_of_proceeds, issue_volume, financials_numeric, rating_action, risk_factor, management_commentary, cross_doc, unanswerable
answer_type: numeric, date, text, list, boolean, none (none is used for unanswerable questions; the correct output is an abstention)
requires (multi-select): table, scan, figure, cross_doc, multi_page

Scoring by answer type:

answer_type	Scoring
numeric	Exact match within tolerance (absolute or relative)
date	Exact match
boolean / list	Exact match (lists compared as sets)
text	Manual review, with an optional LLM judge as a secondary signal only
none	Correct only if the system abstains

Composition targets for v1 (60–100 questions): at least 15% unanswerable, at least 20% table, at least 10% scan, at least 10% cross-doc, spread across at least 10 issuers. Freeze the test split; tune only on dev. A JSON Schema validator in eval/golden.py enforces this, and CI fails on an invalid golden file.

Corpus manifest (eval/corpus/*.jsonl): required doc_sha256, isin, issuer_inn, doc_type, source_url, fetched_at, local_path; optional disclosure_id, published_at, retrieved_at, and source_url_basis. The golden set may only reference documents listed in the manifest.

Phases and acceptance criteria
Phase	Deliverable	Done when
P0	Schemas, validator, corpus manifest tooling, fixtures	Golden and manifest validation pass; Sergei has authored the first 30 questions
P1	Ingestion + dense-only text baseline + eval runner	docs eval produces a baseline run with all metrics recorded
P2	BM25 + RRF + reranker	Dev page-level recall@5 beats P1 (the diff report shows which questions changed)
P3	Visual path + page-level fusion	Recall improves specifically on questions tagged scan, table or figure, with no regression elsewhere
P4	Fact extraction + reconcile + router + SQL path	Numeric exact-match on dev is at least 95% on answerable numeric questions; reconcile mismatches are listed
P5	Hardening: versioning/rollback, tracing, eval in CI on the fixture golden set	Index rollback is tested; a regression on the fixture set fails CI

Report metrics per phase on both dev and frozen test, but only make decisions from dev.

Sergei's part, which doesn't go to Claude Code: choose the 30–50 documents and write the golden questions by hand. Include some questions you know the corpus can't answer, since those test whether the system abstains instead of guessing. This is the slowest part, and it's what makes the numbers in every later phase trustworthy.
