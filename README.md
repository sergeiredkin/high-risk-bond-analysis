# hybond

Credit-analysis platform for Russian high-yield (ВДО) corporate bonds:
deterministic Python pipeline + SQLite store + a static HTML dashboard, with
a few Claude Code skills for the LLM-assisted steps (event classification,
credit scorecards, thesis tracking). See `PLAN.md` for the original
three-layer design; this README reflects what's actually built.

No LLM in the compute path. Prices, spreads, duration, default probabilities,
G-spread mispricing, and the AEY scenario layer are all pure functions of
MOEX/CBR data and are unit-tested. LLM involvement is opt-in and confined to
the Slice 3 skills (event classification, briefs, scorecards, thesis checks).

## Layout

```
src/hybond/
  ingest/sources.py      MOEX ISS + CBR ZCYC clients, snapshot-first caching
  ingest/nalog.py         ГИР БО (bo.nalog.gov.ru) financials client
  compute/bondmath.py     YTM, G-spread, duration, convexity (pure, tested)
  compute/cashflows.py    MOEX coupon/amortization schedule -> (t, amount) pairs
  compute/default_probs.py  Эксперт РА default-probability table + fair-spread math
  compute/pillars.py      six-pillar financial health computation
  store/db.py             SQLite schema + access layer, append-only daily snapshots
  calendar/wall.py        maturity wall / refinancing calendar
  pipeline.py             Slice 1: watchlist ingest -> compute -> store -> screen + wall
  universe_pipeline.py    full MOEX corporate-bond universe screen + G-spread +
                          mispricing + AEY (not just the watchlist)
  portfolio.py            rate-scenario loader (base/stress/bull) + per-bond AEY
                          and PD-weighted risk-adjusted AEY
  cbr_tracker.py          CBR key-rate meeting tracker: fetch, parse, diff
  diff/engine.py          Slice 2: change-detection (fixed/stat/hybrid detectors)
  diff_pipeline.py        Slice 2 CLI glue
  factlist/assemble.py    per-issuer factlist assembly (the boundary object skills read)
  scrape/edisclosure.py   e-disclosure (AZIPI) material-fact filing scraper
  scrape/smartlab.py      smart-lab.ru bonds feed (secondary source)
  scrape_pipeline.py      Slice 3 CLI glue
  financials_pipeline.py Slice 4 CLI glue
  grade.py                Slice 2 detector grading/scorecard
  skills/classify_api.py  documented stub for automated classification (off by design)
  __main__.py             CLI entry point (see Commands below)

scripts/
  export_dashboard_data.py  aggregates DB + factlists + report.json -> data/dashboard_data.json
  build_dashboard.py        renders dashboard/template.html against that JSON
  daily_run.sh               cron entry point: full pipeline, 3x/day
  weekly_financials.sh       cron entry point: Slice 4 refresh
  seed_*.py, bulk_mark_routine.py, revert_board_decisions_routine.py  one-off/demo tooling

dashboard/
  template.html            main 4-tab dashboard (Скрининг / Универсум / Карта рынка / ЦБ РФ)
  market_map_template.html market-map tab source template
  index.html, market_map.html  built output

skills/                    Claude Code skills invoked manually at the LLM steps
  hy-event-classify, hy-daily-brief, hy-credit-scorecard, hy-thesis-check,
  cbr-rate-analysis

tests/                     unit tests, textbook-anchored where possible; offline fixtures
```

## Setup

```bash
cp watchlist.example.json watchlist.json   # fill in real ВДО issuers + MOEX SECIDs
PYTHONPATH=src python -m hybond selftest   # confirm MOEX/CBR endpoints are reachable
```

Run everything from the repo root with `src/` on the path (`PYTHONPATH=src`).

## Commands

```
hybond selftest                     confirm MOEX/CBR reachability
hybond run <watchlist.json>         Slice 1: prices, YTM, G-spread, maturity wall
hybond universe [--refresh-emitent-map]
                                     full MOEX universe screen: G-spread, mispricing,
                                     AEY scenarios, rating join (not just the watchlist)
hybond bonds <INN>                  live: all outstanding MOEX series for one issuer
hybond rate --inn --agency --rating [--outlook] [--action] [--date] | --list
                                     manual credit-rating entry (feeds default-probability layer)
hybond financials <watchlist.json> [--key ...]
                                     Slice 4: ГИР БО financials + six pillars
hybond scrape <watchlist.json> [--selenium] [--smartlab] [--no-bodies] [--debug]
                                     Slice 3: pull e-disclosure / smart-lab material-fact filings
hybond queue                        Slice 3: filings awaiting classification
hybond diff [--driving fixed|stat|hybrid] [--factlist-dir data/factlists]
                                     Slice 2: change detection + factlist assembly
hybond grade                        Slice 2: detector scorecard (run weeks later)
hybond cbr --fetch YYYY-MM-DD | --fetch-all | --list | --install-cron
           --diff YYYY-MM-DD | --diff-all
                                     CBR key-rate meeting tracker: fetch, list, diff
```

