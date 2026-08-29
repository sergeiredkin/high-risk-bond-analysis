# hybond — Decision Log

## 2026-08-27 — G-spread distress gate in build_preset()

**Decision:** Do NOT add a G-spread threshold gate to `build_preset()`.

**Reasoning:** Real tier=1 watchlist bonds have G-spreads of 2800–6000bp
on normal days (Моторные технологии 6024bp ORANGE, Л-Старт 4448–5592bp
YELLOW, Сегежа 4016bp GREEN). A 1000bp gate — initially proposed as
"safe" — would incorrectly exclude most of the legitimate watchlist.

**Root cause of mistake:** G-spread thresholds calibrated for IG/investment
grade bonds (where >300bp = distress) do not apply to ВДО. Russian
high-yield operates at structurally higher spreads — 500–2000bp is normal,
2000–4000bp is elevated but not necessarily distressed.

**Correct approach:** Health status filter IS the distress gate:
- Conservative: GREEN/YELLOW only
- Balanced: GREEN/YELLOW/ORANGE
- Aggressive: all statuses with explicit RED criteria (rating ≥ ruBB-, PD <3%)

**Lesson:** Always verify thresholds against real watchlist data before
implementing filters. Run the G-spread distribution query first.

**Data reference (2026-08-27, tier=1 active issuers):**
- Max legitimate G-spread: 6024bp (Моторные технологии, ORANGE)
- Min distress threshold: no fixed number — context-dependent
- АПРИ corrupted row: ytm=0.0, g_spread_bp=-1525 (pipeline bug,
  not real market price — investigate separately)

---

## 2026-08-27 — Portfolio bond source: watchlist not universe

**Decision:** `build_preset()` must source bonds from `issuer_bond` JOIN
`issuer` (watchlist) not `universe_snapshot` (full MOEX universe).

**Root cause of mistake:** Initial spec said "pull from universe_snapshot"
without considering that universe contains 2004 bonds from 375 issuers
including defaulted/distressed names never reviewed by hybond.

**Consequence caught:** АПРИ (13 bonds, G-spread 1500–3000bp, technical
default on RU000A10BM56) was eligible for conservative portfolio inclusion.

**Lesson:** Portfolio construction must always be gated on human-reviewed
issuers (watchlist) not automated universe data.

---

## 2026-08-27 — ВДО duration distribution

**Finding:** 187 watchlist bonds, almost all under 2 years modified duration.
Only 6 bonds above 2.0yr. Longest: Селигдар gold/silver-linked at 2.58–3.01yr.

**Lesson:** Duration filter UI should use 0–3yr range, not 0–5yr.
ВДО market convention is short-duration high-coupon paper.
Bonds >3yr in ВДО universe are structural exceptions (commodity-linked,
special purpose), not typical ВДО.

---

## 2026-08-27 — Селигдар commodity-linked bonds

**Decision:** Exclude SELSILV01, SELGOLD002, SELGOLD003 from
`build_preset()` auto-selection via `COMMODITY_LINKED_BOND_IDS`.

**Reasoning:** Gold/silver-indexed instruments with YTM 1.9–8% are not
comparable to standard ВДО yield. Including them in AEY-weighted presets
distorts calculations.

**Scope:** Exclusion is build_preset() only. Bonds remain visible in
screener, market map, and watchlist. User can manually add if desired.

**Lesson:** Structural exclusions need explicit lists, not heuristic
filters — commodity-linked bonds look like normal bonds in most fields.

---

## 2026-08-27 — Backtest status and honest positioning

Current positioning: "institutional-grade methodology, retail-first
in RU, one month live operation, backtest pending."

History starts 2026-07-28. Meaningful backtest possible by 2027-02
(6 months). Until then: anecdotal validation only (Оил Ресурс,
Контрол Лизинг, АПРИ catches). Forward tracking: log every
promotion/rejection with reason via existing flag_log + promotion
audit trail. Review outcomes at 6 and 12 months.

## 2026-08-27 — CSS token correction: --text-accent does not exist

