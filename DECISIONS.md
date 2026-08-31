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

## 2026-08-29 — Universe health scan architecture

Universe-only bonds (190 of 303 vdo_scope=1 ВДО bonds) had no
health_status because scan-watchlist-health only computes it for
watchlist_tier=1 issuers (34 issuers).

Investigated during implementation: universe_snapshot.in_watchlist=1
is NOT the same set as "has a real computed health_status" -- it's
set from watchlist_tier IN (1, 2), but issuer.health_status is only
ever written by scan-watchlist-health, which scans tier=1 only. Of
the 113 in_watchlist=1 vdo_scope bonds, 68 belong to tier=2 issuers
whose issuer.health_status has never been computed by anything --
it's just the schema default 'green'. Copying that as health_source=
'full' would stamp an uncomputed default as if it were a real signal.

Fix: two-tier health computation keyed on watchlist_tier, not
in_watchlist:
- Tier=1 issuers' bonds: health_source='full', copied from
  issuer.health_status (real full-signal computation -- YTM +
  G-spread trajectory + filing events, just run by
  scan-watchlist-health immediately before this step)
- Everything else (universe-only AND tier=2 in_watchlist bonds):
  health_source='quantitative', computed from YTM + G-spread
  thresholds only (watchlist_health.compute_universe_bond_health())

Thresholds for quantitative (more lenient than full -- no event
confirmation, no trajectory, level only):
RED: YTM>150% or G-spread>3000bp
ORANGE: YTM in (60%, 150%] or G-spread in (2000, 3000bp]
YELLOW: YTM in (30%, 60%] or G-spread in (1000, 2000bp]
GREEN: otherwise

Also: hourly `hybond backfill-ratings` has no bare form -- requires
--agency, only 'nkr' supported today. Wired as
`backfill-ratings --agency nkr` into daily_run.sh, between `hybond run`
and `hybond universe`. `scan-universe-health` itself is wired AFTER
scan-watchlist-health (not immediately after `hybond universe`), since
tier=1 bonds' health_source='full' depends on that step's freshly
computed issuer.health_status.

Verified against the live DB (2026-08-29): 303/303 vdo_scope=1 bonds
got a health_status, 0 skipped (null YTM/G-spread) -- 87 green, 141
yellow, 35 orange, 40 red. 45 bonds got health_source='full' (exactly
the tier=1-owned in_watchlist bonds), matching issuer.health_status
exactly for all 45. The other 258 (190 universe-only + 68 tier=2)
got 'quantitative'.

This unlocks: preset portfolios from full universe, screener health
filter/column on all 303 ВДО bonds, map color-by-health full coverage.

## 2026-08-29 — MOEX defaults: Монополия and ЕвроТранс confirmed defaults

MOEX defaults import revealed:
- Монополия (транспорт) 7810766685, tier=2, inactive: 41 confirmed
  Дефолт events Dec 2025-Aug 2026. Health manually set to RED.
  Promotion permanently blocked by audit dimension 7
  (promotion_audit.moex_confirmed_default()).
- ЕвроТранс 5029169023, tier=2, inactive: 2 confirmed Дефолт events
  Jul 2026. Health manually set to RED.

Both were showing health_status='green' (schema default) despite
confirmed defaults. NOT because scan-watchlist-health skips inactive
issuers -- it doesn't check `active` at all. The actual cause: it
only scans watchlist_tier=1 (db.list_issuers_by_tier(con, 1) in
scan_watchlist()); both issuers are tier=2, so neither was ever in
scope regardless of active status -- the same tier=1-only gap the
Universe Health Scan task (2026-08-29, earlier this session)
addressed for universe_snapshot.health_status. Manual correction
applied via direct SQL as a stopgap for these two.

Lesson: any issuer outside tier=1 needs its health status set
explicitly (at demotion/deactivation time, or via a tier-agnostic
scan) -- the schema default 'green' is silently wrong for a
tier=2/inactive issuer with a real, disclosed default on record.

Verified in live DB (2026-08-29):
  7810766685|Монополия (транспорт)|2|0|red|Подтверждённый дефолт в реестре MOEX — 41 событий (Dec 2025 – Aug 2026)
  5029169023|ЕвроТранс|2|0|red|Подтверждённый дефолт в реестре MOEX — 2 события (Jul 2026)

## 2026-08-29 — YTP (yield-to-offer) not built — already in MOEX data

MOEX's own YIELD field (stored as universe_snapshot.ytm) is already
yield-to-offer for bonds where offer_date < mat_date -- confirmed
empirically for RU000A10E6U4 (Самолёт) and RU000A10FJ65 (Роделен):
ytm=28.88%/28.57% respectively, both computed to their offer dates
(2027-08-02 / 2027-12-22), not their later maturities (2029-01-18 /
2031-05-31). See universe_pipeline.py's existing yield_horizon comment
(~line 299) for the underlying mechanism.

Correction: universe_snapshot.yield_to_offer itself is NOT populated
from this -- it is a separate column, hardcoded to NULL at
universe_pipeline.py:401 ("not derivable without per-bond cashflows"),
still NULL for all 1,247 rows in the latest snapshot including both
bonds above. The offer-aware figure lives in `ytm`/`YIELD`, not in the
`yield_to_offer` column -- the two must not be conflated.

What's missing is yield-to-final-maturity for offer bonds -- a
different, less commonly needed figure. Building a competing YTP
estimate (synthetic, flat-coupon, no amortization -- bondmath.py has
no cashflow builder that works without real per-bond MOEX bondization
data) would sit next to a YTM that, for near-offer bonds, is already
the same thing -- likely confusing, not complementary.

Decision: YTP task closed, not built. yield_to_offer column remains
NULL/unpopulated by design (Tier 2 backlog, if ever revisited: either
(a) populate yield_to_offer by relabeling/copying the existing
offer-aware `ytm` value for bonds where yield_horizon uses the offer
date, so the dashboard can show "YTM (к оферте)" honestly instead of
leaving the column empty, or (b) compute real yield-to-final-maturity
using per-bond bondization data, the actually-missing figure).

## 2026-08-31 — Conservative preset gap: real-data limitation

Conservative preset requires 20+ bonds but only 14 issuers pass its
filters against today's real DB. Two separate issues:

1. Real-data shortfall: watchlist has 34 active tier=1 issuers but
   only 14 pass conservative's rating/health/AEY criteria today.
   Fix: watchlist expansion (batch onboarding Tier 2 backlog item).

2. Cap-enforcement convergence bug in build_preset() equal-weight
   sizing: the trim loop can converge at exactly the minimum bond
   count with an unsatisfiable per-issuer/sector overage.
   Fix: redesign cap enforcement loop -- separate backlog item.

Balanced and aggressive presets work correctly end-to-end.
