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
PYTHONPATH=src python -m hybond scrape --smartlab >> "$LOG" 2>&1
PYTHONPATH=src python -m hybond market-notes >> "$LOG" 2>&1
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
PYTHONPATH=src python -m hybond auto-classify --limit 30 >> "$LOG" 2>&1
echo "=== done $(date) ===" >> "$LOG"
