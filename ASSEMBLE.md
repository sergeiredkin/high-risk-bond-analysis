# hybond — Assembly and Verification Memo for Claude Code (VS Code)

This memo is addressed to Claude Code running inside VS Code.

## Situation

The user built four slices in Claude.ai chat and downloaded them.
Slices 1 and 2 are already imported into the project.
Slices 3 and 4 were downloaded as separate folders named `files3/` and `files4/`
and placed somewhere inside or alongside the project folder.

Your job is to:
1. Find `files3/` and `files4/`.
2. Move every file from those folders into the correct location in the project.
3. Verify the final layout is complete and correct.
4. Run the test suite and confirm all 37 tests pass.
5. Report anything missing or broken.

Do not skip any step. Do not assume a file is in place — check it.

---

## Step 1 — Find files3 and files4

Run this from the project root:

```bash
find . -type d \( -name "files3" -o -name "files4" \) 2>/dev/null
```

If they are not inside the project root, search one level up:

```bash
find .. -maxdepth 3 -type d \( -name "files3" -o -name "files4" \) 2>/dev/null
```

Note the exact paths before doing anything else.

Then list what is inside each:

```bash
find PATH_TO_files3 -type f | sort
find PATH_TO_files4 -type f | sort
```

---

## Step 2 — Required contents of files3 and files4

### files3 must contain exactly these files (Slice 3):

```
src/hybond/scrape/__init__.py
src/hybond/scrape/edisclosure.py
src/hybond/scrape_pipeline.py
src/hybond/skills/__init__.py
src/hybond/skills/classify_api.py
skills/hy-event-classify/SKILL.md
skills/hy-event-classify/references/taxonomy.md
skills/hy-daily-brief/SKILL.md
tests/test_slice3.py
```

Also requires that `store/db.py` gained two new functions compared to Slice 2:
`init_filing` and functions `insert_filing`, `seen_filing_hashes`,
`unclassified_filings`, `mark_classified`, `insert_event`, `events_since`.
These are part of `store/db.py` which was extended in Slice 3 — if the user
only has the original Slice 1+2 version of `store/db.py`, it will be missing
these functions. Check this in Step 4.

### files4 must contain exactly these files (Slice 4):

```
src/hybond/compute/pillars.py
src/hybond/ingest/nalog.py
src/hybond/financials_pipeline.py
skills/hy-credit-scorecard/SKILL.md
skills/hy-thesis-check/SKILL.md
tests/test_slice4.py
```

Also requires that `store/db.py` gained further functions compared to Slice 3:
`init_pillars`, `upsert_financials`, `upsert_pillars`, `latest_pillars`,
`upsert_thesis`, `get_thesis`, `set_thesis_status`.
Same concern as above — the user's current `store/db.py` must be the final
cumulative version that includes all slice additions.

Also requires that `factlist/assemble.py` is the final version that populates
`six_pillars`, `events_since_last`, and `thesis_status` — not the Slice 2
version that left these as empty placeholders.

---

## Step 2b — Handle files_missing (Slice 1/2 files that were not in the original download)

Five files from Slices 1 and 2 were never packaged in the original Slice 1+2
download and are not in `files3/` or `files4/`. They are provided separately
in a folder called `files_missing/`. Find it:

```bash
find . -type d -name "files_missing" 2>/dev/null
find .. -maxdepth 3 -type d -name "files_missing" 2>/dev/null
```

It contains exactly these five files, already laid out in the correct
subdirectory structure:

```
files_missing/
  CLAUDE_CODE_MEMO.md                    → copy to project root
  watchlist.example.json                 → copy to project root
  src/hybond/__main__.py                 → copy to src/hybond/
  src/hybond/compute/cashflows.py        → copy to src/hybond/compute/
  tests/fixtures/sample.py              → copy to tests/fixtures/
```

Copy them to the project root (replace `PATH_TO_files_missing` with the
actual path found above):

