#!/bin/bash
# hybond startup catch — runs at boot AND at systemd-sleep resume
# (/etc/systemd/system-sleep/hybond-resume.sh, 2026-08-23 suspend/resume
# cron-gap fix — @reboot alone never fires on resume-from-suspend, only on a
# genuine cold boot), checks if today's data is actually stale rather than
# just "was a run attempted today".
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

# Staleness check via the DB's own snap_date, not a log filename's mere
# existence (2026-08-23, suspend/resume cron-gap follow-up): a run can start
# -- creating today's log -- and still fail partway through without ever
# writing a fresh universe_snapshot row, which the old log-date check could
# not tell apart from a genuinely fresh run. This also naturally covers the
# "no previous logs at all" first-run case (LAST_SNAP comes back empty/null,
# which never equals TODAY_ISO), so that special case no longer needs its
# own branch.
LAST_SNAP=$(sqlite3 data/db/hybond.sqlite "SELECT MAX(snap_date) FROM universe_snapshot;" 2>/dev/null)
TODAY_ISO=$(date +%Y-%m-%d)

if [ "$LAST_SNAP" != "$TODAY_ISO" ]; then
    echo "$(date) — snap_date stale ($LAST_SNAP vs $TODAY_ISO), running catch-up pipeline"
    bash scripts/daily_run.sh >> data/logs/startup_catch.log 2>&1
else
    echo "$(date) — snap_date is current ($LAST_SNAP) — skipping full run"
    echo "Running filings catch-up only"
    bash scripts/hourly_filings.sh
fi
