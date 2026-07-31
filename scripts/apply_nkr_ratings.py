"""Apply НКР (ratings.ru) ratings to unrated ВДО-scope issuers.

Fetches ratings.ru's single-page issuer table (scrape/nkr.py), matches it
by normalised core name against every issuer behind a currently-unrated
vdo_scope=1 bond (universe_snapshot.rating IS NULL), and upserts a hit as a
real `НКР` rating row.

Staleness is applied from the start (see db.verification_flags's docstring
and the rating-staleness commit): confirming a name-match against НКР's live
table today means we verified the row NOW (verified_on = today), but
needs_verification is only cleared to 0 when the agency's own `rated_on` is
itself within RATING_STALENESS_MONTHS (12) of today -- an old rated_on is
still the best data available, just flagged for another look later rather
than given a clean bill of health.

An issuer missing from the `issuer` table entirely (universe_snapshot is
keyed by emitent_inn/emitent_title, not issuer_id -- most ВДО-scope names
were never seeded into `issuer`) is upserted there first, since rating.issuer_id
is an FK and PRAGMA foreign_keys=ON would otherwise reject the insert.

Idempotent: re-running re-fetches the live page and re-applies upsert_rating,
which is itself idempotent on (issuer_id, agency, rated_on).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybond.store import db  # noqa: E402
from hybond.scrape import nkr  # noqa: E402


def unrated_vdo_issuers(con) -> dict[str, str]:
    """{emitent_inn: emitent_title} for distinct issuers behind a vdo_scope=1
    bond that currently has no rating on its universe_snapshot row."""
    snap = db.latest_universe_snap_date(con)
    rows = db.universe_snapshot_rows(con, snap)
    out: dict[str, str] = {}
    for r in rows:
        if r["vdo_scope"] == 1 and not r["rating"] and r["emitent_inn"]:
            out[r["emitent_inn"]] = r["emitent_title"]
    return out


def main() -> None:
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)

    issuers = unrated_vdo_issuers(con)
    print(f"Unrated ВДО-scope issuers to check: {len(issuers)}")

    html = nkr.fetch_issuers_page()
    nkr_rows = nkr.parse_issuers(html)
    print(f"НКР issuer-table rows fetched: {len(nkr_rows)}")

    today = date.today()
    hits, stale_hits, current_hits, withdrawn_skipped = 0, 0, 0, 0

    for inn, title in issuers.items():
        match = nkr.match_by_name(title, nkr_rows)
        if match is None:
            continue
        if match.rating is None or match.rated_on is None:
            # withdrawn (or otherwise gradeless) row -- not a usable rating
            withdrawn_skipped += 1
            continue

        hits += 1
        verified_on, needs_verification = db.verification_flags(match.rated_on, today)
        if needs_verification:
            stale_hits += 1
        else:
            current_hits += 1

        existing_issuer = con.execute(
            "SELECT 1 FROM issuer WHERE issuer_id=?", (inn,)).fetchone()
        if not existing_issuer:
            db.upsert_issuer(con, issuer_id=inn, name=title)

        db.upsert_rating(
            con, issuer_id=inn, agency="НКР", rating=match.rating,
            outlook=match.outlook, action=None, rated_on=match.rated_on,
            source="nkr_scrape", verified_on=verified_on,
            needs_verification=needs_verification)

    con.commit()

    print(f"Matched (usable rating): {hits}")
    print(f"  -> current (needs_verification=0): {current_hits}")
    print(f"  -> stale   (needs_verification=1): {stale_hits}")
    print(f"Matched but withdrawn/gradeless (skipped): {withdrawn_skipped}")
    print(f"No match: {len(issuers) - hits - withdrawn_skipped}")

    con.close()


if __name__ == "__main__":
    main()