```bash
cp PATH_TO_files_missing/CLAUDE_CODE_MEMO.md .
cp PATH_TO_files_missing/watchlist.example.json .
cp PATH_TO_files_missing/src/hybond/__main__.py src/hybond/
cp PATH_TO_files_missing/src/hybond/compute/cashflows.py src/hybond/compute/
mkdir -p tests/fixtures
cp PATH_TO_files_missing/tests/fixtures/sample.py tests/fixtures/
```

What each file is and why it is critical:

| File | Why critical |
|---|---|
| `__main__.py` | The CLI entry point — without it `python -m hybond` fails immediately |
| `cashflows.py` | MOEX coupon/amort schedule → cash-flow pairs; imported by `pipeline.py` and all four test files |
| `tests/fixtures/sample.py` | Offline bond + curve data used by all 37 tests; without it test collection fails before any test runs |
| `watchlist.example.json` | Template the user copies to build their issuer watchlist |
| `CLAUDE_CODE_MEMO.md` | Operational guide for running the platform once assembled |

**Without `cashflows.py` and `tests/fixtures/sample.py`, zero tests run.**
These two are the immediate blockers. Copy all five before continuing.

---

## Step 3 — Move files into the correct project locations

For each file found in `files3/` and `files4/`, move it to the matching path
under the project root. The layout the project expects is:

```
hybond/                              ← project root
  src/
    hybond/
      __init__.py                    [Slice 1]
      __main__.py                    [Slice 1, extended through Slice 4]
      calendar/
        __init__.py                  [Slice 1]
        wall.py                      [Slice 1]
      compute/
        __init__.py                  [Slice 1]
        bondmath.py                  [Slice 1]
        cashflows.py                 [Slice 1]
        pillars.py                   [Slice 4] ← from files4
      diff/
        __init__.py                  [Slice 2]
        engine.py                    [Slice 2]
      diff_pipeline.py               [Slice 2]
      factlist/
        __init__.py                  [Slice 2]
        assemble.py                  [Slice 2, extended in Slice 4]
      financials_pipeline.py         [Slice 4] ← from files4
      grade.py                       [Slice 2]
      ingest/
        __init__.py                  [Slice 1]
        sources.py                   [Slice 1]
        nalog.py                     [Slice 4] ← from files4
      pipeline.py                    [Slice 1]
      scrape/
        __init__.py                  [Slice 3] ← from files3
        edisclosure.py               [Slice 3] ← from files3
      scrape_pipeline.py             [Slice 3] ← from files3
      skills/
        __init__.py                  [Slice 3] ← from files3
        classify_api.py              [Slice 3] ← from files3
      store/
        __init__.py                  [Slice 1]
        db.py                        [ALL slices — must be the cumulative version]
  skills/
    hy-event-classify/
      SKILL.md                       [Slice 3] ← from files3
      references/
        taxonomy.md                  [Slice 3] ← from files3
    hy-daily-brief/
      SKILL.md                       [Slice 3] ← from files3
    hy-credit-scorecard/
      SKILL.md                       [Slice 4] ← from files4
    hy-thesis-check/
      SKILL.md                       [Slice 4] ← from files4
  tests/
    fixtures/
      sample.py                      [Slice 1]
    test_slice1.py                   [Slice 1]
    test_slice2.py                   [Slice 2]
    test_slice3.py                   [Slice 3] ← from files3
    test_slice4.py                   [Slice 4] ← from files4
  data/
    db/                              ← must exist (create if missing)
    raw/                             ← must exist (create if missing)
    raw/edisclosure/                 ← must exist (create if missing)
    raw/nalog/                       ← must exist (create if missing)
  data/factlists/                    ← must exist (create if missing)
  PLAN.md
  README.md
  CLAUDE_CODE_MEMO.md
  ASSEMBLE.md                        ← this file
  watchlist.example.json
```

Create any missing directories:

```bash
mkdir -p data/db data/raw/edisclosure data/raw/nalog data/factlists
```

---

## Step 4 — Verify db.py is the cumulative version

