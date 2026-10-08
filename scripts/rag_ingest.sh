#!/bin/bash
# Incremental local RAG ingestion. New source files are hash-deduplicated in SQLite.
set -euo pipefail
cd "/home/sergei/Documents/high risk bond analysis"
export PYTHONPATH=src
python3 -m hybond docs ingest-dir \
  --root data/raw/edisclosure \
  --pattern '**/msg_*.html' \
  --doc-type edisclosure_html \
  --source-kind edisclosure --authority-level issuer_disclosure \
  --limit "${RAG_INGEST_LIMIT:-100}" \
  --since-days "${RAG_INGEST_SINCE_DAYS:-7}"