## Daily pipeline

`scripts/daily_run.sh` runs the full sequence (prices/spreads, universe
screen, CBR check, event scrape, diff/factlists) and is intended to run three
times a day via cron — see the script for the exact schedule and the
weekly `--refresh-emitent-map`/financials refresh logic. It must run with VPN
off (e-disclosure.azipi.ru requires a local IP).

## Dashboard

```bash
PYTHONPATH=src python scripts/export_dashboard_data.py   # -> data/dashboard_data.json
python scripts/build_dashboard.py                         # -> dashboard/index.html
```

Four tabs: **Скрининг** (watchlist spread screen + maturity wall + factlists),
**Универсум** (full MOEX universe screener), **Карта рынка** (scatter market
map with a time toggle and fair-value curve overlay), **ЦБ РФ** (key-rate
meeting timeline, rate-path chart, and the AEY rate-scenario panel).

## Default probability / mispricing layer

`compute/default_probs.py` holds Эксперт РА's theoretical default-probability
matrix and a flat 35% recovery-rate assumption. `implied_fair_spread_bp`
converts a rating into a fair G-spread; `mispricing_bp` is the gap between
that and the bond's actual G-spread (positive = cheap, negative = rich).
Bonds are excluded from the *signal* (not from the table) when the raw
comparison would be misleading — commodity-linked notes, thin near-maturity
prints, and MOEX's own degenerate `YIELD=0.0` reports — see
`universe_pipeline.py`'s `COMMODITY_LINKED`/`SHORT_TENOR_THIN`/
`ZERO_YIELD_DEGENERATE` and the `signal_excluded` column.

## AEY / portfolio scenario layer

`portfolio.get_rate_scenarios(con)` derives base/stress/bull annual rate-path
deltas live from the most recent CBR meeting with a completed analysis
(base = 2027 forecast midpoint, bull = 2029 forecast midpoint, stress = 2026
forecast high, each relative to the current key rate) and falls back to
conservative static defaults — visibly flagged via `is_fallback` — when no
such meeting is available. `bond_aey` applies the resulting delta to each
bond's modified duration (`ytm - modDur * delta_rate`); `pd_adjusted_aey`
further weights that by the bond's rating-implied default probability and
recovery rate, so a raw distress YTM doesn't get read as an attractive yield.
This is the single source both `universe_pipeline.py`'s per-bond AEY columns
and the dashboard's ЦБ tab scenario panel read from.

When ranking bonds by AEY, pair `aey_base_risk_adj` with a YTM sanity bound —
PD-weighting alone doesn't tame nominal YTMs in the hundreds/thousands of
percent, since those reflect distress pricing that already assumes
restructuring regardless of the formal default event.

## CBR key-rate tracker

`cbr_tracker.py` fetches and parses cbr.ru meeting materials (press release,
Nabiullina's speech, the quarterly Monetary Policy Report forecast table,
and the follow-up резюме), stores them in `cbr_meeting`, and computes a
deterministic meeting-over-meeting diff (`cbr_meeting_diff`): rate-pace
change, forecast-range shifts, tone shift, and a portfolio-duration signal.
`hybond cbr --install-cron` prints the crontab entries for same-day capture
around each scheduled 2026 meeting/résюме date.

## Slice 2 — change-detection gate

Three detectors compute every run; `fixed` drives attention early on,
`stat`/`hybrid` log silently until graded (see `PLAN.md`, "detector policy").
`diff` surfaces only the issuers the driving detector flagged, and assembles
a versioned factlist per surfaced issuer — the boundary object the Slice 3
skills read without recomputing. Grading is semi-manual:
`grade.grade_flag(con, flag_id, followed_by, is_true)` records outcomes;
`hybond grade` shows the scorecard with its sample size.

## Slice 3 — event stream + first LLM skills

Scrapes the e-disclosure message feed (material-fact events, not financial
PDFs); `company.aspx` is JS-gated so `--selenium` needs selenium +
chromedriver, plus each watchlist issuer's `edisclosure_id`. Classify queued
filings by invoking the **hy-event-classify** skill in Claude Code — it reads
the issuer factlist + filing text against the taxonomy in
`skills/hy-event-classify/references/taxonomy.md`. **hy-daily-brief** formats
the day's classified events + diff flags into a publishable roundup.
`skills/classify_api.py` is a documented, intentionally-disabled automation
stub — invoke the skill manually and spot-check before wiring the API client.

## Slice 4 — six-pillar financials

Financials come from ГИР БО (bo.nalog.gov.ru, via parser-api.com, 200 free
requests/month) as structured JSON keyed by ИНН — no OCR. The six pillars are
computed deterministically; **hy-credit-scorecard** narrates them, and
**hy-thesis-check** records/checks an investment thesis against new events.
ГИР БО holds single-entity РСБУ (annual, lagging) — for holding-company
issuers the `has_consolidated` flag warns that group debt may be understated.

## Tests

```bash
PYTHONPATH=src python -m pytest tests/ -q
```