The most likely assembly problem is that `store/db.py` is the Slice 1+2 version
and is missing the functions added in Slices 3 and 4.

Check by running:

```bash
python3 -c "
import sys; sys.path.insert(0,'src')
from hybond.store import db
needed = [
    'init_filing', 'insert_filing', 'seen_filing_hashes',
    'unclassified_filings', 'mark_classified',
    'insert_event', 'events_since',
    'init_pillars', 'upsert_financials', 'upsert_pillars',
    'latest_pillars', 'upsert_thesis', 'get_thesis', 'set_thesis_status',
]
missing = [f for f in needed if not hasattr(db, f)]
if missing:
    print('MISSING from db.py:', missing)
else:
    print('db.py OK — all functions present')
"
```

If anything is missing, the user needs to replace `store/db.py` with the
cumulative version from the downloads. The cumulative `db.py` is the one that
contains all four slices' additions in one file — it is the largest version
and should be around 280–320 lines. Check: `wc -l src/hybond/store/db.py`.

---

## Step 5 — Verify factlist/assemble.py is the Slice 4 version

The Slice 2 version of `assemble.py` returns empty placeholders for
`six_pillars`, `events_since_last`, and `thesis_status`. The Slice 4 version
populates them from the database.

Check:

```bash
grep -n "_latest_pillars\|_thesis_status\|_events_since_last" src/hybond/factlist/assemble.py
```

Expected: three matches. If the grep returns nothing, the user has the Slice 2
version and needs to replace `factlist/assemble.py` with the Slice 4 version.

---

## Step 6 — Verify __main__.py is the Slice 4 version

The CLI gains new subcommands with each slice. The final version has seven:
`selftest`, `run`, `diff`, `grade`, `scrape`, `queue`, `financials`.

Check:

```bash
grep "add_parser" src/hybond/__main__.py | sort
```

Expected output contains all seven of:
```
selftest
run
diff
grade
scrape
queue
financials
```

If `scrape`, `queue`, or `financials` are missing, replace `__main__.py` with
the Slice 4 version.

---

## Step 7 — Verify all __init__.py files exist

Python requires an `__init__.py` in every package directory. Check:

```bash
for d in src/hybond \
          src/hybond/calendar \
          src/hybond/compute \
          src/hybond/diff \
          src/hybond/factlist \
          src/hybond/ingest \
          src/hybond/scrape \
          src/hybond/skills \
          src/hybond/store; do
  if [ -f "$d/__init__.py" ]; then
    echo "OK  $d/__init__.py"
  else
    echo "MISSING  $d/__init__.py"
  fi
done
```

For every MISSING line, create the file:

```bash
touch src/hybond/scrape/__init__.py   # example — repeat for each missing one
```

---

## Step 8 — Verify skills folder

```bash
for f in \
  "skills/hy-event-classify/SKILL.md" \
  "skills/hy-event-classify/references/taxonomy.md" \
  "skills/hy-daily-brief/SKILL.md" \
  "skills/hy-credit-scorecard/SKILL.md" \
  "skills/hy-thesis-check/SKILL.md"; do
  if [ -f "$f" ]; then echo "OK  $f"
  else echo "MISSING  $f"; fi
done
```

All five must show OK. If any are missing, copy them from files3 or files4.

---

## Step 9 — Full file existence check

Run this single command. It checks every required file in one pass:

