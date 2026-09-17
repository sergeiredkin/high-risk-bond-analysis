# hybond — Project Memo for Claude Code

This is a living project memo, not a one-time setup script. hybond has been
assembled, deployed, and running live in production (real MOEX/CBR/e-disclosure/
ГИР БО data, daily cron, local-Ollama classification) for weeks. This file
orients a fresh Claude Code session to the current state of the project —
architecture, data model, automation, and the operational rules that have come
out of real incidents. For exact CLI flags, see `README.md`'s command table
(27 subcommands) or `hybond <cmd> --help`.

---

## 1. Architecture

```
hybond/
  src/hybond/
    __main__.py                 CLI entry point -- 27 subcommands
    store/db.py                 SQLite store, ALL tables, connect() sets WAL
                                 mode + busy_timeout=5000
    pipeline.py                 Slice 1: prices/spreads/maturity wall
    universe_pipeline.py        full MOEX ВДО universe screen, G-spread,
                                 mispricing, AEY scenarios (not watchlist-gated)
    diff_pipeline.py / diff/    Slice 2: change detection (3 detectors)
    scrape_pipeline.py / scrape/edisclosure.py, smartlab.py, nkr.py, nra.py,
      rss_generic.py, smartlab_news.py
                                 Slice 3: e-disclosure + smart-lab/NKR/NRA/
                                 RSS (MOEX listing, CBR press) event feed
    classify_ollama.py          local-Ollama (qwen3.5) auto-classification;
                                 classify_utils.py has queue-bucket priority
    financials_pipeline.py / ingest/nalog.py / compute/pillars.py
                                 Slice 4: ГИР БО financials + six-pillar compute
    compute/default_probs.py    Default Probability Layer (Эксперт РА PD
                                 matrix + 35% recovery) -- implied fair
                                 spread, mispricing_bp
    compute/expected_return.py, ofz_carry.py, inflection.py, bondmath.py,
      cashflows.py
                                 YTM/duration/convexity, OFZ carry/roll-down,
                                 expected-return and inflection-point math
    portfolio.py                 AEY rate-scenario derivation from CBR meetings
    promote.py                  Phase 2 candidate shortlist + manual-candidate
                                 -> real-issuer path (company-scan promotions)
    company_scan_pipeline.py    ad hoc due-diligence scans on issuers not yet
                                 on the watchlist (company_scan_result table)
    cbr_tracker.py               CBR key-rate meeting tracker; meeting schedule from the
                                 CBR calendar (cbr_schedule), missed-meeting catch-up
    expectations.py              CBR forecasts, ОНЕГДКП scenarios, macro survey, decision
                                 documents, daily key rate -- with history (hybond expectations)
    discover.py                  MOEX-vs-watchlist.json coverage diff (kept
                                 deliberately independent of the tier system)
    ofz_pipeline.py               OFZ (government bond) universe
    spravka_parser.py             parses spravka_vdo.md for the dashboard's
                                 Справка tab
  dashboard/
    template.html + src/         redesign source (split CSS/JS, inlined by
                                 build_dashboard.py's INCLUDE markers) plus
                                 data/dashboard_data.json -> index.html
    legacy/template.html         pre-redesign dashboard, -> legacy/index.html
  scripts/
    export_dashboard_data.py, build_dashboard.py
                                 dashboard rebuild (also invoked directly by
                                 rebuild_dashboard.sh, not a `hybond` subcommand)
    daily_run.sh, hourly_filings.sh, weekly_financials.sh, cbonds_sample.sh,
      backup_db.sh, startup_catch.sh
                                 cron jobs -- see section 5
  rebuild_dashboard.sh          classification-aware dashboard rebuild
                                 (project root, not scripts/ -- cron target)
  tests/                        39 test files, 473 tests, offline/fixture-based
  skills/                       Claude Code SKILL.md files (hy-event-classify,
                                 hy-daily-brief, hy-credit-scorecard, hy-thesis-check)
  CONTRIBUTING.md                Definition of Done -- see section 10
```

Single SQLite file: `data/db/hybond.sqlite`, WAL mode. Only three third-party
packages: `requests`, `pytest`, `selenium`.

---

## 2. Watchlist tier system

`issuer.watchlist_tier` (not `watchlist.json`) is the source of truth for
which issuers are actively tracked:

- **tier=1** — active watchlist. Full dashboard card + verdict. Drives the
  classify queue's priority ordering.
- **tier=2** — passive monitoring. Collapsed dashboard row, no verdict.
  Confirmed-default issuers and company-scan `hold` decisions land here.

`watchlist_tier_log` is the audit trail for tier *changes* on existing
issuers (`hybond watchlist-promote` / `watchlist-demote`, both require
`--reason`). It does NOT log brand-new issuer creation via
`hybond promote --confirm` (`db.add_issuer_at_tier()`) — there is no "from
tier" for a new row, by design.

