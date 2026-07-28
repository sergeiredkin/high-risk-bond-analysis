# Feature: Default Probability Layer

**Status:** Planned — implement after Slice 1–4 are running on real data  
**Prerequisite:** Real G-spread data in `spread_snapshot` for at least one week  
**Scope:** Two files + two extra fields in the factlist. No new slice, no new CLI command.

---

## What this adds and why

The platform currently computes G-spread (bond yield minus CBR risk-free curve).
That tells you the market's *price* for an issuer's risk, but not whether that
price is fair.

This feature adds the missing half: a calibrated *baseline default probability*
per rating level, sourced from Эксперт РА's published historical default matrix
(2001–2025, 7560 rating observations). With both numbers you can compute:

```
implied_fair_spread_bp = default_prob_1yr × (1 − recovery_rate) × 10000
mispricing_bp = actual_g_spread_bp − implied_fair_spread_bp
```

Positive `mispricing_bp` → bond is wider (cheaper) than the rating implies.
Negative → bond is tighter (richer) than the rating implies.

This is the core relative-value signal institutional credit analysts use. It
turns the spread screen from a ranking (widest first) into an actionable
assessment (cheap vs fair vs rich vs distressed).

It also upgrades the thesis tracker: instead of qualitative invalidation only,
`hy-thesis-check` can reference a concrete probability shift when an event fires.

---

## Source data

**Эксперт РА theoretical default probabilities**, published April 2025,
covering 2001–2025. Use the *theoretical* table (logistic model), not the
*observed* frequencies. Reason: the observed data has rating-level anomalies
from small samples (e.g. ruBB− observed 6.89% > ruBB 5.06%, which is
theoretically backwards). The logistic model smooths these out and gives a
monotone relationship. Эксперт РА explicitly publishes both and recommends
the theoretical for forward-looking use.

The agency itself notes these frequencies are non-static and change with
macroeconomic conditions — the 2022–2026 default wave has elevated BB/B
frequencies. Treat these as calibrated priors, not precise loss estimates.
Re-check the published table semi-annually (Эксперт РА updates it every
six months): `raexpert.ru/about/disclosure/default-level-data/`

**Recovery rate assumption:** 35% for Russian ВДО (mid-point of 30–40%
practitioner range). Store as a named constant, not a magic number.
This assumption dominates the fair-spread calculation and should be
documented wherever it appears.

---

## Implementation

### File 1: `src/hybond/compute/default_probs.py`

Pure lookup module. No I/O, no network, no LLM. Contains:

1. `DEFAULT_PROBS` dict — the theoretical probability table from Эксперт РА,
   keyed by rating string (e.g. `"ruBB"`, `"ruBBB-"`), value is a dict with
   keys `yr1`, `yr2`, `yr3` (decimal, not percent).

2. `RECOVERY_RATE = 0.35` — named constant.

3. `lookup(rating: str) -> dict | None` — returns the probability dict for a
   rating string, handling common variants (e.g. `"BB"` → `"ruBB"`, case
   variants). Returns `None` if rating not found rather than raising — callers
   handle the miss gracefully.

4. `implied_fair_spread_bp(rating: str, horizon_yr: int = 1) -> float | None`
   — computes `default_prob × (1 − RECOVERY_RATE) × 10000`. Returns `None` if
   rating not found.

5. `mispricing_bp(actual_g_spread_bp: float, rating: str,
                  horizon_yr: int = 1) -> float | None`
   — actual minus implied. Positive = cheap, negative = rich.

The full table to encode (Эксперт РА theoretical, 2001–2025):

| Rating  | 1yr %  | 2yr %  | 3yr %  |
|---------|--------|--------|--------|
| ruAAA   | 0.143  | 0.562  | 0.989  |
| ruAA+   | 0.218  | 0.796  | 1.374  |
| ruAA    | 0.284  | 0.994  | 1.693  |
| ruAA-   | 0.371  | 1.240  | 2.083  |
| ruA+    | 0.484  | 1.545  | 2.561  |
| ruA     | 0.631  | 1.924  | 3.145  |
| ruA-    | 0.823  | 2.395  | 3.858  |
| ruBBB+  | 1.072  | 2.976  | 4.724  |
| ruBBB   | 1.396  | 3.694  | 5.772  |
| ruBBB-  | 1.815  | 4.577  | 7.037  |
| ruBB+   | 2.357  | 5.658  | 8.553  |
| ruBB    | 3.056  | 6.975  | 10.359 |
| ruBB-   | 3.954  | 8.572  | 12.494 |
| ruB+    | 5.102  | 10.493 | 14.996 |
| ruB     | 6.561  | 12.785 | 17.897 |
| ruB-    | 8.399  | 15.490 | 21.218 |
| ruCCC   | 13.524 | 22.273 | 29.137 |
| ruCC    | 25.834 | 35.905 | 43.681 |
| ruC     | 51.756 | 58.956 | 65.416 |

