# hybond — Assembly Memo for VS Code + Claude Code

This memo is addressed to Claude Code running inside VS Code. Read it fully
before touching any file. Follow the steps in order — each one is a gate for
the next.

---

## CORRECTIONS VS THE PRIOR DRAFT

The earlier memo had three errors. Fixed here:

1. **Tests need `PYTHONPATH=src:tests`**, not `PYTHONPATH=tests` alone (tests
   import from `hybond` which lives in `src/`).
2. **`HYBOND_DB` env var does not exist** in the code — the DB path is set
   directly on `hybond.store.db.DB_PATH`. The env var was a fiction. Removed.
3. **`init_filing` and `init_pillars` must be called** before the store tables
   they own exist. The base `init_db()` only creates Slice 1+2 tables. Steps 8
   and 9 below call the right init functions explicitly.

---

## 0. What you have

```
hybond/
  PLAN.md                              full architecture + slice roadmap
  README.md                            run reference
  CLAUDE_CODE_MEMO.md                  this file
  watchlist.example.json               issuer template
  src/hybond/                          Python package
    __init__.py
    __main__.py                        CLI  (run / diff / scrape / queue /
                                            financials / grade / selftest)
    calendar/__init__.py
    calendar/wall.py                   maturity wall                [Slice 1]
    compute/__init__.py
    compute/bondmath.py                YTM / G-spread / duration    [Slice 1]
    compute/cashflows.py               MOEX schedule → cash-flows   [Slice 1]
    compute/pillars.py                 six-pillar credit metrics    [Slice 4]
    diff/__init__.py
    diff/engine.py                     three-detector change engine [Slice 2]
    diff_pipeline.py                   diff orchestrator            [Slice 2]
    factlist/__init__.py
    factlist/assemble.py               factlist assembler           [Slice 2]
    financials_pipeline.py             ГИР БО fetch + pillar store  [Slice 4]
    grade.py                           detector grader stub         [Slice 2]
    ingest/__init__.py
    ingest/sources.py                  MOEX ISS + CBR clients       [Slice 1]
    ingest/nalog.py                    ГИР БО / bo.nalog client     [Slice 4]
    pipeline.py                        Slice 1 orchestrator
    scrape/__init__.py
    scrape/edisclosure.py              e-disclosure scraper         [Slice 3]
    scrape_pipeline.py                 scrape orchestrator          [Slice 3]
    skills/__init__.py
    skills/classify_api.py             classifier API stub (OFF)    [Slice 3]
    store/__init__.py
    store/db.py                        SQLite store — ALL slices
  skills/                              Claude Code SKILL.md files
    hy-event-classify/SKILL.md         filing classifier            [Slice 3]
    hy-event-classify/references/
      taxonomy.md                      Russian HY event taxonomy    [Slice 3]
    hy-daily-brief/SKILL.md            daily brief publisher        [Slice 3]
    hy-credit-scorecard/SKILL.md       pillar narrative             [Slice 4]
    hy-thesis-check/SKILL.md           thesis tracker               [Slice 4]
  tests/
    fixtures/sample.py                 offline bond + curve data
    test_slice1.py                     9 tests   — bondmath, cashflows,
                                                   calendar, store
    test_slice2.py                     10 tests  — diff engine, factlist,
                                                   grader
    test_slice3.py                     9 tests   — scraper parser, dedupe,
                                                   filing store, classify stub
    test_slice4.py                     9 tests   — nalog normalise, pillars
                                                   math, factlist integration,
                                                   thesis store
```

**37 tests total. All run offline. All must pass before touching live data.**

---

## 1. Create the data directories

