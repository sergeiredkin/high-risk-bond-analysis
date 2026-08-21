# HY Bond Intelligence Platform — PLAN.md

Persistent project context. Russian high-yield (ВДО) corporate bond credit
intelligence, as an extension of the personal blog/newsletter platform.

Mission (single objective, everything serves this):

> **Detect changes in issuer credit quality earlier than the market, and
> explain why the risk changed.**

Not a screener. Not "more data." A continuously updated, explainable opinion
about issuer credit quality, plus a validated record of whether those opinions
were right.

---

## Design principle: invert where the LLM spends

The deterministic layer runs with **zero LLM**. Every LLM token is spent on
judgment a model is uniquely good at (reading a filing, deciding "does this
change default risk"), never on arithmetic or fetching. A pipeline that pays a
model to compute a spread is burning tokens on something Python does for free
and does correctly.

```
  Layer 1 — DETERMINISTIC CORE (Python, zero LLM)
    fetch -> normalize -> compute -> store -> diff
              |
              v  emits a structured "factlist" per issuer/bond/day
  =========== THE BOUNDARY (the factlist is the contract) ===========
              |
              v  reads factlists, never recomputes them
  Layer 2 — INTERPRETIVE SKILLS (Claude Code, LLM)
    event classification . scorecard narrative . thesis check
              |
              v
  Layer 3 — PUBLISHING SKILLS (LLM, thin)
    blog post . Telegram brief . maturity-wall writeup
```

The factlist (versioned JSON) is the boundary. It lets Layer 1 be unit-tested
in isolation and lets the skills stay cheap. Same pattern as the MOEX suite:
a validated Python module produces facts; SKILL.md files interpret them.

---

## Data sources (verified July 2026)

| Data | Source | Cost | Notes |
|---|---|---|---|
| Bond master, coupon/amort schedule | MOEX ISS | Free | JSON API, no contract |
| EOD price / yield / volume | MOEX ISS | Free | 15-min delay; thin for ВДО |
| Risk-free G-curve, key rate, FX | CBR | Free | Official, machine-readable |
| Material facts, filings, financials | e-disclosure.ru | Free | HTML/PDF, no API, parse cost |
| Current ratings (ACRA, Expert RA) | agency sites | Free (scrape) | No free historical feed |
| YTM, G-spread, duration, pillars | **computed** | Free | our core product |
| Structured cross-issuer history | Cbonds | ~$350–1000+/mo | only once scale is the bottleneck |
| RUSBONDS | — | **closed to retail since Jun 2025** | not an option |

Everything in the six-pillar model is **calculated, not purchased**. Cbonds is
deferred until a full manual cycle proves coverage/history is the real
bottleneck — automating fields before we know which we use is the overengineering
trap.

---

## Layer 1 — deterministic core (modules)

- **ingest/** — MOEX ISS + CBR. Pure HTTP+JSON. Snapshot-first: cache raw
  responses to `data/raw/` *before* parsing, so a parse bug is replayable.
- **compute/** — YTM, G-spread vs interpolated CBR curve, modified duration,
  convexity, time-to-maturity, coupon cash-coverage. Pure functions of stored
  inputs. Unit-tested. No LLM ever touches this.
- **store/** — SQLite, one snapshot per day, append-only. Tables: `bond`,
  `issuer`, `price_snapshot`, `spread_snapshot`, `financials`, `event`,
  `thesis`.
- **diff/** — the delta engine. Compares today vs prior snapshot per issuer:
  spread move > threshold, volume spike, new filing, maturity crossing.
  Produces the candidate list. **This is what decides which issuers get an LLM
  call today** — the single biggest lever on token spend.
- **factlist/** — assembles per-issuer JSON the skills consume. Versioned schema.
- **calendar/** — aggregates coupon/redemption dates -> maturity wall. Pure
  calculation, no financials, no LLM. First shippable output.

---

## Layer 2 — interpretive skills (triggered only on diff-flagged issuers)

Each reads the factlist and interprets; **never fetches, never computes**. If a
skill finds itself doing math, a Layer-1 function is missing.

- **hy-event-classify** — filing text + factlist -> structured event (category,
  severity, affected pillar, direction, confidence, one-line "so what").
- **hy-credit-scorecard** — six-pillar raw inputs -> narrative around the pillars
  (scores computed in Layer 1; skill explains them).
- **hy-thesis-check** — stored thesis + new events -> strengthening / neutral /
  weakening / broken, with triggering evidence. The compounding feature.

---

## Layer 3 — publishing skills (thin)

- **hy-daily-brief**, **hy-issuer-writeup**, **hy-maturity-wall-post** — consume
  already-interpreted output, format for blog/Telegram. Near-mechanical, cheap,
  reuse the same structured data.

---

## Return-maximizing rules (once running)

1. **The diff gate is the money.** Never call a skill on an unchanged issuer.
2. **Cache interpretations by input hash.** Re-runs and backfills become free.
3. **Batch the day's interpretive calls** into one pass where possible.
4. **Let the deterministic layer pre-filter severity** so skills start from
   "something material happened."
5. **Skills read facts, never raw sources.** No paying twice; no divergent answers.
6. **Escalate model size by stake.** Small model tags a coupon notice; best model
   judges a thesis break on a live position.

---

## Build order (slices)

1. **Slice 1 (no LLM):** ingest + compute + store + calendar -> spread screen +
   maturity wall. Publishable. Proves the data path end to end. ← *this repo now*
2. **Slice 2 (no LLM):** diff engine + factlist assembler on 15–25 hand-picked
   issuers. Change-detection + skill contract, still zero tokens.
3. **Slice 3 (first LLM):** hy-event-classify + hy-daily-brief, diff-gated.
4. **Slice 4:** hy-credit-scorecard (after hand-entering financials) +
   hy-thesis-check.
5. **Slice 5:** remaining publishing skills; re-evaluate Cbonds.

Slices 1–2 are the "prove it by hand" stage: no LLM cost, real output. The LLM
enters at slice 3, by which point the diff gate keeps spend proportional to
actual change.

---

## Slice 2 detector policy (change-detection gate)

Three detectors compute every run; all three LOG to `flag_log`, one DRIVES
attention. This exists because the statistical detectors are blind cold — a
z-score needs ~20 snapshots of history before it means anything — so we cannot
"pick the best in a week." Instead:

- **fixed** — absolute thresholds (|ΔG-spread| ≥ 50bp, volume-to-zero,
  new filing, maturity window). Works day one. Over-flags volatile names.
  DRIVES attention weeks 1–4.
- **stat** — z-score of the change vs the issuer's own rolling history.
  Self-calibrating; silent until ~8+ change-observations exist. Logs only.
- **hybrid** — fires only when a fixed floor (25bp) AND an unusual move (≥2σ)
  both hold. Almost certainly the endpoint. Inherits stat's cold-start. Logs only.

Around week 4–6, run `hybond grade`: mark whether each flag was followed by
something that mattered, and compare detectors on ONE shared history. This is a
DIRECTIONAL read (few real events on 15–25 names), not a precision statistic —
it will clearly show if `fixed` over-flags the volatile names; it will not give
a clean precision number. Then flip `--driving` to the winner and tune.

`grade` reports precision *with its denominator* so the thinness is always visible.

## Slice 3 — first LLM (interpretive + publishing skills)

The diff gate (Slice 2) decides which issuers get a filing pulled and classified,
so token spend stays proportional to real change. Two skills, both SKILL.md-first
(mirrors the MOEX stock suite: skills interpret a validated factlist):

- **hy-event-classify** (Layer 2): reads the factlist + a filing's text, emits a
  structured credit event (JSON for the `event` table + a Russian note for
  publishing). Authoritative logic lives in `references/taxonomy.md`, a
  Russian-HY-failure-mode taxonomy that is the main quality lever — tune it
  without touching skill logic.
- **hy-daily-brief** (Layer 3, thin): turns classified events + diff flags into a
  publishable Russian brief, severity-ordered, `noise`/`routine` excluded.

### Scraper scope decision (message feed ONLY)

e-disclosure.ru is scraped from day one (user preference), but ONLY the
material-fact **message feed** — the event stream that feeds the classifier.
Financial-statement PDFs are deliberately OUT of Slice 3 scope: practitioners
confirm they are scanned, OCR-dependent, and unregulated in layout. Those are a
Slice 4 problem. Keeping scope to the message feed is what makes day-one
automation realistic.

Access reality (verified): company.aspx pages are JS/cookie-gated and need a
headless browser (`--selenium`); plain HTTP is tried first and raises a clear
error on the gate. Parser and fetcher are separated so parsing is unit-tested
against a saved HTML fixture with no network dependency.

### Cost shape

LLM tokens are the *small* cost (diff-gated, a few filings/day). The real
variable is data acquisition, and the filing text is free (mandated disclosure) —
so automation spends the cheap thing (tokens) on genuine judgment and pays
nothing for data. Classification automation (`skills/classify_api.py`) is a
documented STUB, OFF by design: invoke the SKILL.md manually and spot-check it,
then wire the API. Model policy is already defined (cheap model routine, strong
model for already-flagged issuers).

## Slice 4 — six-pillar financials + thesis tracker

### The ГИР БО breakthrough (no OCR needed)

The plan feared Slice 4 as "the hard OCR slice" — financials trapped in scanned,
unregulated-layout e-disclosure PDFs. There is a way around it: **ГИР БО /
bo.nalog.gov.ru**, the Federal Tax Service's open financial-statements resource.
Every Russian entity files РСБУ there; it's published as machine-readable JSON
keyed by standard Минфин 66н line codes (1410 debt, 2110 revenue, 2200 EBIT,
2330 interest, 1250 cash, 4100 operating CF, ...). So financials are a
{code: value} lookup by ИНН — not an OCR problem.

This collapses the hard part: the six pillars move BACK into Layer 1
(deterministic `compute/pillars.py`), where the architecture wants the scores —
auditable, unit-tested against hand-checked arithmetic. The skill only writes
narrative over them.

Access: `ingest/nalog.py` with a parser-api.com backend (documented, stable
JSON, **200 free requests/month** — covers a ~25-issuer annual watchlist many
times over) and a documented direct-bo.nalog stub. Snapshot-first as always.

Known limitation, surfaced not hidden: ГИР БО holds single-entity РСБУ. For a
holding-company issuer whose business sits in subsidiaries, single-entity debt
can be understated (the cross-guarantee problem). The record carries the source's
`has_ks` flag; when consolidated statements exist, the pillars payload adds a
`coverage_note` warning, and the scorecard skill must surface it. IFRS
consolidated (still in the PDFs) is an optional later enhancement for the few
holding-company names where single-entity РСБУ genuinely misleads — not a
prerequisite.

### Skills

- **hy-credit-scorecard** (Layer 2): narrates the computed pillar scores +
  factlist context. Governance pillar comes from the event stream, market pillar
  from the spread — NOT from РСБУ. Never invents a figure; honours the
  coverage_note.
- **hy-thesis-check** (Layer 2, the compounding feature): checks a stored thesis
  (assumptions + pre-committed invalidation criteria) against new events and
  current pillars → strengthening / holding / weakening / broken, with the exact
  triggering evidence. Records outcomes → validated track record over cycles.

The factlist's `six_pillars`, `events_since_last`, and `thesis_status`
placeholders (reserved back in Slice 2) are now populated.

## Status

- [x] Slice 1: ingest / compute / store / calendar
- [x] Slice 2: diff engine (3 detectors) + factlist + grader stub
- [x] Slice 3: e-disclosure scraper (message feed) + hy-event-classify +
      taxonomy + hy-daily-brief + API stub
- [x] Slice 4: ГИР БО financials client (parser-api + direct stub) +
      deterministic six-pillar compute + hy-credit-scorecard + hy-thesis-check;
      factlist placeholders populated; 473 tests pass
- [x] Bugfixes: `db.connect()` call-time DB_PATH; factlist tolerates
      pre-Slice-3/4 missing tables
- [x] Live connectivity in real env (`hybond selftest`; daily cron running on
      MOEX/CBR/e-disclosure/ГИР БО since real deployment)
- [x] Run the full pipeline on a real watchlist; accumulate history (34
      tier=1 active + 48 tier=2 passive-monitoring issuers as of 2026-08-21;
      1,937 flag_log rows accumulated)
- [x] Classify automation enabled: local Ollama (qwen3.5) auto-classify
      pipeline, tier-priority queue, burst/pause mode, hourly cron +
      classification-aware dashboard rebuild every 30 min
- [ ] `hybond grade` → pick detector: 1,937 flag_log rows accumulated but
      `--driving` still defaults to `fixed` (src/hybond/__main__.py) --
      no evidence the grade output has actually been reviewed and a
      detector chosen yet
- [ ] Optional later: IFRS consolidated for holding-company issuers where
      single-entity РСБУ misleads (the one remaining OCR-ish task)

## The platform, end to end

```
MOEX ISS + CBR ──► compute (YTM/G-spread/duration) ──► snapshots ─┐
ГИР БО (ФНС) ────► compute (six pillars) ─────────────────────────┤
e-disclosure ────► scrape (event feed) ──► hy-event-classify ─────┤
                                                                   ▼
                              diff engine (3 detectors) ──► factlist
                                                                   │
                                    ┌──────────────────────────────┤
                                    ▼              ▼               ▼
                         hy-credit-scorecard  hy-thesis-check  hy-daily-brief
                                    │              │               │
                                    └──────► blog / Telegram ◄──────┘
```

## Environment note

MOEX ISS (`iss.moex.com`) and CBR (`cbr.ru`) must be reachable. The sandbox this
was scaffolded in allows only package registries, so live pulls were not run here
— run `python -m hybond.selftest` in your environment to confirm the endpoints
before the first real ingest. Offline fixtures in `tests/fixtures/` make the
compute layer fully testable without network.
