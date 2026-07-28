"""Seed a SYNTHETIC second trading day into data/db/hybond.sqlite so the
Slice 2 diff engine has two days of history to compare against the real
Slice 1 pull.

This is demo data, not a live MOEX fetch: it takes day 1's real snapshot for
each bond and applies a plausible jitter (bigger for 'stress'-tier issuers,
per watchlist.json risk_tier, to give the diff engine something to flag).
Every row this script writes is confined to snap_date=DAY2 — nothing here
touches or overwrites the real day-1 data pulled by `hybond run`.

Run once. Re-running is safe (INSERT OR REPLACE on the same date).
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import sys
sys.path.insert(0, "src")

from hybond.store import db  # noqa: E402

DAY1 = "2026-07-27"
DAY2 = "2026-07-28"

STRESS_JITTER_BP = (80, 400)   # stress tier: big moves, sometimes flag-worthy
WATCH_JITTER_BP = (-60, 120)
CORE_JITTER_BP = (-30, 40)
ANCHOR_JITTER_BP = (-15, 15)

JITTER_BY_TIER = {
    "stress": STRESS_JITTER_BP,
    "watch": WATCH_JITTER_BP,
    "core": CORE_JITTER_BP,
    "anchor": ANCHOR_JITTER_BP,
}


def main() -> None:
    random.seed(20260728)  # reproducible demo run
    watchlist = json.loads(Path("watchlist.json").read_text(encoding="utf-8"))
    watchlist = {k: v for k, v in watchlist.items() if not k.startswith("_")}

    con = db.connect()
    db.init_db(con)

    n_bonds = 0
    for issuer_id, meta in watchlist.items():
        tier = meta.get("risk_tier", "core")
        lo, hi = JITTER_BY_TIER.get(tier, CORE_JITTER_BP)
        for secid in meta["secids"]:
            sp = db.latest_spread(con, secid, DAY2)  # already-seeded guard
            sp1 = con.execute(
                "SELECT * FROM spread_snapshot WHERE secid=? AND snap_date=?",
                (secid, DAY1),
            ).fetchone()
            pr1 = con.execute(
                "SELECT * FROM price_snapshot WHERE secid=? AND snap_date=?",
                (secid, DAY1),
            ).fetchone()
            if not sp1 or not pr1:
                continue  # bond had no day-1 price (illiquid) — nothing to jitter

            jitter_bp = random.uniform(lo, hi)
            new_g = max(50.0, sp1["g_spread_bp"] + jitter_bp)
            # keep ytm roughly consistent with the new spread (same rf move)
            ytm_shift = jitter_bp / 10000.0
            new_ytm = max(0.01, (sp1["ytm"] or 0.0) + ytm_shift)

            db.insert_spread_snapshot(
                con, secid=secid, snap_date=DAY2, ytm=new_ytm,
                tenor_yrs=sp1["tenor_yrs"], rf_rate=sp1["rf_rate"],
                g_spread_bp=new_g, mod_dur=sp1["mod_dur"],
                convexity=sp1["convexity"], yield_source=sp1["yield_source"],
            )
            vol_jitter = random.uniform(0.6, 1.4)
            db.insert_price_snapshot(
                con, secid=secid, snap_date=DAY2,
                last_pct=max(1.0, (pr1["last_pct"] or 100.0) - jitter_bp / 200.0),
                yield_moex=new_ytm * 100.0,
                duration=pr1["duration"],
                volume=(pr1["volume"] or 0) * vol_jitter,
                num_trades=pr1["num_trades"],
                systime=DAY2,
            )
            n_bonds += 1

    con.commit()
    con.close()
    print(f"Seeded synthetic day-2 ({DAY2}) spread+price snapshots for {n_bonds} bonds.")


if __name__ == "__main__":
    main()
