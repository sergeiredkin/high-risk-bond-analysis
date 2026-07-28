#!/bin/bash
# hybond weekly financials run (Slice 4) — Sunday 07:00 Moscow time
# Requires PARSER_API_KEY to be set in the environment (cron does not inherit
# your login shell's env vars — set it in this script or in crontab's own
# environment, not via $PARSER_API_KEY on the invoking line).

export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base

set -e
cd "/home/sergei/Documents/high risk bond analysis"
mkdir -p data/logs
LOG="data/logs/financials_$(date +%Y%m%d).log"

PYTHONPATH=src python -m hybond financials watchlist.json >> "$LOG" 2>&1
