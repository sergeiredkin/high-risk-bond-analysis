"""Rating-tier yield thresholds, replacing the ad-hoc CS_MAX_YIELD_PCT=35%
cap with a PD-aware version -- for RATED bonds only (see module docstring
below for the unrated fallback, which the flat cap still is).

Two distinct numbers per rating tier, not one -- see the finding written up
for Task A ("PD-adjusted expected-value inflection curve") before this
module existed:

1. `floor_ytm` (was requested as "break_even_ytm"): the aey_base at which
   portfolio.pd_adjusted_aey() crosses zero, i.e. PD*(1-RECOVERY_RATE)/(1-PD).
   pd_adjusted_aey is STRICTLY INCREASING in aey (slope = 1-PD > 0, always
   positive) for a fixed rating tier -- so this is a FLOOR (minimum yield
   needed to compensate the tier's default risk), not a ceiling. Yields
   BELOW floor_ytm have negative expected value; above it, positive. This
   floor is tiny for investment/upper-HY grades (~2.7% for ruBB-, ~10.2% for
   ruCCC) because it only has to cover the tier's own small expected default
   loss -- real ВДО market yields sit far above it for every tier except the
   worst two or three, so on its own it almost never fires and cannot serve
   as a general-purpose ceiling (nominal yield can rise without bound and
   pd_adjusted_aey rises right along with it -- exactly the blind spot
   portfolio.pd_adjusted_aey()'s own docstring already flags).

2. `ceiling_spread_bp` / `caution_spread_bp`: the actual rating-aware
   CEILING, built differently -- from how far the bond's real market spread
   has decoupled from what the rating implies is fair
   (default_probs.implied_fair_spread_bp), not from pd_adjusted_aey's zero
   crossing. A bond trading at several multiples of its rating's implied
   fair spread is the ЭффТех1Р2 / Альфа Дон Транс pattern: the market is
   pricing far more risk than the rating reflects, meaning the rating itself
   is stale, lagging, or about to be revised -- not that the yield is
   "attractively high."

   The multiple is NOT a fixed constant (an earlier version of this module
   tried CEILING_SPREAD_MULTIPLE=4.0 -- see git history / the Task-A
   write-up): checked against the live universe_snapshot on 2026-08-10, the
   MEDIAN rated ВДО bond already traded at ~7.5x its rating's theoretical
   fair spread (p75 ~12.7x, p90 ~18.6x, p95 ~23.8x) -- the Эксперт РА
   default-only theoretical model is calibrated far tighter than actual
   current market spreads (elevated-CBR-rate / illiquidity / distress-era
   premium the whole segment carries, not evidence each individual rating is
   wrong). A flat multiplier picked from theory alone can't tell "this
   bond's rating looks stale" from "the whole asset class trades wide of the
   model right now" -- there is no historical calibration data (no "which of
   these later got downgraded" dataset) to derive a principled fixed cutoff.

   Instead the multiple is derived from THIS RUN's own live distribution
   (see `ceiling_multiples_from_distribution` below): the ceiling sits at
   the P90 of (g_spread_bp / implied_fair_spread_bp) across all currently-
   rated, non-excluded universe bonds, caution at P75. Self-calibrating as
   market-wide conditions shift instead of assuming today's regime is
   permanent -- it flags bonds unusually decoupled RELATIVE TO THEIR PEERS
   today, not relative to a static theoretical constant. `universe_pipeline.
   run_universe()` computes this once per run and applies it uniformly.
"""
from __future__ import annotations

import statistics

from . import default_probs

# Percentile cutoffs against the live (g_spread_bp / implied_fair_spread_bp)
# distribution -- see module docstring for why this replaced a flat
# multiplier. Same documented-assumption status as default_probs.
# RECOVERY_RATE: a chosen cutoff, not a derived one (no dataset exists to
# calibrate "how many percentile points signals a stale rating" against).
CEILING_PERCENTILE = 90
CAUTION_PERCENTILE = 75

