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

# Agency ratings from MOEX ISS ЦКИ (2026-09-17, replaces the НКР history
# scrape that ran here -- ratings.ru's terms forbid automated collection).
# Free anonymous endpoints keyed by emitent_id/ISIN; see
# src/hybond/ratings_pipeline.py for the incremental workload (~570 calls,
# ~5 min, only on the first run of the day -- the 13:00/19:00 runs find every
# target fresh and make almost no calls). Right before `hybond universe` so
# today's screen/promote/health steps resolve today's ratings. Network step,
# so non-fatal like scrape/market-notes below: a failed or blocked refresh
# keeps yesterday's ratings (flagged stale after 3 days) and logs a
# MOEX_RATINGS_* health code instead of aborting the run.
echo "=== Ratings refresh (MOEX ЦКИ) ===" >> "$LOG"
PYTHONPATH=src python -m hybond refresh-ratings >> "$LOG" 2>&1 \
    || echo "refresh-ratings step failed (non-fatal)" >> "$LOG"

# Key-rate expectations (Ставка и ОФЗ page, 2026-09-17): CBR forecasts,
# ОНЕГДКП scenarios, the macro survey, decision documents, the daily key rate
# and the meeting calendar -- each from the official cbr.ru page it is updated
# on, each stored with its history (the first run loads the whole history by
# itself). Cheap when nothing is new: the survey workbook and forecast PDFs are
# only downloaded when a new one appears. Placed BEFORE `hybond universe`,
# whose AEY scenarios are read from these publications (stress =
# «Проинфляционный», bull = «Дезинфляционный», aey_risky = «Рисковый»).
# Network step, non-fatal; every source fails independently.
echo "=== Rate expectations ===" >> "$LOG"
PYTHONPATH=src python -m hybond expectations >> "$LOG" 2>&1 \
    || echo "expectations step failed (non-fatal)" >> "$LOG"

# CBR catch-up (2026-09-17): check_and_fetch_today() further down only acts
# on the exact calendar day, so a day the machine was off loses that meeting
# for good -- the 11.09.2026 decision was lost exactly that way (no runs 5-13
# Sep). This re-fetches any recent past meeting whose decision, résumé or
# comment is still missing, from the schedule the step above just refreshed.
# Before `universe` for the same reason: the current key rate feeds the AEY
# deltas. Network step, non-fatal.
echo "=== CBR catch-up ===" >> "$LOG"
PYTHONPATH=src python -m hybond cbr --catch-up >> "$LOG" 2>&1 \
    || echo "cbr catch-up failed (non-fatal)" >> "$LOG"

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

# OFZ snapshot (Ставка и ОФЗ page, 2026-09-17): `hybond ofz` was never in any
# cron script, so every OFZ number in both dashboards stayed frozen at the one
# manual run of 2026-08-05. Placed right after `universe`, whose run also
# stores today's CBR zero-coupon curve. ~60 MOEX bondization calls, so it is a
# network step: non-fatal under `set -e`, like scrape/market-notes below.
echo "=== OFZ snapshot ===" >> "$LOG"
PYTHONPATH=src python -m hybond ofz >> "$LOG" 2>&1 \
    || echo "ofz step failed (non-fatal)" >> "$LOG"

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

# Portfolio monitoring, second pass (Portfolio preset from full universe
# task, 2026-08-29): monitor_portfolios() reads issuer/universe_snapshot
# health for its health-degradation alert (FLAG_TYPE_HEALTH/FLAG_TYPE_RED)
# -- the earlier portfolio-monitor call above (right after `hybond run`)
# ran before scan-universe-health had refreshed today's health_status, so
# it saw yesterday's values for that check specifically. This second call
# is intentionally redundant with the first for G-spread/refi (those
# already had fresh data first time) -- writes a second portfolio_snapshot
# and can duplicate a still-active flag_log alert within the same day, an
# accepted tradeoff for freshness over a more surgical health-only rerun.
echo "=== Portfolio monitoring (post-health-scan) ===" >> "$LOG"
PYTHONPATH=src python -m hybond portfolio-monitor >> "$LOG" 2>&1

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