`issuer_bond` is the persistent issuer→bond mapping (with a `delisted` flag)
— neither the live `universe_snapshot` (drops delisted bonds) nor `bond`
(only populated by past `pipeline.run()` calls) is independently complete;
`issuer_bond` unions `watchlist.json` → `universe_snapshot` → `bond` so a
delisted ISIN that only ever existed in `watchlist.json` isn't silently lost.

`watchlist.json` is now an **optional enrichment lookup** (risk_tier, note,
edisclosure_id per issuer_id), not the list of which issuers appear anywhere
— `hybond run`, `hybond financials`, `hybond scrape`, and the dashboard export
all iterate `issuer` (tier 1+2), not `watchlist.json`.

**Company-scan → promotion flow:** `hybond scan-company <query>` runs ad hoc
due diligence on an issuer not yet tracked, `scan-company-store` persists the
result to `company_scan_result`, `scan-company-decide --decision avoid|hold|
promote --reason ...` records a verdict. A `hold` (or `promote`) decision
becomes a real watchlist entry via `hybond promote --inn INN --name NAME`
(flags it in `manual_candidate`) then `hybond promote --confirm INN --tier
1|2` (default tier=2 — passive monitoring, not the active watchlist).

---

## 3. Classification pipeline

`hybond auto-classify` calls local Ollama (`qwen3.5:latest`, configurable via
`OLLAMA_MODEL` env var / `.env`) per filing. Key mechanics:

- **`think` must be a top-level payload field**, not nested under `options`
  — Ollama silently ignores it there (`invalid option provided option=think`
  server-side), which was the real cause of a whole session's worth of empty-
  response failures: the model ran full unsuppressed reasoning and exhausted
  the 4096-token context before ever emitting JSON.
- **Tier-priority queue**: `get_auto_classify_queue()` sorts tier=1 issuers'
  filings before tier=2 before everything else, then by content-priority
  bucket (`classify_utils.queue_bucket()`), then newest first.
- **Per-batch commits** (every `AUTO_CLASSIFY_BATCH_SIZE=5` items, with a
  brief `time.sleep(0.1)` yield) — the fix for a real "database is locked"
  root cause: one uncommitted transaction previously held the write lock for
  up to ~38 minutes on a deep queue.
- **`--pause SECONDS`**: burst mode. With `--pause` set, `--limit` becomes
  the *per-burst* size (not a total budget) — it loops bursts, closing and
  reopening the DB connection each time, until a burst returns fewer items
  than `--limit` (queue drained). Lets a large backlog run release the DB
  between bursts instead of holding one connection for the whole run.
- **`classify_running.lock`** (`data/logs/`) and **`last_classify.timestamp`**
  are written/removed by `cmd_auto_classify` itself (not duplicated in
  calling scripts) — mutual exclusion between the hourly cron's classify
  call and `rebuild_dashboard.sh`'s own catch-up burst. The lock is cleared
  via an explicit `SIGTERM`/`SIGINT` handler, not a bare `try/finally` —
  Python's default `SIGTERM` handling bypasses `finally` entirely (confirmed
  live: a `timeout`-killed run left the lock behind until the handler was
  added).
- **`classify_filings_batch()`** (multi-item-per-call batching) exists,
  unit-tested, but is NOT wired into production — live-tested and found not
  viable before the `think`-placement fix; not re-measured since.
- `hybond queue --skip-noise` clears regex-matched noise-pattern filings
  instantly, no GPU.

---

## 4. Cron schedule

| Time | Job | Purpose |
|---|---|---|
| 05:00, 10:00, 16:00 Mon–Fri | `daily_run.sh` | prices/spreads, universe screen, OFZ snapshot, promote report, scrape, market-notes, CBR check + catch-up, rate expectations, diff/factlists, dashboard rebuild |
| `:15` hourly, Mon–Fri | `hourly_filings.sh` | scrape + market-notes + `auto-classify --limit 30` |
| `*/30 * * * *` | `rebuild_dashboard.sh` (project root) | classification-aware dashboard rebuild — see section 6 |
| 06:47 daily | `cbonds_sample.sh` | Cbonds sample data |
| 07:00 Sun | `weekly_financials.sh` | annual financials refresh (needs `PARSER_API_KEY`) |
| 06:00, 18:00 daily | `backup_db.sh` | DB backup |
| `@reboot` | `startup_catch.sh` | polls MOEX ISS for real network readiness (not a fixed sleep — a fixed 120s sleep once raced a slow post-reboot DNS), catches up any missed daily/hourly run |

`daily_run.sh`'s 05:00 slot has historically never fired in practice (machine
off overnight) — 10:00 is effectively the first real run of the day.

