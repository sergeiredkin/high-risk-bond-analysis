#!/bin/bash
# hybond startup catch — runs at reboot, checks if today's scheduled
# pipeline runs were missed while machine was off
export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base
cd "/home/sergei/Documents/high risk bond analysis"

# Check last successful daily run from logs
LAST_RUN=$(ls -t data/logs/hybond_[0-9]*.log 2>/dev/null | head -1)

if [ -z "$LAST_RUN" ]; then
    echo "No previous logs found — running full pipeline"
    bash scripts/daily_run.sh
    exit 0
fi

# Extract date of last run
LAST_DATE=$(basename "$LAST_RUN" | grep -o '[0-9]\{8\}')
TODAY=$(date +%Y%m%d)

if [ "$LAST_DATE" != "$TODAY" ]; then
    echo "Last run was $LAST_DATE, today is $TODAY — running missed pipeline"
    bash scripts/daily_run.sh
else
    echo "Pipeline already ran today ($LAST_DATE) — skipping full run"
    echo "Running filings catch-up only"
    bash scripts/hourly_filings.sh
fi
