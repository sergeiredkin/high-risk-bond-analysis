"""Calibrated baseline default probabilities per rating level.

2026-08-16 rebase: previously used Эксперт РА's theoretical (logistic-model)
default matrix from April 2025, DELIBERATELY not the agency's raw observed
disclosure -- see git history on this docstring for that reasoning (the raw
data has small-sample non-monotonicity, e.g. a worse-rated tier showing a
better observed outcome than a better-rated one). This task explicitly
directs replacing the table with the raw-observed 01.07.2026 disclosure
figures anyway (regulatory-mandated under CBR Указание 6539-У), so that is
what DEFAULT_PROBS's yr1 values now are. The non-monotonicity the old
docstring warned about is still present in this new 1yr data (e.g. ruB+
0.0582 > ruB 0.0505 -- a worse letter grade showing a LOWER observed 1yr
default rate) -- flagging this explicitly rather than silently absorbing a
methodology reversal: this table is now the officially-sourced but noisier
one, not the smoothed one.

Structural note: Эксперт РА's 01.07.2026 Таблица 7 publishes ONLY a 1-year
horizon. The rest of this module (survival_probability, expected_return.py's
survival-weighted IRR, mispricing_bp at horizon_yr=2/3) depends on yr2/yr3
per-rating figures that no longer exist as agency data. Rather than fabricate
yr2/yr3 numbers (explicitly out of scope) or break every multi-year caller,
yr2/yr3 are DERIVED from the new yr1 via a constant-hazard (flat annual
hazard rate) model: h = -ln(1 - yr1), yr_n = 1 - (1-yr1)**n. This is the same
technique survival_probability() already uses to extrapolate PAST year 3
(see implied_annual_hazard_from_yr3) -- applied here from year 1 instead,
since year 1 is now the only real anchor point. uses_extrapolated_pd (see
expected_return.py) does NOT currently flag yr2/yr3 as extrapolated the way
it flags >3yr -- that flag's scope was written when yr2/yr3 were real agency
data and has not been updated; treat ANY horizon >1yr computed from this
table as model output, not just >3yr, until that flag is widened.

Source: Эксперт РА, Таблица 7, 01.07.2026
1-year default frequencies, national Russian rating scale
Next update due: January 2027 (semi-annual publication schedule)
Do not edit manually — update via: hybond check-pd-table
(that CLI command does not exist yet -- this comment documents the intended
future update path per the task that introduced this table, not a shipped
feature; update DEFAULT_PROBS_1YR by hand until it's built.)

Source PDF: data/exports/pd_tables/matrix_defaults_2026_07_01.pdf
"Исторические данные об уровнях дефолта по рейтинговым категориям
применяемых рейтинговых шкал на 01.07.2026", Таблица 7, горизонт 1 года.
"""
from __future__ import annotations

import math

# LGD (loss given default) and its complement RECOVERY_RATE, for Russian ВДО.
#
# Source: Fedresurs empirical recovery data, Russian ВДО bonds 2022-2026
# Average recovery rate 6.6% (retail investors receive ~6.6 kopeks per rouble)
# Significantly lower than global IG averages (35-40%) due to:
# - Unsecured subordinated claims typical in ВДО
# - Russian bankruptcy proceedings favouring secured creditors
# - Limited liquidation value of typical ВДО issuers
#
# Previously RECOVERY_RATE = 0.35 (LGD = 0.65), the mid-point of a generic
# 30-40% practitioner range -- not Russian-ВДО-specific, not empirical. LGD
# is derived from RECOVERY_RATE (not a second independent magic number) so
# the two can never drift out of sync: LGD = 1 - RECOVERY_RATE = 0.934.
RECOVERY_RATE = 0.066
LGD = 1 - RECOVERY_RATE

