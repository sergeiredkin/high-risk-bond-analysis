#!/bin/bash
# One-off sampling job for the Cbonds feasibility check (Task 4 follow-up).
# NOT the production pipeline -- see scripts/cbonds_sample.py's docstring.
# Self-terminates via END_DATE inside the python script; safe to leave the
# crontab entry in place through the window, but delete it after END_DATE.

export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base

cd "/home/sergei/Documents/high risk bond analysis"
mkdir -p data/cbonds_sample
LOG="data/cbonds_sample/cron_$(date +%Y%m%d_%H%M%S).log"

echo "=== cbonds_sample run $(date) ===" >> "$LOG"
PYTHONPATH=src python scripts/cbonds_sample.py >> "$LOG" 2>&1
