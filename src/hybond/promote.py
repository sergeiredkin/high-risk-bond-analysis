"""Phase 2 promotion screen: rank the broad ВДО-scope universe (`vdo_scope=1`,
computed daily in universe_pipeline.run_universe()) down to a shortlist of
candidates for deep monitoring (pillars, events, financials -- the full
26-issuer treatment this platform already gives its watchlist). Pure query +
ranking over the already-computed `universe_snapshot` table -- no new
storage, same "don't duplicate storage" principle as vdo_scope itself.

Filter order (each stage only sees survivors of the one before it):
  1. Rating floor (hard) -- ruB- or better; excludes ruCCC/ruCC/ruC and any
     unparseable rating. Unrated bonds are excluded from the candidate list
     too (no PD input to rank them on) but tracked separately as
     "unrated near-miss" -- they passed vdo_scope (so already clear the
     liquidity/spread/bank-debt gates) and would be worth rating-capture
     priority precisely because nothing else is disqualifying them.
  2. Liquidity floor (hard) -- same volume threshold as vdo_scope's own gate
     (universe_pipeline.VDO_MIN_VOLUME). Reapplied explicitly here (not just
     inherited from vdo_scope=1) so this function's own cut-count is honest
     even if vdo_scope's gate logic changes later -- expected to cut ~0 in
     practice today, since every vdo_scope=1 row already cleared it upstream.
  3. Risk-adjusted AEY ranking -- aey_base_risk_adj descending (requires a
     rating, so rows without one already left in stage 1). Top N before the
     concentration cap gets room to work without under-filling the final list.
  4. Issuer concentration cap -- portfolio.apply_issuer_cap(), reused as-is
     (max 2 bonds/issuer by default), producing the final candidate list.

THRESHOLDS BELOW ARE A STARTING POINT, NOT A FINAL POLICY. B- floor,
N=30-35 before the cap, and cap=2/issuer were chosen in the abstract, before
seeing what the actual candidate list looks like -- they're meant to be
observed and tuned against real output (see the CLI report), not treated as
settled once this ships.
"""
from __future__ import annotations

from .compute import default_probs
from .portfolio import apply_issuer_cap
from .store import db
from .universe_pipeline import VDO_MIN_VOLUME

RATING_FLOOR_DEFAULT = "ruB-"
N_BEFORE_CAP_DEFAULT = 32
PER_ISSUER_CAP_DEFAULT = 2
TARGET_AFTER_CAP_DEFAULT = 25


def promote_candidates(
    con, snap_date: str | None = None,
    rating_floor: str = RATING_FLOOR_DEFAULT,
    n_before_cap: int = N_BEFORE_CAP_DEFAULT,
    per_issuer_cap: int = PER_ISSUER_CAP_DEFAULT,
    min_volume: float | None = None,
    target_after_cap: int | None = TARGET_AFTER_CAP_DEFAULT,
) -> dict:
    """Run the 4-stage promotion filter over the latest (or given) ВДО-scope
    snapshot. Returns the final candidate list plus a per-stage cut count and
    the unrated near-miss list, so a caller can see exactly where bonds fell
    out rather than just the survivors."""
    min_volume = VDO_MIN_VOLUME if min_volume is None else min_volume
    snap_date = snap_date or db.latest_universe_snap_date(con)
    if not snap_date:
        return {
            "snap_date": None, "scope_total": 0,
            "cut_by_rating_floor": 0, "cut_by_liquidity_floor": 0,
            "cut_by_missing_aey": 0, "cut_by_concentration_cap": 0,
            "candidates": [], "unrated_near_miss": [],
            "rating_floor": rating_floor, "n_before_cap": n_before_cap,
            "per_issuer_cap": per_issuer_cap,
        }

    scope_rows = [dict(r) for r in con.execute(
        "SELECT * FROM universe_snapshot WHERE snap_date=? AND vdo_scope=1",
        (snap_date,))]

    floor_rank = default_probs.rating_rank(rating_floor)

    # --- stage 1: rating floor ---------------------------------------------
    passed_rating, unrated_near_miss = [], []
    cut_by_rating = 0
    for row in scope_rows:
        rating = row.get("rating")
        if rating is None:
            unrated_near_miss.append(row)
            continue
        rank = default_probs.rating_rank(rating)
        if rank is None or rank > floor_rank:
            cut_by_rating += 1
            continue
        passed_rating.append(row)

    # --- stage 2: liquidity floor -------------------------------------------
    passed_liquidity = []
    cut_by_liquidity = 0
    for row in passed_rating:
        vol = row.get("volume")
        if vol is None or vol < min_volume:
            cut_by_liquidity += 1
            continue
        passed_liquidity.append(row)

    # --- stage 3: risk-adjusted AEY ranking ---------------------------------
    rankable = [r for r in passed_liquidity if r.get("aey_base_risk_adj") is not None]
    cut_by_missing_aey = len(passed_liquidity) - len(rankable)
    rankable.sort(key=lambda r: r["aey_base_risk_adj"], reverse=True)
    top_n = rankable[:n_before_cap]

    # --- stage 4: issuer concentration cap ----------------------------------
    capped = apply_issuer_cap(top_n, per_issuer_cap)
    final = capped[:target_after_cap] if target_after_cap else capped
    cut_by_concentration_cap = len(top_n) - len(final)

    unrated_near_miss.sort(key=lambda r: r.get("g_spread_bp") or 0, reverse=True)

    return {
        "snap_date": snap_date,
        "scope_total": len(scope_rows),
        "cut_by_rating_floor": cut_by_rating,
        "cut_by_liquidity_floor": cut_by_liquidity,
        "cut_by_missing_aey": cut_by_missing_aey,
        "cut_by_concentration_cap": cut_by_concentration_cap,
        "candidates": final,
        "unrated_near_miss": unrated_near_miss,
        "rating_floor": rating_floor,
        "n_before_cap": n_before_cap,
        "per_issuer_cap": per_issuer_cap,
    }