```bash
python3 - << 'EOF'
import os

required = [
    # Slice 1
    "src/hybond/__init__.py",
    "src/hybond/__main__.py",
    "src/hybond/calendar/__init__.py",
    "src/hybond/calendar/wall.py",
    "src/hybond/compute/__init__.py",
    "src/hybond/compute/bondmath.py",
    "src/hybond/compute/cashflows.py",
    "src/hybond/ingest/__init__.py",
    "src/hybond/ingest/sources.py",
    "src/hybond/pipeline.py",
    "src/hybond/store/__init__.py",
    "src/hybond/store/db.py",
    "tests/fixtures/sample.py",
    "tests/test_slice1.py",
    "watchlist.example.json",
    # Slice 2
    "src/hybond/diff/__init__.py",
    "src/hybond/diff/engine.py",
    "src/hybond/diff_pipeline.py",
    "src/hybond/factlist/__init__.py",
    "src/hybond/factlist/assemble.py",
    "src/hybond/grade.py",
    "tests/test_slice2.py",
    # Slice 3
    "src/hybond/scrape/__init__.py",
    "src/hybond/scrape/edisclosure.py",
    "src/hybond/scrape_pipeline.py",
    "src/hybond/skills/__init__.py",
    "src/hybond/skills/classify_api.py",
    "skills/hy-event-classify/SKILL.md",
    "skills/hy-event-classify/references/taxonomy.md",
    "skills/hy-daily-brief/SKILL.md",
    "tests/test_slice3.py",
    # Slice 4
    "src/hybond/compute/pillars.py",
    "src/hybond/ingest/nalog.py",
    "src/hybond/financials_pipeline.py",
    "skills/hy-credit-scorecard/SKILL.md",
    "skills/hy-thesis-check/SKILL.md",
    "tests/test_slice4.py",
    # Docs
    "PLAN.md",
    "CLAUDE_CODE_MEMO.md",
]

missing = []
ok = []
for f in required:
    if os.path.isfile(f):
        ok.append(f)
    else:
        missing.append(f)

print(f"\n{'='*50}")
print(f"PRESENT : {len(ok)}/{len(required)}")
print(f"MISSING : {len(missing)}")
if missing:
    print("\nFiles to locate and copy:")
    for f in missing:
        print(f"  {f}")
else:
    print("\nAll files present. Proceed to Step 10.")
print('='*50)
EOF
```

Do not proceed to Step 10 until this prints `MISSING: 0`.

---

## Step 10 — Run the test suite

```bash
PYTHONPATH=src:tests python -m pytest tests/ -v
```

Expected result: **37 passed, 0 failed.**

The tests are grouped:
- `test_slice1.py` — 9 tests: bondmath, cashflows, calendar, store
- `test_slice2.py` — 10 tests: diff engine, factlist, grader
- `test_slice3.py` — 9 tests: e-disclosure parser, dedupe, filing store, classify stub
- `test_slice4.py` — 9 tests: ГИР БО normalise, pillar math, factlist integration, thesis store

All run fully offline — no network, no API key, no database file needed.

If any test fails, read the error and cross-reference with the table below.

---

## Failure diagnosis table

| Test file | Failing test | Most likely cause |
|---|---|---|
| test_slice1 | any | Slice 1 file missing or corrupted |
| test_slice2 | `test_factlist_shape` | `assemble.py` is Slice 2 version, not Slice 4 |
| test_slice2 | `test_stat_fires_*` | `diff/engine.py` missing or wrong version |
| test_slice3 | `test_filing_store_*` | `db.py` missing `init_filing` / `insert_filing` |
| test_slice3 | `test_api_hook_is_off` | `skills/classify_api.py` missing |
| test_slice4 | `test_pillars_hand_checked` | `compute/pillars.py` missing or from wrong slice |
| test_slice4 | `test_factlist_pulls_pillars` | `db.py` missing `init_pillars` / `upsert_pillars` |
| test_slice4 | `test_thesis_store_lifecycle` | `db.py` missing `upsert_thesis` / `get_thesis` |
| any | `ModuleNotFoundError: hybond` | `PYTHONPATH` missing `src` |
| any | `ModuleNotFoundError: test_slice1` | `PYTHONPATH` missing `tests` |
| any | `SyntaxError: \|` | Python version < 3.10 |

---

## Step 11 — Final confirmation

Once 37 tests pass, run this summary check:

