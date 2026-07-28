---
name: cbr-rate-analysis
description: Analyse a CBR monetary policy meeting's documents to extract rate path implications for the HY bond portfolio. Use when a new CBR meeting document is collected, when the user asks about the rate outlook, or for quarterly portfolio rebalancing. Reads the cbr_meeting table for the most recent meeting plus prior history. Outputs structured rate scenario updates for the portfolio optimizer.
---

# cbr-rate-analysis

Turn a CBR key-rate meeting's raw documents (press release, Nabiullina's
speech, and — anchor meetings only — the mid-term forecast and commentary)
into a structured rate-scenario update. Layer 2 interpretive skill: the
documents and rate figures are already fetched deterministically by
`src/hybond/cbr_tracker.py` and stored in the `cbr_meeting` table. This skill
reads and interprets them — it does not fetch, does not invent a rate figure
that isn't in the stored text, and does not guess at a forecast range that
CBR hasn't itself published for that meeting.

## Inputs (from `cbr_meeting` — do not fetch)

Call `db.recent_cbr_meetings(con, limit=3)` for the three most recent rows
with a non-null `press_release_text` (older meetings without it haven't been
fetched yet and can't be analysed). Read, per meeting:

- `rate_before` / `rate_after` / `rate_change_bp` — the deterministic
  decision, already parsed from the press release's headline sentence.
- `press_release_text` — the full press release. This is where forward
  guidance language and the CBR's own characterization of inflation/economic
  conditions live.
- `nabiullina_speech_text` — the press-conference speech/Q&A transcript.
  Usually far more informative than the press release for tone: it's where
  hedging language, risk balance, and answers to "will you cut again"
  actually appear.
- `resume_text` — резюме обсуждения, published ~2 weeks after the meeting.
  If present for the target meeting, it's the richest source: it shows the
  range of views on the Board, not just the consensus decision. If null,
  the meeting is too recent for it to exist yet — say so, don't wait for it.
- `rate_2026_mid`, `rate_2027_mid`, `rate_neutral`, `inflation_forecast_2026`
  — only populated for anchor meetings (`is_anchor=1`). Non-anchor meetings
  will have these as NULL; carry forward the values from the most recent
  anchor meeting instead of leaving the scenario fields empty, and say
  explicitly that they're carried forward, not newly stated.
- `forecast_summary` / `comment_text` — anchor-only detail behind the
  headline ranges above.

**Raw-extraction caveat to respect**: `rate_2026_mid` etc. are lightly-parsed
strings straight from PDF text extraction (see `cbr_tracker.py`'s module
docstring) — a trailing digit can occasionally be a fused footnote marker,
not real precision (e.g. an observed "14,5–14,61" where the source PDF's own
footnote shows the range is "14,5–14,6"). If a range's last digit looks like
it doesn't belong (odd precision, a "1" or other stray digit isolated at the
end), verify against `forecast_summary` before quoting it, and note the
correction rather than passing the artifact through.

## Method

1. **Extract the current meeting's signal.** From `press_release_text` and
   `nabiullina_speech_text`: the actual decision (already in `rate_after`/
   `rate_change_bp`), the stated reasoning for it (which factors CBR cites:
   inflation, credit growth, labour market, external conditions, fiscal
   policy), any explicit forward guidance ("требуется дальнейшее снижение",
   "видим пространство для...", "не готовы предопределять"), and how CBR
   characterizes inflation risk (проинфляционные / сбалансированные /
   дезинфляционные факторы риска — this phrase is usually explicit in the
   press release's risk-balance paragraph).

2. **Compare to the prior meeting** (the second-most-recent row). Did the
   language get more or less hawkish? Specific things to check: did the step
   size change (50бп → 25бп is a deceleration signal even though both are
   cuts); did the risk-balance phrase shift; did forecast ranges narrow,
   widen, or shift up/down between anchor meetings; did new risk factors get
   named that weren't mentioned before (a specific sector, a fiscal policy
   change, an external shock).

