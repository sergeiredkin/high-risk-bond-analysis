# Document-fidelity fixtures (candidate, not benchmark)

These local fixtures exercise PDF table extraction and scanned-page detection. They are not human-reviewed golden questions and are not part of the issuer retrieval benchmark.

## Source and derivation

- Digital, one-page CBR forecast: [`forecast_260424.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424.pdf), official source URL: <https://www.cbr.ru/Content/Document/File/189801/forecast_260424.pdf>.
- Digital, 14-page CBR forecast commentary: [`comment_26022026.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/comment_26022026.pdf), official source URL: <https://www.cbr.ru/Content/Document/File/187047/comment_26022026.pdf>.
- [`forecast_260424_page1_rasterized.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424_page1_rasterized.pdf) is an image-only test surrogate rasterized from page 1 of that digital source. It is **not an original scanned document** and must not be counted as representative OCR evidence.
- SHA-256 and local paths are recorded in `eval/corpus/manifest_doc_fidelity_v1.jsonl`; the derivative row records its parent digest.

## What is tested

- The digital forecast produces two page-cited structured tables on page 1; the commentary PDF has 14 pages and its forecast tables are attributed to page 2.
- The rasterized surrogate is one page, has no text layer, and is marked scanned without inventing text.
- The PDF extractor removes `pdftotext`'s trailing page-separator form-feed so it does not report a phantom extra page.
- `eval/golden/ru_pdf_tables_candidate_v1.jsonl` is a four-question agent-checked smoke set with document-disjoint dev/test sources. It is not human-reviewed or frozen. FTS5 diagnostics: dev recall@5 1.00 / citation precision@5 0.20 / MRR 0.625; test recall@5 0.50 / citation precision@5 0.10 / MRR 0.25. One held-out query misses its target page; answer correctness remains manual.

## Limits and financial-safety warning

PDF table reconstruction currently uses `pdftotext -layout` spacing heuristics. It retains source strings but does not provide bounding boxes, infer all merged cells, or establish units/column semantics. In particular, a superscript footnote can be fused into a value: this source renders the 2026 key-rate interval as `14,0–14,51` although the final `1` is a footnote marker. Do not use this heuristic as an authoritative numeric answer path; SQL/source-specific parsers and reconciliation remain required.

An optional, local Tesseract extraction hook is available behind `HYBOND_RAG_OCR=1` (language defaults to `rus+eng`). It requires `pdftoppm`, `tesseract`, and installed language data. OCR text and mean word confidence are stored in `rag_elements` / `rag_pages`, but OCR text is deliberately **not** FTS-indexed or returned as answer evidence yet; citation/UI confidence propagation and quality gates are still missing.

A local smoke test used Tesseract 5.3.4 with `rus+eng`, extracted temporarily under `/tmp` without system-wide installation. On the rasterized surrogate, the hook returned mean word confidence **0.9366**; after NFKC/case/dash normalization, token-level WER against `pdftotext` text was **4.63%** (25 edit operations / 540 reference tokens). This is **not** representative scan-quality evidence: the page is a clean 120-DPI raster of a digital PDF, and OCR word confidence/overall WER do not establish numeric cell correctness. Tesseract is not installed system-wide. A genuine scanned issuer source and independent human verification are still required before adding scan questions to a benchmark or indexing OCR output.
