"""Seed the `rating` table from watchlist.json's manual `rating` field --
one-time bootstrap for the Default Probability Layer (see
DEFAULT_PROB_FEATURE.md) until real agency ingestion exists.

Every row is stamped agency='manual', source='manual' -- deliberately lowest
priority in db.current_rating()'s tiebreak order, so a real АКРА/Эксперт РА
row added later (via `hybond rate`) always wins over this bootstrap value.

Skips issuers with no rating or an unresolved 'TBD' placeholder (e.g. МФК
Мани Капитал) -- never seed a guess.

Re-running is safe: upsert_rating flips the prior 'manual' row to
is_current=0 and inserts/updates the new one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybond.store import db  # noqa: E402

RATED_ON = "2026-07-27"  # date the watchlist ratings were last verified/cleaned


def main() -> None:
    wl = json.loads(Path("watchlist.json").read_text(encoding="utf-8"))
    wl = {k: v for k, v in wl.items() if not k.startswith("_")}

    con = db.connect()
    db.init_db(con)
    db.init_rating(con)

    n = 0
    for inn, v in wl.items():
        r = v.get("rating")
        if not r or r == "TBD":
            continue
        db.upsert_rating(con, issuer_id=inn, agency="manual", rating=r,
                          outlook=None, action=None, rated_on=RATED_ON,
                          source="manual")
        n += 1
    con.commit()
    con.close()
    print(f"Seeded {n} rating(s) from watchlist.json into the rating table.")


if __name__ == "__main__":
    main()