Open a terminal inside VS Code (`Ctrl+`` ``). Make sure you are inside the
`hybond/` folder, then run:

```bash
mkdir -p data/db data/raw data/raw/edisclosure data/raw/nalog data/factlists
```

Confirm the full layout with:

```bash
find src/hybond -name "*.py" | sort
find skills -name "*.md" | sort
ls tests/
```

If any file listed in Section 0 is missing, locate it before continuing.

---

## 2. Python environment

Requires **Python 3.10 or later** (the code uses `X | Y` union type syntax).

```bash
python3 --version          # confirm >= 3.10
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install requests pytest selenium
```

Only three third-party packages across the entire codebase:
- `requests` — all HTTP calls (MOEX ISS, CBR, parser-api)
- `pytest` — test runner only
- `selenium` — e-disclosure scraper only (Slice 3)

Nothing else. Do not install additional packages.

Verify the environment is complete:

```bash
python -c "import requests, sqlite3, json, statistics, hashlib; print('ok')"
```

---

## 3. Run the test suite — the assembly gate

**This is the single most important step.** All 37 tests are fully offline
(fixtures + in-memory SQLite). If they pass, the code is correctly assembled.

```bash
PYTHONPATH=src:tests python -m pytest tests/ -v
```

**Expected: 37 passed, 0 failed.**

Do not proceed past this step until all 37 pass.

Common failure causes:

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: hybond` | `PYTHONPATH` missing `src` | Use `PYTHONPATH=src:tests` |
| `ModuleNotFoundError: test_slice1` | `PYTHONPATH` missing `tests` | Use `PYTHONPATH=src:tests` |
| `X \| Y` SyntaxError | Python < 3.10 | Upgrade Python |
| `ImportError` on a specific module | File not copied | Check `src/hybond/` tree |
| `OperationalError: no such table` | `__init__.py` missing in a subpackage | Create empty `__init__.py` in the affected dir |

---

## 4. Slice 1 — connectivity check

```bash
PYTHONPATH=src python -m hybond selftest
```

Expected:
```
[ok]  MOEX bond master: NNNN bonds
[ok]  CBR ZCYC: 10 points, 1y=0.XX
```

**If MOEX fails:** ask Claude Code to open `src/hybond/ingest/sources.py`,
fetch `https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json`
and verify the column names match `fetch_bond_master()`.

**If CBR ZCYC fails:** ask Claude Code to fetch
`https://www.cbr.ru/hd_base/zcyc_params/` and update the `_parse_cbr_zcyc`
function's regex at the bottom of `ingest/sources.py` to match the current
page layout. The function returns `[]` on a layout mismatch (by design) and
`selftest` will report `[FAIL]` clearly.

---

## 5. Build your watchlist

```bash
cp watchlist.example.json watchlist.json
```

Edit `watchlist.json`. Each issuer needs four fields:

```json
"7714699320": {
  "name":            "Issuer display name",
  "sector":          "leasing",
  "edisclosure_id":  "12345",
  "secids":          ["RU000A107SM5", "RU000A10XXX2"]
}
```

| Field | Where to find it |
|---|---|
| key (`7714699320`) | The INN (ИНН) — 10-digit number; used as the unique key |
| `name` | Display name for reports |
| `sector` | leasing / microfinance / realty / transport / etc |
| `edisclosure_id` | Number in `company.aspx?id=NNN` on e-disclosure.ru |
| `secids` | MOEX bond tickers — find via ISS or MOEX bond search |

Start with 5–10 issuers. The `edisclosure_id` is needed only for Slice 3.

---

## 6. Slice 1 — first pipeline run (prices + spreads + maturity wall)

```bash
PYTHONPATH=src python -m hybond run watchlist.json --out data/report.json
```

This fetches bond master + EOD prices + CBR curve, computes YTM / G-spread /
duration for every watchlist bond, stores everything in
`data/db/hybond.sqlite`, and writes `data/report.json`.

Open `data/report.json` and confirm:
- `spread_screen` has rows with non-null `ytm_pct` and `g_spread_bp`.
- `maturity_wall` has monthly buckets for upcoming payments.

A bond with null `ytm_pct` means MOEX had no last price that day — normal for
illiquid ВДО. Ignore it.

The database tables created by this step: `issuer`, `bond`,
`price_snapshot`, `spread_snapshot`.

---

## 7. Slice 2 — change detection

The diff engine needs two days of snapshots to produce a delta. Either run
Step 6 again the next day, or test immediately with seeded synthetic data:

```bash
PYTHONPATH=src python3 - << 'EOF'
from datetime import date as D
from pathlib import Path
import hybond.store.db as dbm
dbm.DB_PATH = Path('data/db/demo_diff.sqlite')
from hybond.store import db
from hybond import diff_pipeline

con = db.connect()
db.init_db(con)
db.upsert_issuer(con, '7701234567', 'Demo Issuer', 'leasing')
db.upsert_bond(con, secid='RU000DEMO1', isin='RU000DEMO1', issuer_id='7701234567',
               short_name='Demo', mat_date='2028-07-25', face_value=1000.0,
               face_unit='SUR', coupon_pct=18.0, list_level=3, qualified=1,
               updated='x')
for day, spread in [('2026-07-24', 600.0), ('2026-07-25', 670.0)]:
    db.insert_spread_snapshot(con, secid='RU000DEMO1', snap_date=day, ytm=0.2,
        tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=spread, mod_dur=1.5, convexity=3.0)
    db.insert_price_snapshot(con, secid='RU000DEMO1', snap_date=day, last_pct=92.0,
        yield_moex=20.0, duration=560, volume=500000, num_trades=20, systime=day)
con.commit()
con.close()
import json
rep = diff_pipeline.run_diff(snap_date=D(2026, 7, 25))
print(json.dumps(rep, ensure_ascii=False, indent=2))
EOF
```

Expected: `attention_issuers` contains `7701234567`, `flag_tally_by_detector`
shows `{"fixed": 1}`. The 70bp move clears the 50bp fixed threshold.

Once real data is accumulating, run daily:

```bash
PYTHONPATH=src python -m hybond diff --factlist-dir data/factlists
```

Factlists for changed issuers are written to `data/factlists/`. These are
the JSON files you paste into the skills (Slices 3–4).

New database tables: `flag_log`, `flag_outcome`.

---

## 8. Slice 3 — e-disclosure event scraper

### Prerequisites

Install ChromeDriver matching your installed Chrome version:

```bash
chromedriver --version   # must print a version, not "command not found"
```

If missing: download from https://chromedriver.chromium.org/downloads or use
`brew install chromedriver` (Mac) / the Linux package manager equivalent.

### Run the scraper

```bash
PYTHONPATH=src python -m hybond scrape watchlist.json --selenium
PYTHONPATH=src python -m hybond queue
```

`queue` shows every new filing that needs classification.

### Initialise the Slice 3 tables explicitly

The first time Slice 3 runs, call this once to create the `filing` and `event`
tables (the scraper calls `init_filing` internally, but if you want to check
the DB state before scraping, run this first):

```bash
PYTHONPATH=src python3 -c "
from hybond.store import db
con = db.connect()
db.init_db(con)
db.init_filing(con)
print('filing + event tables ready')
con.close()
"
```

### Classify filings with hy-event-classify

For each filing shown by `queue`:

1. Open `skills/hy-event-classify/SKILL.md` in VS Code.
2. In the Claude Code panel, paste the filing title + body text, then paste
   the relevant factlist from `data/factlists/<INN>_<date>.json`.
3. Claude Code returns a JSON block (category, severity, direction, so_what,
   note_ru) and a short prose explanation.
4. Save the result to the database:

```bash
PYTHONPATH=src python3 -c "
from hybond.store import db
from datetime import date
con = db.connect()
db.init_db(con)
db.init_filing(con)
eid = db.insert_event(con,
    issuer_id='YOUR_INN',
    occurred='2026-07-25',
    category='reporting_delay',
    severity='high',
    summary='paste so_what from the classifier here',
    source_url='https://www.e-disclosure.ru/...')
db.mark_classified(con, 'BODY_HASH_FROM_QUEUE')
con.commit()
con.close()
print('event', eid, 'saved')
"
```

Replace `YOUR_INN`, the date, the summary, the url, and the `body_hash` (from
the `queue` output) with real values.

New database tables: `filing`, `event`.

---

## 9. Slice 4 — ГИР БО financials + six pillars

### Get a parser-api key

Email support@parser-api.com. Free tier: 200 requests/month, no conditions.
This covers a ~25-issuer annual watchlist many times over.

### Initialise the Slice 4 tables explicitly

```bash
PYTHONPATH=src python3 -c "
from hybond.store import db
con = db.connect()
db.init_db(con)
db.init_pillars(con)
print('financials_snapshot + pillars_snapshot + thesis tables ready')
con.close()
"
```

### Fetch financials + compute pillars

```bash
export PARSER_API_KEY=your_key_here
PYTHONPATH=src python -m hybond financials watchlist.json
```

The issuer key in `watchlist.json` must be the ИНН — that is what the ГИР БО
lookup uses. Output shows per-issuer sub-scores (0–100) and key ratios.

### Use hy-credit-scorecard

After `financials` has run, the factlist for each issuer now has `six_pillars`
populated. To get the written credit assessment:

1. Run diff to refresh the factlist:
   `PYTHONPATH=src python -m hybond diff --factlist-dir data/factlists`
2. Open `skills/hy-credit-scorecard/SKILL.md` in VS Code.
3. Paste the factlist JSON into the Claude Code panel.
4. Copy the Russian narrative paragraph to your blog draft.

### Record an investment thesis

```bash
PYTHONPATH=src python3 -c "
from hybond.store import db
from datetime import date
con = db.connect()
db.init_db(con)
db.init_pillars(con)
db.upsert_thesis(con,
    issuer_id='YOUR_INN',
    thesis='рефинансирует выпуск 2027 г. без технического дефолта',
    assumptions='EBITDA > 300 млн; сохраняет кредитную линию в банке X',
    invalidation='пропуск купона; задержка отчётности > 30 дн.; leverage > 6x',
    status='holding',
    updated=date.today().isoformat())
con.commit()
con.close()
print('thesis saved')
"
```

### Use hy-thesis-check

Open `skills/hy-thesis-check/SKILL.md` in VS Code. Paste the factlist (which
now contains `thesis_status` and `events_since_last`). The skill returns
strengthening / holding / weakening / broken with the exact triggering evidence.

New database tables: `financials_snapshot`, `pillars_snapshot`, `thesis`.

---

## 10. Database — full table reference

All data is in one file: `data/db/hybond.sqlite`.

Which init function creates which tables:

| init function | call it in | tables created |
|---|---|---|
| `db.init_db(con)` | always first | issuer, bond, price_snapshot, spread_snapshot, flag_log, flag_outcome, event, thesis |
| `db.init_filing(con)` | before Slice 3 | filing |
| `db.init_pillars(con)` | before Slice 4 | financials_snapshot, pillars_snapshot |

Full table inventory:

| Table | Slice | Contents |
|---|---|---|
| `issuer` | 1 | INN, name, sector |
| `bond` | 1 | ISIN, SECID, coupon %, maturity date, list level |
| `price_snapshot` | 1 | Daily EOD price / yield / volume per bond |
| `spread_snapshot` | 1 | Daily computed YTM, G-spread, duration per bond |
| `flag_log` | 2 | Every flag fired by every detector, every run |
| `flag_outcome` | 2 | Human verdict on each flag (for grading) |
| `event` | 2/3 | Classified credit events per issuer |
| `thesis` | 2/4 | Investment theses and current status per issuer |
| `filing` | 3 | e-disclosure filings; dedupe by body_hash |
| `financials_snapshot` | 4 | Raw ГИР БО JSON per issuer per year |
| `pillars_snapshot` | 4 | Computed six-pillar metrics per issuer per year |

Useful queries (run inside `sqlite3 data/db/hybond.sqlite`):

```sql
-- today's spread screen, widest first
SELECT b.short_name, s.g_spread_bp, round(s.ytm*100,2) AS ytm_pct, s.snap_date
FROM spread_snapshot s JOIN bond b ON s.secid=b.secid
WHERE s.snap_date = date('now') ORDER BY s.g_spread_bp DESC;

-- flags fired today, by detector
SELECT issuer_id, detector, signal, round(metric,1) AS metric, detail
FROM flag_log WHERE snap_date = date('now') ORDER BY detector;

-- unclassified filings in the queue
SELECT issuer_id, published, title, body_hash
FROM filing WHERE classified=0 ORDER BY published DESC;

-- pillar sub-scores for all issuers, latest period
SELECT issuer_id, period,
  json_extract(payload,'$.score_leverage')    AS leverage,
  json_extract(payload,'$.score_liquidity')   AS liquidity,
  json_extract(payload,'$.score_refinancing') AS refinancing,
  json_extract(payload,'$.score_operations')  AS operations,
  json_extract(payload,'$.debt_to_ebit')      AS debt_ebit
FROM pillars_snapshot ORDER BY period DESC;

-- current thesis statuses
SELECT issuer_id, status, substr(thesis,1,60) AS thesis_preview, updated
FROM thesis ORDER BY updated DESC;

-- events in the last 30 days
SELECT issuer_id, occurred, category, severity, summary
FROM event WHERE occurred >= date('now','-30 days') ORDER BY occurred DESC;
```

---

## 11. Daily workflow (once all slices are running)

```bash
cd hybond && source .venv/bin/activate

# --- morning run, ~2–3 minutes ---
PYTHONPATH=src python -m hybond run watchlist.json          # Slice 1: prices
PYTHONPATH=src python -m hybond scrape watchlist.json --selenium  # Slice 3: events
PYTHONPATH=src python -m hybond diff --factlist-dir data/factlists  # Slice 2: gate

# --- check what needs attention ---
PYTHONPATH=src python -m hybond queue
# For each new filing: invoke hy-event-classify in Claude Code (see Step 8)

# --- weekly: refresh financials (annual data, harmless to re-run) ---
PYTHONPATH=src python -m hybond financials watchlist.json   # Slice 4: pillars

# --- after 4–6 weeks: grade the three detectors ---
PYTHONPATH=src python -m hybond grade
```

---

## 12. What to ask Claude Code to do

Open the Claude Code panel in VS Code (`Ctrl+Shift+P` → "Claude Code: Focus").

| Task | What to do |
|---|---|
| Invoke a skill | Type "read `skills/hy-event-classify/SKILL.md`" then paste the factlist + filing |
| Fix a broken parser | Paste the raw file from `data/raw/` and ask to update the function |
| Add an issuer | Paste the e-disclosure company URL; ask to extract the id and add to `watchlist.json` |
| Extend the taxonomy | Ask to add a new entry to `skills/hy-event-classify/references/taxonomy.md` |
| Enable classifier automation | Ask to wire the Anthropic client in `src/hybond/skills/classify_api.py` |
| Tune pillar thresholds | Ask to adjust the scoring bands in `src/hybond/compute/pillars.py` |
| Build Slice 5 | Ask to wire `hy-daily-brief` into a Telegram sender using the diff output |

---

## 13. When something breaks

1. **Run pytest first.** `PYTHONPATH=src:tests python -m pytest tests/ -v`.
   Do not debug a pipeline failure until all 37 tests pass — a test failure
   means the code itself is wrong, not the environment.

2. **Check `data/raw/`.** Every network fetch is snapshotted before parsing.
   If a parser gives wrong output, the raw file is the ground truth to
   compare against.

3. **The database is append-only for snapshots.** Re-running a day is always
   safe. If one day's data is corrupt, delete only those rows:
   `DELETE FROM spread_snapshot WHERE snap_date='2026-07-25';`

4. **Never delete `data/db/hybond.sqlite`** unless starting fresh. The
   `flag_log` history is what makes the `grade` step meaningful after 4–6 weeks.

5. **The diff demo creates `data/db/demo_diff.sqlite`.** You can delete that
   file safely; it is only a test artifact.

---

*End of memo. Start at Step 1 and work downward.*
