#!/bin/bash
# hybond startup catch — runs at reboot, checks if today's scheduled
# pipeline runs were missed while machine was off
export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base
cd "/home/sergei/Documents/high risk bond analysis"

# Wait for network readiness (max 5 min) instead of a fixed sleep -- a fixed
# `sleep 120` in the crontab @reboot line was too short for DNS/network to
# actually be up (confirmed live: a real run at 08:41 on 2026-08-20 failed
# with "Failed to resolve 'iss.moex.com'" right after the machine rebooted
# with the old fixed delay). Polls the real dependency (MOEX ISS) directly
# every 10s -- proceeds the moment it's actually reachable instead of always
# waiting the full fixed delay, and gives up cleanly rather than running the
# pipeline against an unreachable API if network never comes up.
NETWORK_READY=0
for i in $(seq 1 30); do
    if curl -sf --max-time 3 https://iss.moex.com > /dev/null 2>&1; then
        echo "Network ready after $((i * 10))s"
        NETWORK_READY=1
        break
    fi
    echo "Waiting for network... ($((i * 10))s)"
    sleep 10
done
if [ "$NETWORK_READY" -eq 0 ]; then
    echo "Network never came up after 5 min — exiting without running the pipeline"
    exit 1
fi

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
