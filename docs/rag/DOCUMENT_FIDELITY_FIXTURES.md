# Document-fidelity fixtures (candidate, not benchmark)

These local fixtures exercise PDF table extraction and scanned-page detection. They are not human-reviewed golden questions and are not part of the issuer retrieval benchmark.

## Source and derivation

- Digital, one-page CBR forecast: [`forecast_260424.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424.pdf), official source URL: <https://www.cbr.ru/Content/Document/File/189801/forecast_260424.pdf>.
- [`forecast_260424_page1_rasterized.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424_page1_rasterized.pdf) is an image-only test surrogate rasterized from page 1 of that digital source. It is **not an original scanned document** and must not be counted as representative OCR evidence.
- SHA-256 and local paths are recorded in `eval/corpus/manifest_doc_fidelity_v1.jsonl`; the derivative row records its parent digest.

## What is tested

- The digital source produces page-cited structured table rows and separate table chunks; the fixture currently yields two aligned tables.
- The rasterized surrogate is one page, has no text layer, and is marked scanned without inventing text.
- The PDF extractor removes `pdftotext`'s trailing page-separator form-feed so it does not report a phantom extra page.

## Limits and financial-safety warning

PDF table reconstruction currently uses `pdftotext -layout` spacing heuristics. It retains source strings but does not provide bounding boxes, infer all merged cells, or establish units/column semantics. In particular, a superscript footnote can be fused into a value: this source renders the 2026 key-rate interval as `14,0–14,51` although the final `1` is a footnote marker. Do not use this heuristic as an authoritative numeric answer path; SQL/source-specific parsers and reconciliation remain required.

An optional, local Tesseract extraction hook is available behind `HYBOND_RAG_OCR=1` (language defaults to `rus+eng`). It requires `pdftoppm`, `tesseract`, and installed language data. OCR text and mean word confidence are stored in `rag_elements` / `rag_pages`, but OCR text is deliberately **not** FTS-indexed or returned as answer evidence yet; citation/UI confidence propagation and quality gates are still missing. Tesseract and Russian language data are not installed here, so only mocked contract tests run. The rasterized surrogate tests scan detection and safe no-text behavior, not OCR quality. A genuine scanned source and real local OCR evaluation are required before enabling OCR retrieval.