Store percentages as decimals in the dict (divide by 100). Example:
`"ruBB": {"yr1": 0.03056, "yr2": 0.06975, "yr3": 0.10359}`

---

### File 2: `tests/test_default_probs.py`

Unit tests (offline, no network). Must cover:

- `lookup("ruBB")` returns correct values matching the table above.
- `lookup("BB")` (without "ru" prefix) still works — normalisation.
- `lookup("bb")` (lowercase) still works — normalisation.
- `lookup("ruAAA")` returns values (not None).
- `lookup("ruXXX")` returns None without raising.
- `implied_fair_spread_bp("ruBB", 1)` ≈ 3.056% × 65% × 10000 ≈ 198bp.
  (use `abs(result - 198.6) < 1` as the assertion).
- `mispricing_bp(500, "ruBB", 1)` ≈ 500 − 198.6 ≈ 301bp (positive = cheap).
- `mispricing_bp(100, "ruBBB", 1)` is negative (rich at 100bp vs ~91bp fair).
- `implied_fair_spread_bp("ruXXX", 1)` returns None without raising.
- Monotone check: 1yr probability increases from ruAA to ruBB to ruB (simple
  assertion that the table is ordered correctly).

---

### Factlist change: `src/hybond/factlist/assemble.py`

Add three fields to the `Factlist` dataclass (alongside the existing reserved
placeholders):

```python
default_prob_1yr: float | None = None    # from lookup, decimal
implied_fair_spread_bp: float | None = None
mispricing_bp: float | None = None       # actual G-spread minus implied fair
```

In `build_factlist()`, after assembling `bonds`:

1. Get the issuer's current rating. Source: `six_pillars` if it carries a
   rating field, otherwise leave as None for now (rating ingestion is a
   separate future step — do not block the factlist on it).
2. If rating is available, call `default_probs.lookup(rating)` and
   `default_probs.implied_fair_spread_bp(rating)`.
3. For `mispricing_bp`: use the widest bond's current `g_spread_bp` from the
   `bonds` list as the representative spread (widest = most liquid signal in
   ВДО context where thinly traded series can be stale).
4. All three fields tolerate None gracefully — the factlist is valid without
   them. Skills handle None by noting "rating not yet stored."

---

### Watchlist change: `watchlist.json`

Add a `rating` field to each issuer entry. The scraper and pipeline do not
currently ingest ratings automatically — the watchlist is the manual source
for now. Example:

```json
"9705101614": {
  "name": "ЛК Европлан",
  "sector": "leasing",
  "risk_tier": "anchor",
  "rating": "ruAA-",
  ...
}
```

Use the Эксперт РА or АКРА notation consistently (`ruXX` prefix).
This field feeds directly into the default probability lookup.

---

### Spread screen upgrade: `src/hybond/pipeline.py`

In the spread screen output rows, add two fields alongside the existing
`g_spread_bp`:

```python
"implied_fair_spread_bp": round(implied, 0) if implied else None,
"mispricing_bp": round(misprice, 0) if misprice else None,
```

Source: call `default_probs.implied_fair_spread_bp(rating)` using the rating
from the watchlist entry for that issuer.

The spread screen sorted by `g_spread_bp` (widest first) stays as-is. A
secondary sort by `mispricing_bp` (most undervalued first) is useful but
optional for v1.

---

## What to NOT build yet

- Automated rating ingestion (scraping АКРА/Эксперт РА pages). Manual
  watchlist field is sufficient until the rest of the platform is stable.
- Multi-horizon spread analysis (1yr vs 3yr comparison). Single horizon (1yr)
  is the standard for short-duration ВДО.
- Recovery rate by sector. A flat 35% is the right starting point. Per-sector
  differentiation (e.g. secured lenders like CarMoney might be 50-55%) is a
  calibration step after first results.
- Probability-weighted loss calculation. That requires position sizes, which
  are not in the current data model.

---

## Verification after implementation

1. Run `PYTHONPATH=src:tests python -m pytest tests/test_default_probs.py -v`
   — all tests pass offline.
2. Run `python -m hybond run watchlist.json --out data/report.json` with at
   least one issuer that has a `rating` field in watchlist.json.
3. Check `data/report.json` spread screen rows — confirm `implied_fair_spread_bp`
   and `mispricing_bp` are populated for rated issuers and None for unrated.
4. Run `python -m hybond diff --factlist-dir data/factlists` and open a
   factlist JSON — confirm `default_prob_1yr`, `implied_fair_spread_bp`,
   `mispricing_bp` appear as top-level fields.

---

## Source reference

Эксперт РА, «Матрица дефолтов как инструмент оценки кредитного риска»,
апрель 2025. URL: `raexpert.ru/press/articles/pohvalenskaya_apr2025/`

Updated semi-annually. Next update expected ~H2 2026. When updating the table,
also update the docstring in `default_probs.py` with the new as-of date.
The АКРА equivalent is published at `acra-ratings.ru/compliance/` and covers
the same rating levels — cross-check if values diverge significantly.
