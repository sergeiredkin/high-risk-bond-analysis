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


# DEFAULT_PROBS is already ordered best-to-worst by construction -- exposed
# here so callers needing a rating-band comparison (e.g. universe_pipeline's
# ВДО-scope filter) reuse this ordering instead of hand-maintaining a second
# rating scale.
RATING_ORDER: list[str] = list(DEFAULT_PROBS.keys())


def rating_rank(rating: str) -> int | None:
    """Position on RATING_ORDER (0 = ruAAA, best), or None if unrecognised."""
    norm = _normalise(rating)
    return RATING_ORDER.index(norm) if norm in RATING_ORDER else None
