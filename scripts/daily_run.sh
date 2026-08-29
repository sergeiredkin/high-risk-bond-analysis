#!/bin/bash
# hybond daily pipeline run
# Cron: 08:00, 13:00, 19:00 Moscow time (05:00, 10:00, 16:00 UTC)
# Run from project root with VPN off — e-disclosure.azipi.ru requires local IP

export PATH="/home/sergei/miniconda3/bin:$PATH"
source /home/sergei/miniconda3/etc/profile.d/conda.sh
conda activate base

set -e
cd "/home/sergei/Documents/high risk bond analysis"
LOG="data/logs/hybond_$(date +%Y%m%d_%H%M%S).log"
mkdir -p data/logs

echo "=== hybond run $(date) ===" >> "$LOG"

# Ensure the rating table exists before any pipeline step reads it
# (Default Probability Layer -- see DEFAULT_PROB_FEATURE.md)
PYTHONPATH=src python -c "
from hybond.store import db
con = db.connect()
db.init_db(con)
db.init_rating(con)
con.close()
" >> "$LOG" 2>&1

# Slice 1: prices + spreads
PYTHONPATH=src python -m hybond run >> "$LOG" 2>&1

# Portfolio monitoring (Phase B, 2026-08-27): every active portfolio's
# bonds checked against their save-time baseline right after spread_snapshot
# is refreshed above -- G-spread deltas/AEY here would otherwise compare
# against yesterday's prices. Pure DB read+write, no network of its own, so
# it stays under `set -e` like the other local steps (diff, dashboard
# rebuild) rather than being marked non-fatal like the network steps below.
echo "=== Portfolio monitoring ===" >> "$LOG"
PYTHONPATH=src python -m hybond portfolio-monitor >> "$LOG" 2>&1

# Rating backfill (NKR history) -- only agency supported today. Read-heavy
# per-issuer scrape over tier=1/2 issuers, no dependency on today's universe
# snapshot, so it can run anywhere before `hybond universe`; placed here so
# any newly-backfilled rating is available to today's universe/promote/
# health-scan steps below rather than only from tomorrow's run.
echo "=== Rating backfill ===" >> "$LOG"
PYTHONPATH=src python -m hybond backfill-ratings --agency nkr >> "$LOG" 2>&1
echo "Rating backfill exit: $?" >> "$LOG"

# Universe snapshot: full MOEX corporate-bond screen + G-spread + mispricing.
# The SECID->ИНН emitent map is cached (emitent_map table) and only
# re-paginated weekly -- force that refresh on Sundays (same cron slot as
# financials), same day-of-week check pattern used there. Daily runs read
# the cache and take ~10s instead of ~55s (see universe_pipeline.py docstring).
UNIVERSE_FLAGS=""
if [ "$(date +%u)" = "7" ]; then
    UNIVERSE_FLAGS="--refresh-emitent-map"
fi
PYTHONPATH=src python -m hybond universe $UNIVERSE_FLAGS >> "$LOG" 2>&1

# Watchlist health scan (2026-08-24): hard-demotes/soft-flags tier=1 issuers
# off today's just-refreshed universe_snapshot/spread_snapshot. Placed here
# (after `hybond universe`, not immediately after `hybond run` above) --
# its YTM checks read universe_snapshot, which `hybond run` doesn't write at
# all (only universe_pipeline.py's `hybond universe` does); running it right
# after `hybond run` would check YTM against yesterday's snapshot.
PYTHONPATH=src python -m hybond scan-watchlist-health >> "$LOG" 2>&1

# Universe health scan (2026-08-29): assigns health_status to every
# vdo_scope=1 bond, not just the 34 tier=1 issuers scan-watchlist-health
# covers. Placed after scan-watchlist-health, not immediately after
# `hybond universe` -- tier=1 bonds' health_source='full' is copied from
# issuer.health_status, which the step just above this one just computed;
# running it before scan-watchlist-health would copy yesterday's value.
echo "=== Universe health scan ===" >> "$LOG"
PYTHONPATH=src python -m hybond scan-universe-health >> "$LOG" 2>&1
echo "Universe health exit: $?" >> "$LOG"