# Эксперт РА official 1-year default frequencies (decimal, not percent) --
# see module docstring for source/date/non-monotonicity notes.
DEFAULT_PROBS_1YR: dict[str, float] = {
    "ruAAA":  0.0000,
    "ruAA+":  0.0000,
    "ruAA":   0.0021,
    "ruAA-":  0.0000,
    "ruA+":   0.0050,
    "ruA":    0.0065,
    "ruA-":   0.0059,
    "ruBBB+": 0.0107,
    "ruBBB":  0.0198,
    "ruBBB-": 0.0147,
    "ruBB+":  0.0295,
    "ruBB":   0.0460,
    "ruBB-":  0.0598,
    "ruB+":   0.0582,
    "ruB":    0.0505,
    "ruB-":   0.0526,
    "ruCCC":  0.1364,
    "ruCC":   0.2857,
    "ruC":    0.5000,
}


def _hazard_derived_multi_year(yr1: float) -> dict[str, float]:
    """yr2/yr3 cumulative PD from a flat annual hazard implied by yr1 -- see
    module docstring's "Structural note" for why (Table 7 is 1yr-only)."""
    if yr1 <= 0:
        return {"yr1": yr1, "yr2": 0.0, "yr3": 0.0}
    if yr1 >= 1:
        return {"yr1": yr1, "yr2": 1.0, "yr3": 1.0}
    h = -math.log(1 - yr1)
    return {
        "yr1": yr1,
        "yr2": 1 - math.exp(-2 * h),
        "yr3": 1 - math.exp(-3 * h),
    }


# Эксперт РА official default probabilities: yr1 is the real 01.07.2026
# Table 7 figure; yr2/yr3 are hazard-derived (see _hazard_derived_multi_year
# and the module docstring's "Structural note" -- NOT independently sourced
# agency data).
DEFAULT_PROBS: dict[str, dict[str, float]] = {
    rating: _hazard_derived_multi_year(yr1)
    for rating, yr1 in DEFAULT_PROBS_1YR.items()
}

_HORIZON_KEYS = {1: "yr1", 2: "yr2", 3: "yr3"}

def _normalise(rating: str) -> str:
    """"BB" -> "ruBB", "bb-" -> "ruBB-", "ruBB" -> "ruBB" (unchanged)."""
    r = rating.strip()
    if not r.lower().startswith("ru"):
        r = "ru" + r
    return "ru" + r[2:].upper()


def lookup(rating: str) -> dict[str, float] | None:
    """Probability dict for a rating string, or None if not found.

    Handles common variants: missing "ru" prefix, case differences. Returns
    None rather than raising -- callers (factlist, pipeline) handle a miss by
    treating the mispricing fields as not-yet-available, not as an error.

    Fallback for a scale-suffixed rating (e.g. "ruB+.mfi", a hypothetical
    microfinance-scale variant -- grepped the live rating table and every
    scraper's normalisation code on 2026-08-16 and found NONE in actual use
    today, but the task that added this fallback asked for it defensively):
    if the exact normalised string isn't a key, strip everything from the
    first "." onward and retry against the base tier, rather than returning
    None for a rating that's really just a suffixed variant of a known tier.
    """
    if not rating:
        return None
    norm = _normalise(rating)
    hit = DEFAULT_PROBS.get(norm)
    if hit is not None:
        return hit
    if "." in norm:
        base = norm.split(".", 1)[0]
        return DEFAULT_PROBS.get(base)
    return None


def implied_fair_spread_bp(rating: str, horizon_yr: int = 1) -> float | None:
    """default_prob * LGD * 10000, or None if rating unknown."""
    probs = lookup(rating)
    if probs is None:
        return None
    key = _HORIZON_KEYS.get(horizon_yr, "yr1")
    return probs[key] * LGD * 10000


def mispricing_bp(actual_g_spread_bp: float, rating: str,
                   horizon_yr: int = 1) -> float | None:
    """actual - implied fair spread. Positive = cheap (wider than rating
    implies), negative = rich (tighter than rating implies). None if rating
    unknown."""
    implied = implied_fair_spread_bp(rating, horizon_yr)
    if implied is None:
        return None
    return actual_g_spread_bp - implied


