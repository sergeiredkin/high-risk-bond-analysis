#!/bin/bash
# hybond hourly filing scrape — AZIPI + market notes (smart-lab.ru, MOEX
# Listing RSS, CBR Press RSS). `market-notes` below already pulls all three
# feeds as of the 2026-08-17 MOEX/CBR RSS task -- run_market_notes() was
# extended, not this script; no new invocation needed here.
# Lightweight: static RSS pulls only (MOEX listing feed ~2.1MB, ~15s fetch),
# no MOEX ISS API calls, no universe refresh.
export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base
set -e
cd "/home/sergei/Documents/high risk bond analysis"
LOG="data/logs/hybond_filings_$(date +%Y%m%d_%H%M%S).log"
mkdir -p data/logs
echo "=== filings run $(date) ===" >> "$LOG"
# scrape holds the shared data/logs/pipeline_running.lock (pipeline_lock.py)
# for its duration -- if this run overlaps into rebuild_dashboard.sh's :00/
# :30 slot, that script's own classify burst waits up to 60s for this lock
# to clear rather than starting a concurrent write blind (the exact
# collision confirmed live on 2026-08-21: scrape still running past :15 into
# a :30 classify burst caused a real "database is locked" failure).
#
# market-notes (smartlab/moex/cbr feeds) had NO lock guard at all until
# 2026-08-27, when the inverse collision hit live: a slow :00 classify
# burst was still running well past :15 (classify bursts can take up to
# ~30min, see auto-classify's own comment below), and market-notes' writes
# collided with it directly ("database is locked" on all three feeds).
# cmd_market_notes now wraps run_market_notes() in the same shared
# pipeline_lock.held() (10min wait, then proceeds regardless -- see
# pipeline_lock.py's module docstring for why this is wait-and-retry, not
# a hard mutex/skip). The edisclosure scrape step above is read-heavy and
# was never part of this collision, so it is intentionally left unguarded.
# --no-bodies: body fetch hangs on some issuer URLs, exhausting the 300s
# runtime budget before any issuers are processed. Title+URL is sufficient
# for dedup and classification. Bodies can be fetched on-demand in the UI.
PYTHONPATH=src python -m hybond scrape --smartlab --no-bodies >> "$LOG" 2>&1
# Incremental local RAG ingest: only new/recent e-disclosure bodies, hash-deduped
# in rag_documents. The seven-day window catches delayed downloads without
# repeatedly scanning the entire historical corpus.
PYTHONPATH=src python -m hybond docs ingest-dir --root data/raw/edisclosure \
  --pattern '**/msg_*.html' --doc-type edisclosure_html \
  --source-kind edisclosure --authority-level issuer_disclosure --limit 100 \
  --since-days 7 >> "$LOG" 2>&1
# Other local narrative sources: CBR, rating agencies, Nalog financials,
# and HyBond factlists. Each source is hash-deduplicated and bounded.
bash scripts/rag_ingest_sources.sh >> "$LOG" 2>&1
PYTHONPATH=src python -m hybond market-notes >> "$LOG" 2>&1
# Google News RSS per tier=1 issuer (2026-08-24): one HTTP request per
# issuer with a 2s sleep between them (see scrape/google_news.py), so this
# adds roughly (tier=1 issuer count x 2s) to the hourly run -- noticeably
# heavier than the two static feed pulls above, but still well inside the
# hourly window. cmd_google_news never raises on a single issuer's fetch
# failure (per-issuer fault isolation, same as market-notes' per-feed
# isolation), so `set -e` above won't abort this script over one bad fetch.
PYTHONPATH=src python -m hybond google-news >> "$LOG" 2>&1
# Auto-classify via local Ollama (classify_ollama.py): --limit 20 caps each
# hourly run so the backlog clears gradually without hogging the GPU during
# other work. classify_filing() catches its own network/parse errors and
# returns None per-filing, so a down/slow Ollama daemon degrades to
# "Failed: N" in the log rather than aborting this script (set -e above).
# Each real classification call measured at 52-77s on this machine (qwen3.5,
# RTX 3060) -- --limit is a COMBINED budget across filings+market_notes (see
# cmd_auto_classify), so --limit 30 can take up to ~40 min, inside the hourly
# window but overlapping into the next run's start is possible if the queue
# is deep. cmd_auto_classify itself also holds the shared pipeline_running.lock
# for the duration and writes data/logs/last_classify.timestamp on any
# successful (non-dry-run) completion -- rebuild_dashboard.sh reads the
# timestamp to know when a catch-up burst is needed, and its own auto-classify
# call waits on the same shared lock this scrape step just held.
# 2026-09-23: live trial of the pure-stdlib CPU classifier (--engine rules,
# models/cpu_classifier.json) — no GPU, instant per item. Revert by deleting
# the flag (default engine is ollama).
PYTHONPATH=src python -m hybond auto-classify --engine rules --limit 30 >> "$LOG" 2>&1
echo "=== done $(date) ===" >> "$LOG"
