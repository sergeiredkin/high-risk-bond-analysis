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