```bash
python3 - << 'EOF'
import sys
sys.path.insert(0, 'src')
sys.path.insert(0, 'tests')

checks = []

# package imports
try:
    import hybond
    from hybond import pipeline, diff_pipeline, scrape_pipeline, financials_pipeline, grade
    from hybond.compute import bondmath, cashflows, pillars
    from hybond.ingest import sources, nalog
    from hybond.store import db
    from hybond.diff import engine
    from hybond.factlist import assemble
    from hybond.scrape import edisclosure
    from hybond.skills import classify_api
    checks.append(("all package imports", "OK"))
except ImportError as e:
    checks.append(("package import", f"FAIL: {e}"))

# db.py cumulative functions
try:
    needed = ['init_filing','insert_filing','seen_filing_hashes',
              'unclassified_filings','mark_classified','insert_event',
              'events_since','init_pillars','upsert_financials',
              'upsert_pillars','latest_pillars','upsert_thesis',
              'get_thesis','set_thesis_status']
    missing = [f for f in needed if not hasattr(db, f)]
    checks.append(("db.py functions", "OK" if not missing else f"MISSING: {missing}"))
except Exception as e:
    checks.append(("db.py functions", f"FAIL: {e}"))

# factlist is Slice 4 version
try:
    import inspect
    src = inspect.getsource(assemble)
    has_pillars = '_latest_pillars' in src
    has_thesis = '_thesis_status' in src
    checks.append(("factlist/assemble.py version",
                   "OK (Slice 4)" if has_pillars and has_thesis
                   else "WARN: Slice 2 version — missing pillar/thesis wiring"))
except Exception as e:
    checks.append(("factlist version", f"FAIL: {e}"))

# CLI subcommands
try:
    import ast, pathlib
    main_src = pathlib.Path('src/hybond/__main__.py').read_text()
    tree = ast.parse(main_src)
    parsers = [n.args[0].s for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and hasattr(n.func, 'attr') and n.func.attr == 'add_parser'
               and n.args and isinstance(n.args[0], ast.Constant)]
    expected = {'selftest','run','diff','grade','scrape','queue','financials'}
    missing_cmds = expected - set(parsers)
    checks.append(("CLI subcommands",
                   "OK" if not missing_cmds else f"MISSING: {missing_cmds}"))
except Exception as e:
    checks.append(("CLI subcommands", f"FAIL: {e}"))

# skills on disk
import os
skill_files = [
    'skills/hy-event-classify/SKILL.md',
    'skills/hy-event-classify/references/taxonomy.md',
    'skills/hy-daily-brief/SKILL.md',
    'skills/hy-credit-scorecard/SKILL.md',
    'skills/hy-thesis-check/SKILL.md',
]
missing_skills = [f for f in skill_files if not os.path.isfile(f)]
checks.append(("SKILL.md files",
               "OK" if not missing_skills else f"MISSING: {missing_skills}"))

# data directories
data_dirs = ['data/db','data/raw','data/raw/edisclosure',
             'data/raw/nalog','data/factlists']
missing_dirs = [d for d in data_dirs if not os.path.isdir(d)]
checks.append(("data directories",
               "OK" if not missing_dirs else f"MISSING: {missing_dirs}"))

print("\n" + "="*55)
print("HYBOND FINAL ASSEMBLY CHECK")
print("="*55)
all_ok = True
for label, result in checks:
    icon = "✓" if result.startswith("OK") else ("⚠" if result.startswith("WARN") else "✗")
    print(f"  {icon}  {label:<35} {result}")
    if not result.startswith("OK"):
        all_ok = False
print("="*55)
print("READY TO RUN" if all_ok else "FIX THE ITEMS ABOVE BEFORE RUNNING")
print("="*55 + "\n")
EOF
```

This must print `READY TO RUN` before you proceed to the operational memo
(`CLAUDE_CODE_MEMO.md`).

---

## What comes next

Once this memo prints `READY TO RUN` and all 37 tests pass, switch to
`CLAUDE_CODE_MEMO.md` and follow from Step 4 (connectivity check) onward.
Steps 1–3 of that memo (folder creation, venv, test suite) are already done.
