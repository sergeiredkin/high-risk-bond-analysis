---
name: hy-thesis-check
description: Check a Russian high-yield (ВДО) bond investment thesis against new evidence and judge whether it is strengthening, holding, weakening, or broken. Use this whenever the user asks to check/update a bond thesis, whether an investment case still holds, "не сломался ли тезис", to record a new thesis for an issuer, or when new events arrive for an issuer that has a stored thesis. Compares the thesis's stated assumptions and invalidation criteria against the issuer's recent classified events and current pillar scores from the factlist. This is the disciplined-research feature that compounds over time — it turns the platform from a news reader into a decision-support system with a validated track record.
---

# hy-thesis-check

Judge a stored investment thesis against new evidence. Layer 2 interpretive
skill. It reads the thesis (assumptions + invalidation criteria) and the issuer
factlist (recent events, current pillars, spread), and returns a status with the
specific evidence that moved it. It never fetches and never recomputes — it
matches new facts against pre-committed criteria.

Why this is the compounding feature: every thesis check is recorded, so over
cycles you accumulate a validated history of which theses held and which broke,
and why. That feedback loop is the durable edge — harder to replicate than any
data feed because it depends on consistent, disciplined tracking.

## Two modes

### Record a thesis (when the user first analyses an issuer)
Capture, in the user's words, structured into the `thesis` table:
- **thesis**: the one-line case ("will refinance the 2027 maturity cleanly").
- **assumptions**: what must be true (e.g. "EBITDA stays > X", "retains bank
  lines", "no sanctions escalation").
- **invalidation**: the specific, pre-committed conditions that would BREAK it
  ("misses a coupon", "reporting delay > 30 days", "leverage > 6x",
  "loss of the anchor customer"). Insist on concrete, checkable criteria — a
  thesis with vague invalidation can't be tracked.

### Check a thesis (when new events arrive)
1. Read `events_since_last` and `six_pillars` from the factlist.
2. Test each new event and each current pillar against the stored assumptions
   and invalidation criteria.
3. Return a status:
   - **strengthening** — evidence confirms assumptions (e.g. new bank line secured).
   - **holding** — nothing material changed.
   - **weakening** — an assumption is under pressure but no invalidation tripped.
   - **broken** — a pre-committed invalidation criterion has been met.
4. Cite the exact triggering evidence. A `broken` verdict must name the specific
   invalidation criterion and the event/metric that met it — never a vibe.

## Method

- **Invalidation is binary and pre-committed.** If the thesis said "broken on a
  missed coupon" and a coupon-miss event arrives, it is `broken` — do not soften
  it because the issuer "probably will recover." The discipline is the point.
- **Weakening needs a named pressure**, not a general unease. Tie it to a
  specific assumption and the evidence pressuring it.
- **Respect confidence.** If the triggering event was low-confidence
  (from hy-event-classify), note that the status is provisional pending
  confirmation.
- **Never invent events or criteria.** Work only from the stored thesis and the
  factlist.

## Output

- JSON: `{status, triggering_evidence, criterion_met, confidence, provisional}`.
- Prose: 2-4 sentences explaining the verdict and what to watch next.
- On `broken` or `weakening`, a short Russian note for the brief/blog.

The status is written back to the `thesis` table so the factlist reflects it and
the outcome enters the validated history.

## Guardrails

- **No buy/sell advice.** Report thesis status; the user decides.
- **Do not re-litigate the thesis itself** — you check it against its own stated
  criteria, you don't substitute your own view of whether it was a good thesis.
- **A correct earlier `broken` is not reversed by hope.** If new positive news
  arrives after a break, that's a NEW thesis to record, not an un-breaking of the
  old one.
