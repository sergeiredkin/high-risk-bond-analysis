# Document-fidelity fixtures (candidate, not benchmark)

These local fixtures exercise PDF table extraction and scanned-page detection. They are not human-reviewed golden questions and are not part of the issuer retrieval benchmark.

## Source and derivation

- Digital, one-page CBR forecast: [`forecast_260424.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424.pdf), official source URL: <https://www.cbr.ru/Content/Document/File/189801/forecast_260424.pdf>.
- Digital, 14-page CBR forecast commentary: [`comment_26022026.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/comment_26022026.pdf), official source URL: <https://www.cbr.ru/Content/Document/File/187047/comment_26022026.pdf>.
- [`forecast_260424_page1_rasterized.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/forecast_260424_page1_rasterized.pdf) is a clean image-only test surrogate rasterized from page 1 of that digital source.
- [`comment_page2_synthetic_scan.pdf`](../../eval/corpus/fixtures_doc_fidelity_v1/cbr/comment_page2_synthetic_scan.pdf) is a degraded, image-only surrogate derived from page 2 of the commentary. Recreate it locally with Poppler and Pillow: `python3 scripts/make_rag_synthetic_scan.py eval/corpus/fixtures_doc_fidelity_v1/cbr/comment_26022026.pdf /tmp/comment-scan.pdf --page 2 --dpi 200`. It adds mild skew, blur, contrast loss, deterministic speckles, and JPEG compression. It is **not an original scanner capture**.
- SHA-256 and local paths are recorded in `eval/corpus/manifest_doc_fidelity_v1.jsonl`; the derivative row records its parent digest.

## What is tested

- The digital forecast produces two page-cited structured tables on page 1; the commentary PDF has 14 pages and its forecast tables are attributed to page 2.
- Both image-only surrogates are one page, have no text layer, and are marked scanned without indexing OCR output when OCR is disabled. The degraded surrogate's checked-in SHA-256 pins the tested pixels/PDF.
- The PDF extractor removes `pdftotext`'s trailing page-separator form-feed so it does not report a phantom extra page.
- `eval/golden/ru_pdf_tables_candidate_v1.jsonl` is a four-question agent-checked smoke set with document-disjoint dev/test sources. It is not human-reviewed or frozen. FTS5 diagnostics: dev recall@5 1.00 / citation precision@5 0.20 / MRR 0.625; test recall@5 0.50 / citation precision@5 0.10 / MRR 0.25. One held-out query misses its target page; answer correctness remains manual.

## Limits and financial-safety warning

PDF table reconstruction currently uses `pdftotext -layout` spacing heuristics. It retains source strings but does not provide bounding boxes, infer all merged cells, or establish units/column semantics. In particular, a superscript footnote can be fused into a value: this source renders the 2026 key-rate interval as `14,0–14,51` although the final `1` is a footnote marker. Do not use this heuristic as an authoritative numeric answer path; SQL/source-specific parsers and reconciliation remain required.

An optional, local Tesseract extraction hook is available behind `HYBOND_RAG_OCR=1` (language defaults to `rus+eng`). It requires `pdftoppm`, `tesseract`, and installed language data. OCR text and mean word confidence are stored in `rag_elements` / `rag_pages`; TSV line breaks are now preserved instead of flattening the whole page. OCR text is deliberately **not** FTS-indexed or returned as answer evidence yet; word boxes, table-row reconstruction, citation/UI confidence propagation, and quality gates are still missing.

A local smoke test used Tesseract 5.3.4 with `rus+eng`, first from a temporary extraction under `/tmp` and then re-run against the system installation; the measured results matched. Page-segmentation-mode (PSM) tuning showed no universal winner: token WER for PSM 3/4/6 was **7.22% / 5.74% / 4.63%** on the clean raster, versus **19.83% / 19.83% / 22.15%** on the degraded raster. The degraded default run had mean word confidence **0.9202** and read `8,7` as `87` and `3,4` as `34`. Simple binarization worsened WER to 29.75%; deskewing by the known synthetic angle recovered one additional checked range but did not improve overall WER. These are one-page engineering diagnostics, not representative scan-quality evidence or numeric-cell accuracy; confidence and WER alone are insufficient quality gates. This host now has Tesseract 5.3.4 and `rus+eng`; other deployments must install these separately. A genuine scanned issuer source and independent human verification are still required before adding scan questions to a benchmark or indexing OCR output.
