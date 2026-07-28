---
name: hy-event-classify
description: Classify a Russian high-yield (ВДО) bond issuer's disclosure filing into a structured credit event. Use this whenever the hybond diff engine surfaces an issuer with a new e-disclosure filing, whenever the user pastes or points to a material-fact filing (существенный факт) from a Russian bond issuer and asks what it means for credit risk, or whenever processing the unclassified-filing queue. Turns a raw filing plus the issuer's factlist into a JSON credit event (category, severity, affected pillar, default-risk direction, confidence) plus a short Russian note for publishing. Always classify against the Russian-HY event taxonomy, never as a generic summary. Do NOT invent facts not in the filing; when the filing is ambiguous, lower confidence rather than guessing.
---

# hy-event-classify

Classify one Russian HY bond filing into a structured credit event. This is a
Layer 2 interpretive skill: it READS the Slice 2 factlist and the filing text,
and it INTERPRETS. It never fetches, never recomputes spreads or metrics — those
already live in the factlist. If you find yourself computing a number, a
Layer 1 function is missing; stop and flag it rather than estimating.

## Inputs

1. **The issuer factlist** (JSON from `hybond diff --factlist-dir ...`): current
   spread + delta, flags, sector, issuer name. Gives you the credit context the
   filing lands in — a reporting delay on an issuer whose spread already blew out
   50bp reads differently than one on a calm name.
2. **The filing**: title + body text (from the scraper, or pasted by the user).
3. **The source** (check `hybond queue`'s `source` field, or the filing's
   `edisclosure_id`): `primary` means a Bank-of-Russia-authorized disclosure
   agent (АЗИПИ). `smartlab` means smart-lab.ru — community news/commentary,
   occasionally an issuer's own IR account, but never the primary filing
   itself. Treat `smartlab` filings as SECONDARY: cap `confidence` lower than
   you would for an equivalent primary filing (the underlying material fact
   hasn't been directly read, only reported on), and prefix `note_ru` with
   "по данным Smart-Lab" so the daily brief never presents secondary-source
   news as if it were the primary disclosure.

## Method

1. **Read the taxonomy** in `references/taxonomy.md`. It defines the categories,
   severities, pillars, and the classification rules. This is authoritative —
   the taxonomy is tuned to Russian HY failure modes and is the whole point of
   the skill. Do not classify from general intuition; classify against it.

2. **Identify the primary category.** One filing → one primary category (the
   higher-severity one if it spans two; note the second in `also`). Use the
   `routine` bucket deliberately for filings with no credit content — recording
   them as noise is correct, inflating them is not.

3. **Assign severity by credit impact, not tone.** A calmly-worded auditor
   resignation is still `high`. A routine coupon payment made is `noise`.

4. **Set direction as default-risk direction.** `worse` = more likely to
   default/restructure, regardless of short-term price effect.

5. **Weight by issuer context from the factlist.** Post-2022 many issuers
   legally delay reporting, so a `reporting_delay` is less anomalous than it once
   was — check whether THIS issuer usually reports on time (if the factlist or
   prior events tell you). Never downgrade a genuine covenant breach or coupon
   miss, however common delays have become.

6. **Confidence reflects filing clarity, not your prior.** A clear coupon-miss
   filing is high-confidence even on a "good" issuer. An ambiguous, hedged
   filing is low-confidence even on a scary one.

## Output

Return JSON exactly matching the contract in `references/taxonomy.md`:

```json
{
  "category": "...", "also": null, "severity": "...",
  "affected_pillar": "...", "direction": "worse|better|neutral",
  "confidence": 0.0,
  "so_what": "one English sentence: what this changes about default risk",
  "note_ru": "одно-два предложения на русском для брифа/блога"
}
```

Then, in prose below the JSON, give a 2–4 sentence explanation of the call for a
human — what in the filing drove the category and severity, and what to watch
next. This prose is for the analyst; the JSON is what gets stored in the `event`
table and the `note_ru` is what the daily brief publishes.

## Guardrails

- **Never invent facts.** If the filing doesn't state an amount, a date, or a
  cause, say so and lower confidence. Do not supply a plausible number.
- **Never upgrade to `critical` to be safe.** A false alarm costs trust; the
  taxonomy's rule 4 is explicit that crying wolf is worse than a missed
  `medium`. But never downgrade a real `reporting_delay`, `covenant`, or
  `coupon` miss to `routine`.
- **One filing, one event.** If the user gives several, classify each separately.
- **If the filing text is missing or truncated**, do not classify from the title
  alone unless the title itself is unambiguous (e.g. "Сообщение о неисполнении
  обязательств по выплате купона"). Otherwise ask for the body.

## When automated (Python API path, later)

For cron use, the identical prompt (this SKILL.md body + taxonomy) runs via the
Anthropic API against each unclassified filing. Use a small/cheap model for
routine tagging and escalate to a stronger model only when the factlist shows the
issuer is already flagged (a filing on an already-stressed name deserves the
better model). See `src/hybond/skills/classify_api.py` for the documented hook —
it is a stub until you turn automation on.
