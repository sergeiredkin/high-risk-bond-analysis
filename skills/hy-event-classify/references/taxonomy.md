# Russian HY (ВДО) Event Taxonomy

The classifier maps each material-fact filing to exactly one primary `category`,
a `severity`, the `affected_pillar` it bears on, and a `direction` (does it make
default MORE or LESS likely). This taxonomy is tuned to the *failure modes of
Russian high-yield issuers*, not to generic corporate news — that tuning is the
single biggest quality lever in Slice 3.

Severity scale: `critical` > `high` > `medium` > `low` > `noise`.
Direction: `worse` (raises default/restructuring risk) | `better` | `neutral`.
Pillars (from PLAN.md six-pillar model): liquidity, leverage, refinancing,
operations, governance, market.

## Categories (Russian HY failure-mode–first)

### reporting_delay — задержка/непредоставление отчётности
Late or missing РСБУ/МСФО statements, missed disclosure deadlines.
- Severity: **high** by default; **critical** if repeated or unexplained.
- Pillar: governance (and a liquidity read — issuers hide bad numbers).
- Direction: worse.
- Why it matters most: in Russian HY, a reporting delay is one of the strongest
  early precursors to restructuring. Post-2022 many issuers *legally* redact, so
  a delay is less anomalous than before — weight by whether THIS issuer usually
  reports on time.

### coupon_event — купонные события
Coupon rate set/reset (for floaters), coupon payment made, **coupon missed**,
technical default on a coupon.
- Severity: `noise` for a routine payment made; **critical** for a miss or
  technical default; **high** for an unexpected downward coupon reset.
- Pillar: liquidity, refinancing.
- Direction: worse for miss/technical default; neutral for a routine payment.

### offer_put_call — оферта / put / call / амортизация
Announcement of a put offer (оферта), call, or amortization schedule change.
- Severity: **high** — a put concentrates refinancing need into one date; a
  called bond removes a name; amortization changes the maturity ladder.
- Pillar: refinancing.
- Direction: context-dependent — a large upcoming put the issuer may not fund is
  `worse`; a clean call is `neutral`/`better`.

### covenant — ковенанты
Covenant breach, waiver, amendment, or cross-default trigger.
- Severity: **critical** for a breach or cross-default; **high** for a waiver
  (signals stress even when granted).
- Pillar: leverage, governance.
- Direction: worse.

### rating_action — рейтинговые действия
ACRA / Expert RA / NKR / NRA rating change, outlook change, or withdrawal.
- Severity: **high** for a downgrade or negative outlook; **critical** for a
  multi-notch cut or withdrawal; `low`/`better` for an upgrade.
- Pillar: market, governance.
- Direction: by direction of the action. A **withdrawal** is `worse` — issuers
  often drop coverage rather than take a downgrade.

### governance — корпоративное управление
CEO/CFO/board change, auditor change/resignation, related-party transactions,
change of ultimate beneficial owner, litigation against the issuer.
- Severity: **high** for auditor resignation or a sudden CFO exit; **medium**
  for an ordinary board rotation; **critical** for major litigation or an
  opaque related-party transfer of assets.
- Pillar: governance.
- Direction: usually worse; a change can be `neutral` if orderly and disclosed.

### insolvency — банкротство / реструктуризация
Bankruptcy filing (own or by a creditor), restructuring proposal, moratorium,
asset seizure, or enforcement.
- Severity: **critical**, always.
- Pillar: liquidity, refinancing.
- Direction: worse.

### operations — операционные события
Production halt, loss of a major customer/contract, asset sale, major capex
commitment, guidance change, sanctions naming of the issuer or a key counterparty.
- Severity: **high** for a sanctions naming or loss of a key customer; **medium**
  for a guidance cut; `low`/`better` for a favourable contract win.
- Pillar: operations.
- Direction: by content.

### capital_action — действия с капиталом
New bond issue, bank facility drawn/undrawn, equity injection, dividend
declaration/change.
- Severity: **medium**; a large **dividend** while leveraged is **high** and
  `worse` (cash leaving a stressed issuer); a new facility secured is `better`.
- Pillar: refinancing, liquidity.
- Direction: by content — new funding `better`; dividends out `worse`.

### routine — регуляторная рутина
Scheduled disclosures with no credit content: affiliated-parties lists, AGM
notices, standard quarterly filings that merely confirm expectations.
- Severity: **noise**.
- Pillar: none.
- Direction: neutral.
- Purpose: an explicit bucket so routine filings are *recorded* but never
  surface as signal — this is what stops the feed from drowning real events.

## Classification rules

1. **One primary category.** If a filing spans two (e.g. a reporting delay that
   also mentions a covenant waiver), pick the higher-severity one as primary and
   note the second in `also`.
2. **Severity is about credit impact, not tone.** A calmly-worded auditor
   resignation is still `high`.
3. **Direction is default-risk direction.** "worse" always means more likely to
   default/restructure, regardless of what it does to the bond price short-term.
4. **When unsure between `noise`/`routine` and a real category, do NOT inflate.**
   A false `critical` that cries wolf costs more trust than a missed `medium`.
   But a genuine `reporting_delay` or `covenant` breach must never be downgraded
   to routine.
5. **Confidence reflects filing clarity**, not your prior on the issuer. A clear
   coupon-miss filing is high confidence even on a "good" issuer.

## Output contract

The classifier returns JSON matching this shape (one object per filing):

```json
{
  "category": "reporting_delay",
  "also": null,
  "severity": "high",
  "affected_pillar": "governance",
  "direction": "worse",
  "confidence": 0.0,
  "so_what": "one sentence: what this changes about default risk",
  "note_ru": "одно-два предложения для брифа/блога"
}
```

`confidence` in [0,1]. `so_what` is English for the internal event record;
`note_ru` is the publishable Russian line for the daily brief.