# Fallback multiples used only when a live distribution isn't available
# (e.g. a caller classifying a single bond outside a full universe run, or a
# sample too small to take a percentile from) -- picked near the 2026-08-10
# P90/P75 check above, not re-derived automatically, so THIS constant can
# drift out of date exactly the way a flat multiplier always could. Prefer
# ceiling_multiples_from_distribution() whenever a live sample exists.
FALLBACK_CEILING_MULTIPLE = 18.0
FALLBACK_CAUTION_MULTIPLE = 12.5

# Minimum sample size before trusting a percentile over the fallback --
# below this a handful of outlier bonds could swing P90/P75 wildly.
MIN_DISTRIBUTION_SAMPLE = 30


def ceiling_multiples_from_distribution(
    ratios: list[float], ceiling_pct: int = CEILING_PERCENTILE,
    caution_pct: int = CAUTION_PERCENTILE,
) -> tuple[float, float]:
    """(ceiling_multiple, caution_multiple) derived from a live sample of
    (g_spread_bp / implied_fair_spread_bp) ratios -- P90/P75 by default.
    Falls back to FALLBACK_CEILING_MULTIPLE/FALLBACK_CAUTION_MULTIPLE when
    the sample is too small (see MIN_DISTRIBUTION_SAMPLE) or empty, rather
    than computing a percentile off a handful of points."""
    clean = [r for r in ratios if r is not None and r > 0]
    if len(clean) < MIN_DISTRIBUTION_SAMPLE:
        return FALLBACK_CEILING_MULTIPLE, FALLBACK_CAUTION_MULTIPLE
    q = statistics.quantiles(clean, n=100, method="inclusive")
    return q[ceiling_pct - 1], q[caution_pct - 1]


def floor_ytm(rating: str) -> float | None:
    """aey_base at which pd_adjusted_aey(aey_base, rating) == 0. Below this,
    negative expected value at the tier's own PD; above it, positive. A
    FLOOR, not a ceiling -- see module docstring. None if rating unknown."""
    probs = default_probs.lookup(rating)
    if probs is None:
        return None
    pd = probs["yr1"]
    return pd * default_probs.LGD / (1 - pd)


def ceiling_spread_bp(rating: str, multiple: float = FALLBACK_CEILING_MULTIPLE) -> float | None:
    """Market g_spread_bp above which the bond has decoupled from what its
    rating implies is fair -- the real per-tier ceiling. `multiple` should
    come from ceiling_multiples_from_distribution() against a live universe
    sample; the default here is only the small-sample fallback. None if
    rating unknown."""
    fair = default_probs.implied_fair_spread_bp(rating)
    if fair is None:
        return None
    return fair * multiple


def caution_spread_bp(rating: str, multiple: float = FALLBACK_CAUTION_MULTIPLE) -> float | None:
    fair = default_probs.implied_fair_spread_bp(rating)
    if fair is None:
        return None
    return fair * multiple


def yield_inflection_by_rating(
    ceiling_multiple: float = FALLBACK_CEILING_MULTIPLE,
    caution_multiple: float = FALLBACK_CAUTION_MULTIPLE,
) -> dict[str, dict]:
    """Precomputed floor/ceiling numbers for every rating tier in
    default_probs.DEFAULT_PROBS -- pure arithmetic off the existing PD
    table plus the given multiples (pass live-distribution-derived ones from
    ceiling_multiples_from_distribution() for a real run; the defaults here
    are only the small-sample fallback). No network calls, no DB. Keyed by
    normalised rating string (e.g. "ruBB-")."""
    out = {}
    for rating in default_probs.DEFAULT_PROBS:
        out[rating] = {
            "floor_ytm": floor_ytm(rating),
            "implied_fair_spread_bp": default_probs.implied_fair_spread_bp(rating),
            "ceiling_spread_bp": ceiling_spread_bp(rating, ceiling_multiple),
            "caution_spread_bp": caution_spread_bp(rating, caution_multiple),
        }
    return out


