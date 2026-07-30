#!/bin/bash
# hybond weekly financials run (Slice 4) — Sunday 07:00 Moscow time
# Requires PARSER_API_KEY to be set in the environment (cron does not inherit
# your login shell's env vars). 2026-07-30: this job had NEVER actually fired
# successfully -- neither this script nor the crontab entry ever set the key,
# so every Sunday invocation (if it ran at all) would have failed before
# writing a single log line. Fixed by setting PARSER_API_KEY inline on the
# crontab entry itself (`crontab -l`), NOT here -- this file is git-tracked,
# the crontab is not. Do not hardcode the key in this script.

export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base

set -e
cd "/home/sergei/Documents/high risk bond analysis"
mkdir -p data/logs
LOG="data/logs/financials_$(date +%Y%m%d).log"

if [ -z "$PARSER_API_KEY" ]; then
    echo "=== $(date): ABORT -- PARSER_API_KEY not set in environment ===" >> "$LOG"
    exit 1
fi

PYTHONPATH=src python -m hybond financials watchlist.json >> "$LOG" 2>&1
