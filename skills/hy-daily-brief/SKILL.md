---
name: hy-daily-brief
description: Generate the daily Russian high-yield (ВДО) bond credit brief from the hybond platform's classified events and diff flags. Use this whenever the user asks for the daily brief, the HY bond brief, "прогони бриф по ВДО", a Telegram/blog post summarizing today's credit changes, or a roundup of which issuers deteriorated or improved today. Consumes already-interpreted data (classified events from hy-event-classify plus the diff engine's attention list) and formats it for publishing — it does NOT re-analyze filings or recompute anything. Leads with what changed and why it matters for default risk, ordered by severity.
---

# hy-daily-brief

Turn today's classified events and diff flags into a publishable brief. This is a
Layer 3 publishing skill: it is deliberately THIN. All the analysis already
happened upstream (Layer 1 computed the spreads, the diff engine decided what
changed, hy-event-classify interpreted the filings). This skill only selects,
orders, and phrases. It must not re-open filings or recompute metrics.

## Inputs

- **Today's events**: the classified events for the day (category, severity,
  pillar, direction, `note_ru`, confidence, issuer).
- **The diff attention list**: issuers the driving detector surfaced, with the
  spread deltas from their factlists.
- Optionally, the maturity-wall calendar if a maturity is imminent this week.

## Method

1. **Order by severity, then direction.** `critical` and `high` `worse` events
   lead. Improvements and `better` events come after. `noise`/`routine` are
   excluded from the brief entirely (they stay in the database, not the post).

2. **One issuer, one entry**, even if it had several filings — combine into the
   single most important line, using the highest-severity event as the headline
   and mentioning the rest briefly.

3. **Lead each entry with the credit change, not the news.** "Default risk up:
   Эмитент X delayed its annual report for the second time" — not "Эмитент X
   published a filing." Use the event's `note_ru` as the basis; tighten it.

4. **Include the number when it's the point.** If the diff surfaced the issuer on
   a spread move, state it ("+120 б.п. за день") — that's from the factlist, not
   a recomputation.

5. **Flag imminent maturities** only if within the coming week and material.

6. **Confidence honesty.** If the strongest event of the day is low-confidence,
   say so plainly ("предварительно, до подтверждения") rather than overstating.

## Output

A short Russian-language brief suitable for Telegram and the blog:

- A one-line date header.
- A "Рост риска" (risk up) section: highest-severity deteriorations first.
- A "Снижение риска / нейтрально" (risk down / neutral) section, if any.
- An optional "Ближайшие погашения" (upcoming maturities) line if relevant.
- Keep it tight — a reader should grasp the day's credit picture in under a
  minute. This is a brief, not a report; no headers-heavy structure, no filler.

End with a single honest caveat line noting the brief is model-assisted and
issuers should be verified against primary disclosure before acting.

## Guardrails

- **Publish only what was classified.** Do not add issuers or events not in the
  input. Do not speculate about causes the events don't state.
- **No investment advice.** Describe credit-risk changes; do not say buy/sell.
- **Do not restate `noise`/`routine`** — their exclusion is the feature that
  keeps the brief signal-dense.
- If a day has no material events, say so in one line rather than padding.