3. **Classify tone** as `hawkish` / `neutral` / `dovish` for the current
   meeting standalone, AND `tone_vs_prior` as one of `more_hawkish` /
   `slightly_hawkish` / `unchanged` / `slightly_dovish` / `more_dovish`. Base
   this on the actual language compared, not on the rate decision alone — a
   cut delivered with hawkish guidance ("cutting now but signalling this is
   likely the last one for a while") is a different signal than the same
   cut with dovish guidance ("further significant cuts warranted").

4. **Translate into portfolio-relevant scenario deltas.** These three
   `implied_delta_rate_*_annual` fields are the analyst's judgment call on
   how much further the average annual key rate could move over the next
   ~12 months under base/bull/stress conditions, given everything read in
   steps 1-3 — not a mechanical formula. Ground the judgment in the
   forecast ranges (`rate_2026_mid`/`rate_2027_mid`/`rate_neutral`) as the
   outer bounds of plausibility: base should sit inside the CBR's own
   forecast path, bull should be more dovish than CBR's base case but not
   beyond what the forecast's OWN range already allows for, stress should
   reflect a hawkish reversal or pause, not an arbitrary shock.

5. **Write `portfolio_implication`** as one or two concrete sentences about
   duration/instrument-type positioning — not a restatement of the rate
   decision. E.g. "продолжающийся, но замедляющийся цикл снижения делает
   умеренную дюрацию (1.5-2.5 года) на фиксированном купоне предпочтительнее
   флоатеров". This is the field the portfolio optimizer's rate scenario
   actually consumes for allocation logic, so it must say something a
   duration decision can be made from.

## Output

Return exactly this JSON shape (store it verbatim into
`cbr_meeting.analysis_json` for the analysed `meeting_date` via
`db.upsert_cbr_meeting(con, meeting_date=..., analysis_json=json.dumps(...))`):

```json
{
  "meeting_date": "2026-07-24",
  "rate_current": 14.0,
  "rate_forecast_2026_range": [13.5, 14.5],
  "rate_forecast_2027_range": [8.0, 10.0],
  "rate_neutral": [7.5, 8.5],
  "tone": "neutral",
  "tone_vs_prior": "slightly_dovish",
  "key_signals": [
    "Шаг снижения замедлился до 25бп — цикл продолжается, но осторожно",
    "Инфляционные риски по-прежнему проинфляционные — не готовы ускоряться"
  ],
  "implied_delta_rate_base_annual": -0.027,
  "implied_delta_rate_bull_annual": -0.04,
  "implied_delta_rate_stress_annual": 0.013,
  "portfolio_implication": "Сохранять дюрацию 1.5-2.5 года. Флоатеры не приоритет — цикл снижения продолжается. Фиксированный купон с умеренной дюрацией оптимален."
}
```

`rate_forecast_2026_range` / `rate_forecast_2027_range` / `rate_neutral` are
`[low, high]` floats parsed from the stored `rate_2026_mid` / `rate_2027_mid`
/ `rate_neutral` range strings (e.g. `"13,5–14,5"` → `[13.5, 14.5]`) — carried
forward from the most recent anchor meeting if the analysed meeting itself is
non-anchor (say so in `key_signals` when that happens).

## What this skill does NOT do

- Does not fetch documents — if `press_release_text` is null for the target
  meeting, that meeting hasn't been collected yet; run `hybond cbr --fetch
  <date>` first (or wait for the cron job), don't fabricate a reading from
  nothing.
- Does not recompute the rate decision — `rate_before`/`rate_after`/
  `rate_change_bp` are already correct from `cbr_tracker.py`'s deterministic
  regex parse of the press release headline; use them as given.
- Does not treat the illustrative numbers in this document as real CBR
  output — every figure above is drawn from a single worked example, not a
  fixed lookup table. Read the actual stored text for the actual meeting
  being analysed.
