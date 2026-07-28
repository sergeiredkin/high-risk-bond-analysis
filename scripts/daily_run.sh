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
PYTHONPATH=src python -m hybond run watchlist.json >> "$LOG" 2>&1

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

# Slice 3: event feed (no --selenium, uses AZIPI plain HTTP)
PYTHONPATH=src python -m hybond scrape watchlist.json >> "$LOG" 2>&1

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

echo "=== done $(date) ===" >> "$LOG"
