#!/bin/bash
# rebuild_dashboard.sh -- classification-aware dashboard rebuild, run every
# 30 min from cron. Purpose: dashboard freshness independent of the hourly
# filings cron's own schedule, without racing it for the GPU/DB.
export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base
set -e
cd "/home/sergei/Documents/high risk bond analysis"

LOCK="data/logs/classify_running.lock"
TS_FILE="data/logs/last_classify.timestamp"
LOG="data/logs/rebuild_dashboard.log"
mkdir -p data/logs

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "=== rebuild_dashboard start ==="

# Step 1: if hourly_filings.sh's own classify run is already in progress,
# wait rather than race it for the GPU/DB -- up to 3 min, polling every 10s.
# A lock still present after that is treated as stale (a crashed prior run)
# rather than blocking the dashboard indefinitely.
if [ -f "$LOCK" ]; then
    log "classify_running.lock present, waiting up to 3 min"
    waited=0
    while [ -f "$LOCK" ] && [ "$waited" -lt 180 ]; do
        sleep 10
        waited=$((waited + 10))
    done
    if [ -f "$LOCK" ]; then
        log "lock still present after ${waited}s wait -- proceeding anyway (stale lock from a crashed run?)"
    else
        log "lock cleared after ${waited}s"
    fi
fi

# Step 2: catch up on classification if it hasn't run recently, so freshly
# scraped filings don't render un-categorized on the dashboard. (The task
# spec for this script literally read "if classify RAN within the last 20
# min, run a burst" -- the inverse of what its own stated purpose requires;
# corrected here to "if classify has NOT run in the last 20 min", which is
# what actually achieves "fresh filings get categorized before appearing in
# the dashboard".) cmd_auto_classify (src/hybond/__main__.py) writes/removes
# $LOCK and writes $TS_FILE itself -- this script does not duplicate that.
run_burst=1
if [ -f "$TS_FILE" ]; then
    last_ts=$(cat "$TS_FILE")
    now_ts=$(date +%s)
    age=$((now_ts - last_ts))
    if [ "$age" -lt 1200 ]; then
        run_burst=0
        log "classify ran ${age}s ago (< 20 min) -- skipping catch-up burst"
    fi
fi

if [ "$run_burst" -eq 1 ]; then
    log "classify stale or never run -- starting catch-up burst (--limit 30 --pause 8)"
    PYTHONPATH=src python -m hybond auto-classify --limit 30 --pause 8 >> "$LOG" 2>&1 \
        || log "classify burst failed (non-fatal, continuing to rebuild)"
    log "classify burst done"
fi

# Step 3: rebuild. Local/no-network, so (unlike the classify burst above)
# stays under `set -e` -- a real bug here should fail loudly.
log "running export_dashboard_data.py"
PYTHONPATH=src python scripts/export_dashboard_data.py >> "$LOG" 2>&1
log "running build_dashboard.py"
PYTHONPATH=src python scripts/build_dashboard.py >> "$LOG" 2>&1

log "=== rebuild_dashboard done ==="
