---
name: hy-credit-scorecard
description: Write the credit-health scorecard narrative for a Russian high-yield (ВДО) bond issuer from its computed six-pillar scores. Use this whenever the user asks for an issuer's credit scorecard, credit-health assessment, "разбор эмитента", six-pillar breakdown, or a written explanation of why an issuer's credit quality is strong/weak/deteriorating. Consumes the deterministic pillar SCORES already computed in Layer 1 (compute/pillars.py from ГИР БО financials) plus the issuer factlist — it explains and contextualizes the numbers, it does NOT recompute ratios or invent financial figures. Always ground every statement in the provided pillar metrics; flag data-coverage gaps (e.g. single-entity РСБУ for a holding company) rather than papering over them.
---

# hy-credit-scorecard

Write the analyst narrative around an issuer's six-pillar credit scores. Layer 2
interpretive skill: the SCORES and ratios are already computed deterministically
in `compute/pillars.py` from ГИР БО (ФНС) financials and live in the factlist's
`six_pillars`. This skill EXPLAINS them. It never recomputes a ratio and never
supplies a financial number that isn't in the inputs — if a figure is missing,
say so.

## Inputs (all from the factlist — do not fetch)

- `six_pillars`: the computed metrics and 0-100 sub-scores for leverage,
  liquidity, refinancing, operations, plus the raw `inputs` (revenue, EBIT,
  debt, cash, etc.), a `coverage_note`, and — for leasing issuers — a
  `sector_note`.
- `bonds`: current spread + delta, so the market's view sits next to the
  fundamentals.
- `events_since_last`: recent classified events (Slice 3) — these inform the
  GOVERNANCE pillar, which is NOT computed from financials.
- `flags`, `thesis_status`.

## Method

1. **Governance and market pillars come from events + spread, not РСБУ.** The
   financial statements give you leverage/liquidity/refinancing/operations. For
   governance, read `events_since_last` (a reporting delay, auditor change, or
   covenant event is the governance signal). For market, use the spread and its
   delta. Do not try to score governance from the balance sheet.

2. **Lead with what the scores say, then why.** "Leverage is the weak pillar
   (score 40): debt/EBIT of 5.2x and interest coverage of only 1.8x." Quote the
   metric that drives each sub-score.

3. **Read the pillars together, not in isolation.** Weak liquidity + high
   short-term-debt share + a recent reporting-delay event is a coherent
   deterioration story; say so. A single weak pillar on an otherwise strong
   issuer is different.

4. **Honour the coverage_note.** If it flags missing debt lines or the issuer
   `has_consolidated` statements you didn't use (single-entity РСБУ for a holding
   company), state the limitation plainly — the leverage read may understate
   group debt. This is the cross-guarantee caveat; never hide it.

5. **Leasing issuers: check `sector_note` before citing leverage/refinancing.**
   If `sector_note` is present and contains "лизинговая компания", РСБУ code
   2330 (interest expense) is zero because a leasing company folds interest
   into cost-of-leasing rather than booking it as a separate line — that makes
   `debt_to_ebit` and `interest_coverage` computed from that same zero and
   read as misleadingly clean. Do NOT cite `debt_to_ebit` or
   `interest_coverage` as pillar metrics for these issuers. Use `ocf_to_debt`
   as the primary leverage signal and `short_term_debt_share` as the primary
   refinancing signal instead.

6. **Overall trend, stated as improving / stable / deteriorating**, justified by
   the pillars and recent events — not a made-up composite number.

## Output

- A short JSON summary: `{overall_trend, weakest_pillar, strongest_pillar,
  key_risk, data_caveat}`.
- Then prose: a pillar-by-pillar read (2-3 sentences each for the pillars that
  matter, briefer for the rest), ending with the trend and what would change it.
- A publishable Russian paragraph (`note_ru`) suitable for an "issuer of the
  week" style blog section.

## Guardrails

- **Never invent a financial figure.** Every number you cite must be in the
  `six_pillars.inputs` or the derived metrics. Missing → say "не раскрыто".
- **Never recompute.** If a ratio looks wrong, flag it for a Layer 1 check;
  don't silently recompute it in prose.
- **No buy/sell advice.** Assess credit quality; do not recommend a trade.
- **РСБУ is annual and lagging.** Frame the scorecard as the slow baseline the
  event stream moves against, not a real-time view.
