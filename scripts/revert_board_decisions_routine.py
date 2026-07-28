"""Revert the bulk-routine classification for "Решения совета директоров
(наблюдательного совета)" filings. A 3-sample content check found all three
contained real material content (related-party transaction approval, major-
transaction approval, dividend recommendation) — not noise. This undoes that
one title's auto-classification: deletes the fabricated routine/noise event
rows it produced and un-marks those filings so they return to the
hy-event-classify queue. The other three routine titles (coupon paid/accrued,
board-meeting notice) are untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybond.store import db  # noqa: E402

TITLE = "Решения совета директоров (наблюдательного совета)"


def main() -> None:
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)

    rows = con.execute(
        "SELECT body_hash, url FROM filing WHERE title=? AND classified=1",
        (TITLE,),
    ).fetchall()
    print(f"Found {len(rows)} classified filings with this title")

    urls = [r["url"] for r in rows]
    deleted = 0
    for url in urls:
        cur = con.execute(
            "DELETE FROM event WHERE category='routine' AND severity='noise' "
            "AND summary=? AND source_url=?", (TITLE, url))
        deleted += cur.rowcount

    con.execute(
        "UPDATE filing SET classified=0 WHERE title=? AND classified=1", (TITLE,))
    con.commit()

    remaining = db.unclassified_filings(con)
    print(f"Deleted {deleted} fabricated routine event rows")
    print(f"Un-marked {len(rows)} filings -> back in queue")
    print(f"Unclassified queue now: {len(remaining)}")
    con.close()


if __name__ == "__main__":
    main()
