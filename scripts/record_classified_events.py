"""Record five manually-classified events (via hy-event-classify) into the
event table, mark their source filings classified, and update the EvroTrans
thesis to broken — its invalidation criterion (missed coupon) was met twice
in three days."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybond.store import db  # noqa: E402

EVENTS = [
    dict(issuer_id="5029169023", occurred="2026-07-22", category="coupon_event",
         severity="critical",
         summary="Technical default coupon #13 75.6M RUB — second default in "
                  "3 days, acute liquidity failure",
         source_url="https://e-disclosure.azipi.ru/messages/4569294/"),
    dict(issuer_id="5029169023", occurred="2026-07-20", category="coupon_event",
         severity="critical",
         summary="Technical default coupon #40 41.9M RUB — systemic liquidity "
                  "failure across multiple bond series",
         source_url="https://e-disclosure.azipi.ru/messages/4568381/"),
    dict(issuer_id="7707600245", occurred="2026-07-10", category="capital_action",
         severity="medium",
         summary="Irrevocable offer on new convertible bond placement — "
                  "planned capital action, no distress signal",
         source_url="https://e-disclosure.azipi.ru/messages/4564950/"),
    dict(issuer_id="9705101614", occurred="2026-07-21", category="operations",
         severity="low",
         summary="H1 2026 operating results: new business +13% YoY, coupons "
                  "paid on time",
         source_url="https://e-disclosure.azipi.ru/messages/4568410/"),
    dict(issuer_id="1402047184", occurred="2026-07-15", category="capital_action",
         severity="medium",
         summary="Board approved 50bn RUB syndicated credit facility with "
                  "Gazprombank — strengthens refinancing position",
         source_url="https://e-disclosure.azipi.ru/messages/4566872/"),
]

BODY_HASHES = [
    "0372656d8e3329cf", "db661a047bf20e17", "9d5585c3512a049e",
    "3b1d562dd7ccdf08", "29ec88a365b89456",
]


def main() -> None:
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.init_pillars(con)

    for e in EVENTS:
        eid = db.insert_event(con, **e)
        print(f"event {eid}: {e['issuer_id']} {e['category']}/{e['severity']} "
              f"{e['occurred']}")

    for h in BODY_HASHES:
        db.mark_classified(con, h)
    print(f"marked {len(BODY_HASHES)} filings classified")

    current = db.get_thesis(con, "5029169023")
    db.upsert_thesis(
        con, issuer_id="5029169023",
        thesis=current["thesis"], assumptions=current["assumptions"],
        invalidation=current["invalidation"],
        status="broken", updated="2026-07-27",
    )
    print("EvroTrans thesis -> broken")

    con.commit()
    remaining = db.unclassified_filings(con)
    print(f"Unclassified queue now: {len(remaining)}")
    con.close()


if __name__ == "__main__":
    main()
