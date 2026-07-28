"""The delta engine. Compares today's snapshot against the issuer's own history
and decides what changed. No LLM.

Three detectors run every day and each LOGS what it would flag, so all three can
later be graded against one shared history (see PLAN.md, Slice 2 design):

  fixed   — absolute thresholds. Works cold (day one), no history needed.
            Over-flags volatile names, under-flags quiet ones.
  stat    — z-score of a change vs the issuer's own rolling history.
            Self-calibrating, but blind until ~20 snapshots exist. Needs a
            floor or a 2σ move on a near-flat issuer is meaningless bp.
  hybrid  — fires only when BOTH a floor is cleared AND the move is unusual
            for that issuer. Almost certainly the endpoint; inherits stat's
            cold-start, so it produces nothing until history accrues.

Early on, `fixed` DRIVES attention; `stat` and `hybrid` log silently. Grade
around week 4-6 when each issuer has enough snapshots, then pick and tune.

A detector emits Flag objects; the caller (Slice 2 pipeline) persists them to
flag_log with the `driving` bit set on whichever detector is in charge.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


# --- tunable defaults (deliberately conservative; tune after grading) ------

FIXED_SPREAD_BP = 50.0        # |ΔG-spread| day-over-day to flag
FLOOR_SPREAD_BP = 25.0        # hybrid: never chase a move smaller than this
STAT_Z = 2.0                  # stat/hybrid: |z| of the change to flag
MIN_HISTORY_FOR_STAT = 8      # snapshots of *changes* needed before stat is live
VOLUME_ZERO_AFTER_TRADING = True  # flag a name that was trading then went silent


@dataclass(frozen=True)
class Flag:
    secid: str
    issuer_id: str
    detector: str      # 'fixed' | 'stat' | 'hybrid'
    signal: str        # 'spread_move' | 'volume_zero' | 'new_filing' | ...
    metric: float
    detail: str


def _daily_spread_changes(hist_desc: list) -> list[float]:
    """From spread history (newest-first rows with g_spread_bp), the series of
    day-over-day changes, oldest-first. Used to characterise an issuer's normal
    spread noise for the z-score."""
    rows = list(reversed(hist_desc))  # oldest-first
    vals = [r["g_spread_bp"] for r in rows if r["g_spread_bp"] is not None]
    return [b - a for a, b in zip(vals, vals[1:])]


def detect_spread_move(
    secid: str,
    issuer_id: str,
    today_spread_bp: float,
    prior_spread_bp: float | None,
    hist_desc: list,
) -> list[Flag]:
    """Run all three detectors on the day's G-spread change. Returns every flag
    that fired (across detectors); the caller decides which one is driving."""
    flags: list[Flag] = []
    if prior_spread_bp is None:
        return flags  # no delta on an issuer's first appearance
    delta = today_spread_bp - prior_spread_bp

    # Version A — fixed
    if abs(delta) >= FIXED_SPREAD_BP:
        flags.append(Flag(secid, issuer_id, "fixed", "spread_move", delta,
                          f"ΔG-spread {delta:+.0f}bp ≥ {FIXED_SPREAD_BP:.0f}bp floor"))

    # Versions B/C need the issuer's own change distribution
    changes = _daily_spread_changes(hist_desc)
    if len(changes) >= MIN_HISTORY_FOR_STAT:
        mu = statistics.mean(changes)
        sd = statistics.pstdev(changes)
        if sd > 0:
            z = (delta - mu) / sd
            # Version B — statistical
            if abs(z) >= STAT_Z:
                flags.append(Flag(secid, issuer_id, "stat", "spread_move", z,
                                  f"ΔG-spread {delta:+.0f}bp = {z:+.1f}σ for this issuer"))
            # Version C — hybrid: floor AND unusual
            if abs(delta) >= FLOOR_SPREAD_BP and abs(z) >= STAT_Z:
                flags.append(Flag(secid, issuer_id, "hybrid", "spread_move", delta,
                                  f"ΔG-spread {delta:+.0f}bp ({z:+.1f}σ), clears {FLOOR_SPREAD_BP:.0f}bp floor"))
    return flags


def detect_volume_silence(
    secid: str,
    issuer_id: str,
    today_volume: float | None,
    today_trades: int | None,
    prior_traded: bool,
) -> list[Flag]:
    """A ВДО name that was trading and has gone silent is a liquidity signal.
    Fixed-only (no distribution needed). Applies to all detectors as a shared
    structural signal, logged under 'fixed'."""
    if not VOLUME_ZERO_AFTER_TRADING:
        return []
    silent = (today_volume in (None, 0)) or (today_trades in (None, 0))
    if prior_traded and silent:
        return [Flag(secid, issuer_id, "fixed", "volume_zero", 0.0,
                     "traded previously, no trades today")]
    return []


def detect_new_filing(secid: str, issuer_id: str, new_filing_titles: list[str]) -> list[Flag]:
    """A new disclosure appearing is a structural change flag regardless of
    detector — it doesn't need history. Logged under 'fixed'."""
    return [Flag(secid, issuer_id, "fixed", "new_filing", float(len(new_filing_titles)),
                 "; ".join(new_filing_titles[:3]))
            for _ in [0] if new_filing_titles]


def stat_is_live(hist_desc: list) -> bool:
    """True once the statistical detector has enough change-history to mean
    anything. Surfaced so the pipeline/report can say 'stat still warming up'."""
    return len(_daily_spread_changes(hist_desc)) >= MIN_HISTORY_FOR_STAT