# Phase 2 promotion screen: rank vdo_scope over rating floor -> liquidity
# floor -> AEY-risk-adj -> issuer concentration cap, into a deep-monitoring
# candidate shortlist. Pure read over today's just-refreshed universe_snapshot
# (no network of its own) -- cheap to run every cycle right after `universe`.
# Thresholds are still a starting point (see promote.py's module docstring);
# this only refreshes the report, it does not touch watchlist.json.
PYTHONPATH=src python -m hybond promote >> "$LOG" 2>&1

# Slice 3: event feed (no --selenium, uses AZIPI plain HTTP). --smartlab
# supplements AZIPI with smart-lab.ru's bonds feed for Interfax-only issuers
# AZIPI has no coverage for (see scrape/smartlab.py's INTERFAX_ISSUERS map) --
# was built earlier but never actually wired into this cron pipeline until now.
# `|| echo ... non-fatal` because this step is a live network fetch
# (e-disclosure.azipi.ru / smart-lab.ru) -- under `set -e` above, a timeout
# here would previously abort the whole run before diff/dashboard-rebuild
# ever executed, even though those later steps don't depend on this one
# succeeding.
PYTHONPATH=src python -m hybond scrape --smartlab >> "$LOG" 2>&1 \
    || echo "scrape step failed (non-fatal)" >> "$LOG"

# market_note: smart-lab.ru's general news feed (RSS) -- distinct secondary
# source from the bonds-forum --smartlab above (scrape/smartlab_news.py).
# severity/category stay NULL until the classifier is extended for this
# source; this step only ingests + dedupes. Same non-fatal-on-timeout
# reasoning as the scrape step above -- this hits three live RSS feeds
# (smart-lab, MOEX listing, CBR press).
PYTHONPATH=src python -m hybond market-notes >> "$LOG" 2>&1 \
    || echo "market-notes step failed (non-fatal)" >> "$LOG"

# CBR: check if today has a scheduled document release -- fast no-op unless
# today is a meeting date, a резюме publication date, or +14 days past an
# anchor meeting (comment_text). The dedicated 14:00 MSK meeting-day slot
# (see `hybond cbr --install-cron`) covers same-day press-release/speech
# capture right after the 13:30 publication; this daily-run call is the
# backstop that also catches résюме/comment on days that cron doesn't fire.
PYTHONPATH=src python3 -c "
from hybond.cbr_tracker import check_and_fetch_today
check_and_fetch_today()
" >> "$LOG" 2>&1

# Slice 2: change detection + factlists
PYTHONPATH=src python -m hybond diff --factlist-dir data/factlists >> "$LOG" 2>&1

# Dashboard rebuild -- was MISSING from this script entirely (not a wrong
# script name; there was no call here at all), which is why the dashboard
# went stale while every data step above kept completing successfully.
# export_dashboard_data.py reads the DB and writes data/dashboard_data.json;
# build_dashboard.py inlines that JSON into dashboard/index.html. Both are
# local/no-network, so they stay under `set -e` (a real bug here should
# fail loudly, unlike the network steps above).
PYTHONPATH=src python scripts/export_dashboard_data.py >> "$LOG" 2>&1
PYTHONPATH=src python scripts/build_dashboard.py >> "$LOG" 2>&1

# Pipeline health log rotation (Task 2, 2026-08-28): once per day, not once
# per cron slot -- this script runs 3x/day (08:00/13:00/19:00 MSK), so gate
# on the first slot. Idempotent either way (rotate_pipeline_health only
# deletes rows older than `days`), this just avoids redundant DB writes on
# the 13:00/19:00 runs.
if [ "$(date +%H)" = "08" ]; then
    PYTHONPATH=src python -c "
from hybond.health import rotate_pipeline_health
from hybond.store import db
con = db.connect()
db.init_pipeline_health(con)
print(rotate_pipeline_health(con, days=7))
con.close()
" >> "$LOG" 2>&1
fi

echo "=== done $(date) ===" >> "$LOG"