---

## 5. Dashboard rebuild

`rebuild_dashboard.sh` (project root, not `scripts/`): waits up to 3 min for
`classify_running.lock` to clear, runs a catch-up `auto-classify --limit 30
--pause 8` burst only if classify hasn't run in the last 20 min, then
`scripts/export_dashboard_data.py` + `scripts/build_dashboard.py`.

Two distinct, deliberately separate date signals in the dashboard's hero line:
- **`day2_snap_date`** — the real DB data-snapshot date. Drives every "as of"
  label across the dashboard. Never overridden by wall-clock time.
- **`rebuild_at`** — `datetime.now().isoformat()`, written at export time.
  Answers "how recently was this HTML page regenerated," a different
  question from data freshness. Forcing `day2_snap_date` to always show
  today was explicitly considered and rejected — it would hide genuine
  staleness (exactly the kind a DNS/cron-lock bug caused once already).

---

## 6. Test suite

```bash
PYTHONPATH=src python -m pytest tests/ -v
```

**473 tests, 39 files, fully offline** (fixtures + in-memory/tmp SQLite). Run
this after every backend change; do not debug a pipeline failure until it's
green — a test failure means the code is wrong, not the environment.

---

## 7. What to ask Claude Code to do

| Task | What to do |
|---|---|
| Invoke a skill | Type "read `skills/hy-event-classify/SKILL.md`" then paste the factlist + filing |
| Fix a broken parser | Paste the raw file from `data/raw/` and ask to update the function |
| Add an issuer to the active watchlist | `hybond watchlist-promote <inn> --reason ...` (or edit `watchlist.json` for the enrichment fields) |
| Scan an issuer not yet tracked | `hybond scan-company <query>`, then `scan-company-store` / `scan-company-decide` |
| Extend the taxonomy | Ask to add a new entry to `skills/hy-event-classify/references/taxonomy.md` |
| Tune pillar thresholds | Ask to adjust the scoring bands in `src/hybond/compute/pillars.py` |
| Build Slice 5 | Ask to wire `hy-daily-brief` into a Telegram sender using the diff output |

---

## 8. Database — table reference

All data lives in `data/db/hybond.sqlite`. Key tables beyond the Slice 1–4
basics (`issuer`, `bond`, `price_snapshot`, `spread_snapshot`, `flag_log`,
`flag_outcome`, `event`, `thesis`, `filing`, `financials_snapshot`,
`pillars_snapshot`):

| Table | Contents |
|---|---|
| `watchlist_tier_log` | audit trail for tier changes on existing issuers |
| `issuer_bond` | persistent issuer→bond mapping with `delisted` flag |
| `manual_candidate` | company-scan issuers flagged, awaiting `--confirm` |
| `company_scan_result` | full company-scan history + decision |
| `market_note` | smart-lab/MOEX-listing/CBR-press RSS items |
| `rating` | manual credit-rating entries (feeds default-probability layer) |

Useful queries (`sqlite3 data/db/hybond.sqlite`):

```sql
-- unclassified filings in the queue
SELECT issuer_id, published, title, body_hash
FROM filing WHERE classified=0 ORDER BY published DESC;

-- tier=1 (active) vs tier=2 (passive) issuer counts
SELECT watchlist_tier, COUNT(*) FROM issuer GROUP BY watchlist_tier;

-- latest company-scan decisions
SELECT id, inn, issuer_name, decision, scanned_at
FROM company_scan_result ORDER BY id DESC LIMIT 10;
```

---

## 9. When something breaks

1. **Run pytest first.** `PYTHONPATH=src python -m pytest tests/ -v`.
   Do not debug a pipeline failure until all 473 tests pass — a test failure
   means the code itself is wrong, not the environment.

2. **Check `data/raw/`.** Every network fetch is snapshotted before parsing.
   If a parser gives wrong output, the raw file is the ground truth to
   compare against.

3. **The database is append-only for snapshots.** Re-running a day is always
   safe. If one day's data is corrupt, delete only those rows:
   `DELETE FROM spread_snapshot WHERE snap_date='2026-07-25';`

4. **Never delete `data/db/hybond.sqlite`** unless starting fresh. The
   `flag_log` history is what makes the `grade` step meaningful, and years
   of live classify/scan/tier history live nowhere else.

5. **The diff demo creates `data/db/demo_diff.sqlite`.** You can delete that
   file safely; it is only a test artifact.

6. **All work must satisfy the Definition of Done in `CONTRIBUTING.md`
   before marking a task complete.**

7. **If auto-classify shows repeated Ollama timeouts, run
   `systemctl restart ollama` before investigating further.**

---

*This memo reflects project state as of 2026-08-21. Update it as the project
changes — it should stay a living document, not another stale bootstrap memo.*