# Fallback for unrated bonds -- there is no PD to compute a principled
# threshold from, so the flat cap this module otherwise replaces stays as an
# honest, explicitly-labeled exception rather than silently applying a
# rating-tier number to a bond with no rating. Same value/rationale as
# company_scan_pipeline.CS_MAX_YIELD_PCT (kept in sync by hand, same
# duplication discipline as that module's own constants).
UNRATED_MAX_YIELD_PCT = 35.0

# Absolute sanity backstop for RATED bonds, alongside the ratio-based
# ceiling above. Found necessary against live data: a rating's own
# implied_fair_spread_bp scales up with its PD, so for the worst tiers
# (ruCCC/CC/C, already double-digit-percent PD) an absurd absolute yield can
# still look statistically unremarkable AS A RATIO to that tier's own
# already-huge fair spread -- checked live, ЭффТех1Р2 (ruC, 273% YTM, this
# module's own motivating example) clears the P90 ratio ceiling comfortably
# (~7.7x vs ruC's own median-ish ratio) while still being exactly the
# restructuring-priced case this module exists to catch. Same blind spot
# portfolio.pd_adjusted_aey()'s docstring already documents ("does NOT by
# itself tame an already-absurd nominal YTM ... pair with a YTM sanity
# cap") -- this constant is that pairing, extended to rated bonds instead of
# only the unrated fallback. Calibrated above real legitimate high-B/low-BB
# ВДО yields (checked: АНТЕРРА, ruB-, 78% aey_base, correctly stays green)
# and below genuine distress pricing (checked: ЭффТех1Р2, ruC, 275%).
RATED_ABSOLUTE_MAX_AEY_PCT = 120.0


def classify_yield_zone(aey_base: float | None, g_spread_bp: float | None,
                         rating: str | None,
                         ceiling_multiple: float = FALLBACK_CEILING_MULTIPLE,
                         caution_multiple: float = FALLBACK_CAUTION_MULTIPLE) -> str:
    """One of 'green' / 'caution' / 'negative_ev' / 'unrated'. Used both for
    the universe_snapshot.yield_zone column (where universe_pipeline.
    run_universe() passes live-distribution-derived multiples) and directly
    by callers that already have the inputs in hand (which get the
    small-sample fallback multiples unless they pass their own).

    A bond with no aey_base at all (e.g. signal_excluded -- see
    universe_pipeline.py's COMMODITY_LINKED/SHORT_TENOR_THIN/etc. gates)
    can't be classified regardless of whether it has a rating, so it maps to
    'unrated' too -- that label means "not classifiable", not literally
    "no rating string present".

    Unrated bonds (or unclassifiable ones) fall back to the flat
    UNRATED_MAX_YIELD_PCT cap on aey_base (there is no rating tier to
    compute a principled floor/ceiling from) -- 'negative_ev' if over the
    cap, 'unrated' otherwise (distinct from 'green' so a caller can tell
    "no principled check was possible" from "checked and clean")."""
    if aey_base is None or not rating:
        if aey_base is not None and aey_base * 100 > UNRATED_MAX_YIELD_PCT:
            return "negative_ev"
        return "unrated"
    if aey_base * 100 > RATED_ABSOLUTE_MAX_AEY_PCT:
        return "negative_ev"
    floor = floor_ytm(rating)
    if floor is not None and aey_base < floor:
        return "negative_ev"
    ceiling = ceiling_spread_bp(rating, ceiling_multiple)
    if ceiling is not None and g_spread_bp is not None:
        if g_spread_bp > ceiling:
            return "negative_ev"
        caution = caution_spread_bp(rating, caution_multiple)
        if caution is not None and g_spread_bp > caution:
            return "caution"
    return "green"


def ev_positive(aey_base: float | None, rating: str | None) -> bool | None:
    """Pure floor check (Step 2's literal `ev_positive` column): is aey_base
    above this rating tier's floor_ytm? None if rating unknown or aey_base
    missing -- distinct from False, since "unrated" is not "negative EV"."""
    if not rating or aey_base is None:
        return None
    floor = floor_ytm(rating)
    if floor is None:
        return None
    return aey_base >= floor
