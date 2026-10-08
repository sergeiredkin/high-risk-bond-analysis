#!/bin/bash
# Incremental RAG ingestion for non-e-disclosure sources.
# Structured MOEX/DB data stays in SQL; this covers narrative/reference files.
set -euo pipefail
cd "/home/sergei/Documents/high risk bond analysis"
export PYTHONPATH=src

python3 -m hybond docs ingest-dir --root data/raw/cbr \
  --pattern '**/*' --doc-type cbr_document \
  --source-kind cbr --authority-level official \
  --limit "${RAG_CBR_LIMIT:-25}"
python3 -m hybond docs ingest-dir --root data/raw/nkr \
  --pattern '**/*.html' --doc-type rating_agency_nkr \
  --source-kind rating_agency --authority-level rating_source \
  --limit "${RAG_RATING_LIMIT:-10}"
python3 -m hybond docs ingest-dir --root data/raw/nra \
  --pattern '**/*.html' --doc-type rating_agency_nra \
  --source-kind rating_agency --authority-level rating_source \
  --limit "${RAG_RATING_LIMIT:-10}"
python3 -m hybond docs ingest-dir --root data/raw/nalog \
  --pattern '**/*.json' --doc-type nalog_financials \
  --source-kind nalog --authority-level financial_source \
  --limit "${RAG_NALOG_LIMIT:-50}"
python3 -m hybond docs ingest-dir --root data/factlists \
  --pattern '**/*.json' --doc-type hybond_factlist \
  --source-kind hybond_factlist --authority-level derived_analysis \
  --limit "${RAG_FACTLIST_LIMIT:-100}"
