#!/bin/bash
# hybond database backup — runs twice daily at 06:00 and 18:00
# Backs up SQLite DB + exports + factlists + cbonds_sample to external drive

BACKUP_DIR="/media/sergei/18ACF49AACF4741A/hybond_backups"
PROJECT_DIR="/home/sergei/Documents/high risk bond analysis"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG="$PROJECT_DIR/data/logs/backup_$TIMESTAMP.log"

echo "=== backup run $(date) ===" >> "$LOG"

# Check backup drive is mounted
if [ ! -d "$BACKUP_DIR" ]; then
    echo "ERROR: Backup drive not mounted at $BACKUP_DIR" >> "$LOG"
    exit 1
fi

# 1. SQLite DB — safe even if DB is open
sqlite3 "$PROJECT_DIR/data/db/hybond.sqlite" \
    ".backup '$BACKUP_DIR/hybond_$TIMESTAMP.sqlite'" >> "$LOG" 2>&1
echo "DB backed up" >> "$LOG"

# 2. Exports (classified events, ratings, PD table PDFs)
cp -r "$PROJECT_DIR/data/exports" "$BACKUP_DIR/exports_$TIMESTAMP" >> "$LOG" 2>&1
echo "Exports backed up" >> "$LOG"

# 3. Factlists (diff engine state — not regeneratable without losing history)
cp -r "$PROJECT_DIR/data/factlists" "$BACKUP_DIR/factlists_$TIMESTAMP" >> "$LOG" 2>&1
echo "Factlists backed up" >> "$LOG"

# 4. Cbonds sample (36KB, small)
cp -r "$PROJECT_DIR/data/cbonds_sample" "$BACKUP_DIR/cbonds_sample_$TIMESTAMP" >> "$LOG" 2>&1
echo "Cbonds sample backed up" >> "$LOG"

# Keep only last 14 backups (7 days × 2/day) — delete older
ls -t "$BACKUP_DIR"/hybond_*.sqlite 2>/dev/null | tail -n +15 | xargs rm -f
ls -td "$BACKUP_DIR"/exports_* 2>/dev/null | tail -n +15 | xargs rm -rf
ls -td "$BACKUP_DIR"/factlists_* 2>/dev/null | tail -n +15 | xargs rm -rf
ls -td "$BACKUP_DIR"/cbonds_sample_* 2>/dev/null | tail -n +15 | xargs rm -rf

echo "=== done $(date) ===" >> "$LOG"
