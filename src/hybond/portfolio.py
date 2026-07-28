"""Portfolio scenario layer: per-bond Adjusted Expected Yield (AEY) under
three CBR rate-path scenarios (base/stress/bull), and the live rate-scenario
loader that drives it.

Rate deltas are computed fresh from the most recent cbr_meeting's own
rate_after/rate_2026_mid/rate_2027_mid/rate_2029_mid columns, never from
analysis_json's static aey_delta_rate_* fields -- those were seeded from a
user-authored draft and have since drifted from the corrected forecast
ranges (see export_dashboard_data.py's cbr_export docstring for the same
caveat about analysis_json's other fields).
"""
from __future__ import annotations

from .cbr_tracker import _midpoint
from .compute import default_probs

FALLBACK_SCENARIOS = {
    "base": -0.010, "stress": 0.015, "bull": -0.020,
    "source_date": "fallback", "cbr_2026": "?", "cbr_2027": "?", "cbr_2029": "?",
    "current_rate": None, "is_fallback": True,
}


def _range_high(s: str | None) -> float | None:
    """Upper bound of a "14,5-14,6"-style range string -- same en-dash/comma
    normalisation as cbr_tracker._midpoint, but the high end rather than the
    average. Used for the stress scenario, which prices the worst outcome
    still inside the CBR's own published range, not the range's centre."""
    if not s:
        return None
    normalised = s.replace(",", ".").replace("–", "-").replace("—", "-")
    parts = normalised.split("-")
    try:
        return max(float(parts[0]), float(parts[-1]))
    except (ValueError, IndexError):
        return None


def get_rate_scenarios(con) -> dict:
    """Load current rate-path deltas (decimal, annual) for the AEY overlay
    from the most recent cbr_meeting row with a completed analysis
    (analysis_json IS NOT NULL).

    base   = 2027 forecast midpoint - current key rate (central case)
    bull   = 2029 forecast midpoint - current key rate (cutting cycle runs
             its full course to the CBR's own neutral-rate estimate)
    stress = 2026 forecast HIGH - current key rate (worst case still inside
             the CBR's own published range, not a made-up shock)

    Falls back to conservative static defaults, visibly flagged via
    is_fallback=True, when no meeting has both a completed analysis and all
    three forecast columns populated -- callers must surface that flag
    rather than silently treating a fallback delta as a real one.
    """
    row = con.execute(
        "SELECT meeting_date, rate_after, rate_2026_mid, rate_2027_mid, rate_2029_mid "
        "FROM cbr_meeting WHERE analysis_json IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1"
    ).fetchone()

    if row:
        current = row["rate_after"]
        high_2026 = _range_high(row["rate_2026_mid"])
        mid_2027 = _midpoint(row["rate_2027_mid"])
        mid_2029 = _midpoint(row["rate_2029_mid"])
        if current is not None and None not in (high_2026, mid_2027, mid_2029):
            return {
                "base": (mid_2027 - current) / 100.0,
                "stress": (high_2026 - current) / 100.0,
                "bull": (mid_2029 - current) / 100.0,
                "source_date": row["meeting_date"],
                "cbr_2026": row["rate_2026_mid"],
                "cbr_2027": row["rate_2027_mid"],
                "cbr_2029": row["rate_2029_mid"],
                "current_rate": current,
                "is_fallback": False,
            }

    return dict(FALLBACK_SCENARIOS)


def bond_aey(ytm: float | None, mod_dur_yrs: float | None, delta_rate: float) -> float | None:
    """Adjusted Expected Yield: running YTM plus the one-time price effect of
    `delta_rate` (decimal, annual) acting on this bond's modified duration --
    the standard first-order bond-math approximation dP/P = -ModDur * dy,
    expressed as a yield-equivalent addition to YTM. None when either input
    needed for the calc is missing, rather than silently treating unknown
    duration as zero duration."""
    if ytm is None or mod_dur_yrs is None:
        return None
    return ytm - mod_dur_yrs * delta_rate


def pd_adjusted_aey(aey: float | None, rating: str | None, horizon_yr: int = 1) -> float | None:
    """Expected-return version of AEY: (1 - PD) * aey - PD * (1 - RECOVERY_RATE),
    using the same Эксперт РА default-probability table and recovery
    assumption as the mispricing signal (default_probs.py). None when there's
    no aey or no rating to look up a PD for -- an unrated bond stays
    unranked here rather than being silently treated as riskless.

    This turns a raw distress YTM into what it actually represents: a
    probability-weighted mix of "full yield" and "near-total loss on
    default", not a return to chase. It punishes low ratings hard (ruCCC's
    ~13.5% 1yr PD already halves a merely-high AEY), but does NOT by itself
    tame an already-absurd nominal YTM (hundreds/thousands of percent) --
    those numbers reflect a bond priced for restructuring regardless of the
    formal "default" event, so the linear (1-PD)*aey term still comes out
    huge. Callers ranking a candidate list should pair this with a YTM
    sanity cap (see export_dashboard_data.py / the CLI report), not rely on
    PD-weighting alone to exclude distress-priced bonds.
    """
    if aey is None or not rating:
        return None
    probs = default_probs.lookup(rating)
    if probs is None:
        return None
    key = default_probs._HORIZON_KEYS.get(horizon_yr, "yr1")
    pd = probs[key]
    return (1 - pd) * aey - pd * (1 - default_probs.RECOVERY_RATE)