hybond's codebase does not have a --text-accent token. The correct
accent token is --gold-primary (rebranded to teal in today's session
but the token name is unchanged). Never substitute --text-accent —
it silently breaks hover states with no error.

## 2026-08-27 — six_pillars are normalized scores, not raw financials

six_pillars stores 0–100 normalized pillar scores, not raw financial
figures (Выручка, EBITDA, Net debt, ND/EBITDA etc.). Raw multi-year
financials are not currently exported to dashboard_data.json.
Financial trajectory table in bond drawer uses pillar scores labeled
honestly as .../100. To show raw figures, ГИР БО data must be added
to export_dashboard_data.py first.

## 2026-08-27 — udActiveTab() false positive: #sidebarHomeBtn stale .active

#sidebarHomeBtn retains .active class from initial load that
gotoTab() never clears. Querying .sidebar-item.active matches it
first and returns wrong tab context. Fix: always use
.sidebar-item.active[data-tab] to exclude the home button.

## 2026-08-27 — yield_to_offer not yet computed

universe_snapshot has a yield_to_offer column and offer_date is written
by universe_pipeline.py (line 383, populated when MOEX reports an
offer/put date). Confirmed: universe_pipeline.py line 387 sets
yield_to_offer=None explicitly, with an existing comment "not derivable
without per-bond cashflows" — this is a deliberate placeholder, not an
oversight. export_dashboard_data.py (line 566) exports offer_date but
not yield_to_offer (there is nothing to export).

Backlog Tier 2: add ytm_to_offer() in bondmath.py using offer_date
as terminal date, call in universe_pipeline.py when offer_date is
not null and offer_date < mat_date, surface in unified bond drawer
metrics strip.

## 2026-08-27 — CBR tracker silent abort bug

cbr_tracker.check_and_fetch_today() was missing `result =` before
fetch_meeting(...), causing NameError on every actual CBR scheduled
day. Since daily_run.sh runs under set -e, this aborted the entire
daily pipeline including dashboard rebuild. Discovered during health
logging Task 2 wiring. Fixed and regression test added.

## 2026-08-27 — Live DB safety rule

Never use file-level cp/restore against data/db/hybond.sqlite with
concurrent cron writers active. Correct pattern for test isolation:
sqlite3 data/db/hybond.sqlite ".backup /tmp/hybond_test.sqlite"
Uses SQLite online backup API — safe with concurrent writers.
Or accept small real writes (health log rows) directly to live DB.

## 2026-08-29 — AZIPI scraper hanging on body fetch

hourly_filings.sh ran `hybond scrape --smartlab` without `--no-bodies`,
so the scraper fetched full filing body HTML for every new filing on
every issuer every hour. Body fetch requests are not covered by the
same per-request timeout guard as the feed/listing requests, so a
hanging body URL could exhaust the 300s total-runtime budget
(scrape_pipeline.py MAX_RUNTIME_S) before all issuers were processed.

Fix: added --no-bodies to hourly cron. Bodies are not needed for
dedup or classification (title+url sufficient); on-demand body fetch
for the UI can be added later if needed.

Also added AZIPI_TIMEOUT health logging (log_health_once_today) when
the AZIPI homepage feed fetch itself hits a Timeout/ConnectionError,
so a down/unreachable AZIPI is visible in pipeline_health instead of
only in the per-run log file.

Verified (2026-08-29): `hybond scrape --smartlab --no-bodies` against
the live DB completed in 1m43s, 0 errors, timed_out=false, 0 new
filings this run. Confirms the 300s-timeout/0-issuers-processed
failure mode is fixed, though the run did not complete in <60s as
originally targeted -- with --no-bodies removed as a variable, the
remaining runtime is dominated by per-issuer secondary (ИНН search)
requests for issuers with nothing in today's homepage feed.

Considered and skipped: a bespoke `PRAGMA busy_timeout=2000` inside
scrape_pipeline.py. Every connection there already goes through
db.connect(), which sets busy_timeout=5000 centrally (store/db.py) --
adding a second, shorter value locally would silently override that
in one file instead of adjusting the one shared setting.