def implied_annual_hazard_from_yr3(rating: str) -> float | None:
    """Constant per-year hazard rate implied by the rating's yr3 cumulative
    PD: h = 1 - (1 - PD_yr3)**(1/3). None if rating unknown.

    APPROXIMATION, used only to extrapolate survival_probability() past the
    3-year table. NOTE (2026-08-16 PD rebase): yr3 itself is now hazard-
    derived from yr1, not independent agency data -- see this module's
    docstring "Structural note". This function's own extrapolation-beyond-
    yr3 is therefore a model built on a model; still the least-bad option
    available (yr1 is the only real anchor left), but treat any horizon >1yr
    read from this module as approximation, full stop.
    """
    probs = lookup(rating)
    if probs is None:
        return None
    return 1 - (1 - probs["yr3"]) ** (1 / 3)


# Real coupon dates land on calendar years, so a "3-year bond" measured
# ACT/365F is ~3.003-3.008yr once leap days are counted, not exactly 3.0.
# This tolerance keeps that leap-day drift from being misclassified as
# reaching past the real 3yr table -- it is calendar-arithmetic noise, not a
# genuinely longer horizon. 0.02yr (~7 days) comfortably covers it while
# still flagging any horizon that is actually a year or more past the table.
YEAR_BOUNDARY_TOL = 0.02


def survival_probability(rating: str, t_years: float) -> float | None:
    """P(no default by t_years), for any t_years >= 0. None if rating unknown.

    Exact at t=1 (real agency data). t=2/t=3 are hazard-derived from t=1, not
    independent agency data as of the 2026-08-16 PD rebase -- see this
    module's docstring "Structural note". Interpolated within (0,3) via
    piecewise-constant hazard per calendar year -- standard actuarial
    treatment.

    Beyond t=3 (+YEAR_BOUNDARY_TOL, to absorb leap-day calendar drift):
    extrapolated using the constant hazard rate implied by the 3-year PD
    (implied_annual_hazard_from_yr3). Any caller surfacing a survival/PD
    figure computed from t>1 must label it as an approximation, the same way
    RECOVERY_RATE/LGD and the YTM sanity cap are already labeled as
    assumptions rather than facts.
    """
    if t_years < 0:
        raise ValueError("t_years must be >= 0")
    probs = lookup(rating)
    if probs is None:
        return None
    s1, s2, s3 = 1 - probs["yr1"], 1 - probs["yr2"], 1 - probs["yr3"]
    if t_years <= 1 + YEAR_BOUNDARY_TOL:
        return (1 - probs["yr1"]) ** t_years
    if t_years <= 2 + YEAR_BOUNDARY_TOL:
        h2 = 1 - (s2 / s1) if s1 > 0 else 1.0
        return s1 * (1 - h2) ** (t_years - 1)
    if t_years <= 3 + YEAR_BOUNDARY_TOL:
        h3 = 1 - (s3 / s2) if s2 > 0 else 1.0
        return s2 * (1 - h3) ** (t_years - 2)
    h_ext = implied_annual_hazard_from_yr3(rating)
    return s3 * (1 - h_ext) ** (t_years - 3)


# DEFAULT_PROBS is already ordered best-to-worst by construction -- exposed
# here so callers needing a rating-band comparison (e.g. universe_pipeline's
# ВДО-scope filter) reuse this ordering instead of hand-maintaining a second
# rating scale.
RATING_ORDER: list[str] = list(DEFAULT_PROBS.keys())


def rating_rank(rating: str) -> int | None:
    """Position on RATING_ORDER (0 = ruAAA, best), or None if unrecognised."""
    norm = _normalise(rating)
    return RATING_ORDER.index(norm) if norm in RATING_ORDER else None
