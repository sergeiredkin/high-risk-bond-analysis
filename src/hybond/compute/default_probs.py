"""Calibrated baseline default probabilities per rating level, sourced from
Эксперт РА's published theoretical (logistic-model) default matrix, 2001-2025,
as of April 2025. Pure lookup module -- no I/O, no network, no LLM.

Use the theoretical table, not the observed frequencies: the observed data has
rating-level anomalies from small samples (e.g. ruBB- observed 6.89% >
ruBB 5.06%, backwards). The logistic model smooths these into a monotone
relationship, and Эксперт РА itself recommends the theoretical table for
forward-looking use.

These frequencies are non-static -- the agency updates them semi-annually as
macro conditions shift (the 2022-2026 default wave has elevated BB/B
frequencies). Treat as calibrated priors, not precise loss estimates. Re-check
against raexpert.ru/about/disclosure/default-level-data/ roughly every six
months; next expected update ~H2 2026.

Source: Эксперт РА, «Матрица дефолтов как инструмент оценки кредитного риска»,
апрель 2025 (raexpert.ru/press/articles/pohvalenskaya_apr2025/). АКРА publishes
an equivalent at acra-ratings.ru/compliance/ -- cross-check if values diverge
significantly.

NOT rebased to Эксперт РА's 01.07.2026 regulatory disclosure
(matrix_defaults_01.07.2026.pdf) despite it being newer: that filing
(mandated under CBR Указание 6539-У) publishes only RAW OBSERVED default
frequencies, not the theoretical logistic-model matrix this table is built
from -- and its raw numbers reproduce the exact non-monotonicity problem
this docstring warns about above (e.g. in that filing, ruB's 2yr/3yr
observed frequencies are LOWER than ruB+'s, a worse-rated tier showing a
better outcome: 10.35%/14.39% vs 12.17%/18.25%). No newer "theoretical"
article superseding the April 2025 one was found (checked
raexpert.ru/ratings/credits/matrix and .../corporatebond/matrix -- neither
hosts a live theoretical table). Swapping in the raw disclosure would be a
methodology regression dressed up as a refresh, so it was flagged rather
than done silently -- re-check when Эксперт РА next publishes an updated
theoretical article.

Multi-year-horizon investigation (2026-07-31): none of the four Russian
agencies (Эксперт РА, АКРА, НКР, НРА) publish cumulative PD past 3 years --
confirmed against Эксперт РА's own 01.07.2026 disclosure text ("на
горизонте одного, двух и трех лет"), НКР's methodology paper (which
anchors to 3yr specifically because Basel Committee guidance recommends
3yr as the standard cross-agency comparison horizon), and НРА's disclosure
page (1yr/3yr only). This is a real ceiling in the published data, not a
gap in this codebase's sourcing -- see survival_probability()'s docstring
for how horizons beyond 3yr are handled (constant-hazard extrapolation,
explicitly not agency data).
"""
from __future__ import annotations

# Recovery rate for Russian ВДО: mid-point of the 30-40% practitioner range.
# Dominates the fair-spread calculation -- named constant, not a magic number.
# Flat across sectors for now; secured lenders (e.g. auto-pawn) plausibly
# recover more (50-55%), but per-sector differentiation is a later
# calibration step, not a v1 concern.
RECOVERY_RATE = 0.35

# Эксперт РА theoretical default probabilities (decimal, not percent).
DEFAULT_PROBS: dict[str, dict[str, float]] = {
    "ruAAA":  {"yr1": 0.00143, "yr2": 0.00562, "yr3": 0.00989},
    "ruAA+":  {"yr1": 0.00218, "yr2": 0.00796, "yr3": 0.01374},
    "ruAA":   {"yr1": 0.00284, "yr2": 0.00994, "yr3": 0.01693},
    "ruAA-":  {"yr1": 0.00371, "yr2": 0.01240, "yr3": 0.02083},
    "ruA+":   {"yr1": 0.00484, "yr2": 0.01545, "yr3": 0.02561},
    "ruA":    {"yr1": 0.00631, "yr2": 0.01924, "yr3": 0.03145},
    "ruA-":   {"yr1": 0.00823, "yr2": 0.02395, "yr3": 0.03858},
    "ruBBB+": {"yr1": 0.01072, "yr2": 0.02976, "yr3": 0.04724},
    "ruBBB":  {"yr1": 0.01396, "yr2": 0.03694, "yr3": 0.05772},
    "ruBBB-": {"yr1": 0.01815, "yr2": 0.04577, "yr3": 0.07037},
    "ruBB+":  {"yr1": 0.02357, "yr2": 0.05658, "yr3": 0.08553},
    "ruBB":   {"yr1": 0.03056, "yr2": 0.06975, "yr3": 0.10359},
    "ruBB-":  {"yr1": 0.03954, "yr2": 0.08572, "yr3": 0.12494},
    "ruB+":   {"yr1": 0.05102, "yr2": 0.10493, "yr3": 0.14996},
    "ruB":    {"yr1": 0.06561, "yr2": 0.12785, "yr3": 0.17897},
    "ruB-":   {"yr1": 0.08399, "yr2": 0.15490, "yr3": 0.21218},
    "ruCCC":  {"yr1": 0.13524, "yr2": 0.22273, "yr3": 0.29137},
    "ruCC":   {"yr1": 0.25834, "yr2": 0.35905, "yr3": 0.43681},
    "ruC":    {"yr1": 0.51756, "yr2": 0.58956, "yr3": 0.65416},
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
    """
    if not rating:
        return None
    return DEFAULT_PROBS.get(_normalise(rating))


def implied_fair_spread_bp(rating: str, horizon_yr: int = 1) -> float | None:
    """default_prob * (1 - RECOVERY_RATE) * 10000, or None if rating unknown."""
    probs = lookup(rating)
    if probs is None:
        return None
    key = _HORIZON_KEYS.get(horizon_yr, "yr1")
    return probs[key] * (1 - RECOVERY_RATE) * 10000


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
    """Constant per-year hazard rate implied by the rating's REAL 3-year
    cumulative PD: h = 1 - (1 - PD_yr3)**(1/3). None if rating unknown.

    APPROXIMATION, used only to extrapolate survival_probability() past the
    3-year table (see that function and this module's docstring for why no
    agency publishes anything past 3yr). Anchored to yr3, not yr1: the raw
    yr1->yr2->yr3 marginal hazards are not monotonic in the underlying agency
    data (e.g. ruBB's implied yr1->yr2 marginal hazard, ~4.04%, is HIGHER
    than its yr1 hazard of 3.056% -- small-sample/model noise, not a real
    front-loaded-risk signal), so yr3 -- the longest real data point -- is
    the least noisy single anchor available for projecting further out.
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

    Exact at t=1, t=2, t=3 (matches DEFAULT_PROBS's real cumulative PDs).
    Interpolated within (0,3) via piecewise-constant hazard per calendar
    year -- standard actuarial treatment, and consistent with the fact the
    underlying agency data is itself annual (there is no sub-year PD to
    interpolate from).

    Beyond t=3 (+YEAR_BOUNDARY_TOL, to absorb leap-day calendar drift):
    extrapolated using the constant hazard rate implied by the 3-year PD
    (implied_annual_hazard_from_yr3). THIS PORTION IS A MODEL, NOT AGENCY
    DATA -- no Russian rating agency publishes cumulative default
    probabilities past a 3-year horizon (checked all four: Эксперт РА, АКРА,
    НКР, НРА). Any caller surfacing a survival/PD figure computed from t>3
    must label it as an approximation, the same way RECOVERY_RATE and the
    YTM sanity cap are already labeled as assumptions rather than facts.
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
