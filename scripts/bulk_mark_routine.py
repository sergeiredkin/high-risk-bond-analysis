"""Bulk-mark filings whose title is definitively routine/noise — no LLM
classification needed for these. Inserts a routine/noise event record
(matching skills/hy-event-classify/references/taxonomy.md's `routine`
category) and marks the filing classified, so the queue only surfaces
filings that actually need hy-event-classify's judgment.

Rules are (issuer_id, title) pairs, not title-only. Sampling the Брусника
bulk-mark turned up the same exact title —
"Совершение подконтрольной эмитенту организацией существенной сделки" —
meaning "крупная сделка" (major transaction) for Селигдар's filings while
being routine SPV construction financing for Брусника's. A title-global rule
would have silently misclassified Селигдар's major transactions as noise.
So every rule here is either issuer-scoped (issuer_id given) or has been
sampled across ALL watchlist issuers and confirmed to generalize (issuer_id
None) — do not add a `None` rule without that check.

"Решения совета директоров (наблюдательного совета)" is included on the
caller's explicit instruction, but is a materially different case from the
other global titles: it's the board's actual decisions, not just a meeting
notice, and per the taxonomy a board decision can itself be the event
(governance/capital_action). Its count is broken out separately below —
worth spot-checking body text on a sample before trusting this long-term.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybond.store import db  # noqa: E402

# (issuer_id, title) — issuer_id None means "confirmed routine for any
# issuer by sampling"; a real issuer_id (ИНН) scopes the rule to that
# issuer only, because the same title can mean something else elsewhere.
ROUTINE_RULES: list[tuple[str | None, str]] = [
    (None, "Выплаченные доходы по эмиссионным ценным бумагам эмитента"),
    (None, "Начисленные доходы по эмиссионным ценным бумагам эмитента"),
    (None, "Проведение заседания совета директоров (наблюдательного совета) и его повестка дня"),
    (None, "Решения совета директоров (наблюдательного совета)"),
    # Брусника: 10/10 sampled -> "не крупная, обычная хозяйственная
    # деятельность" (SPV-level construction financing). Same title on
    # Селигдар's filings was "крупная сделка" in both sampled cases -- do
    # NOT widen this to issuer_id=None.
    ("6685151087", "Совершение подконтрольной эмитенту организацией существенной сделки"),
]


def matches_routine_rule(issuer_id: str, title: str) -> bool:
    """True if (issuer_id, title) is covered by ROUTINE_RULES."""
    return any(
        title == rule_title and rule_issuer in (None, issuer_id)
        for rule_issuer, rule_title in ROUTINE_RULES
    )


def main() -> None:
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)

    before = db.unclassified_filings(con)
    print(f"Before: {len(before)} unclassified")

    by_title = Counter()
    for row in before:
        if not matches_routine_rule(row["issuer_id"], row["title"]):
            continue
        db.insert_event(
            con, issuer_id=row["issuer_id"],
            occurred=row["published"] or row["fetched_on"],
            category="routine", severity="noise",
            summary=row["title"], source_url=row["url"],
        )
        db.mark_classified(con, row["body_hash"])
        by_title[row["title"]] += 1
    con.commit()

    after = db.unclassified_filings(con)
    print(f"Marked routine: {sum(by_title.values())}")
    for title, n in by_title.most_common():
        print(f"  {n:4d}  {title}")
    print(f"After: {len(after)} unclassified (still need hy-event-classify)")
    con.close()


if __name__ == "__main__":
    main()
