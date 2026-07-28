"""Factlist assembler. Produces the versioned per-issuer JSON that is the
boundary between the deterministic core and the future interpretive skills
(Slice 3+). Skills READ this; they never recompute or refetch it.

Same pattern as the MOEX suite's factlist module: the deterministic thing emits
facts, the SKILL.md files interpret them. Keeping this a stable, versioned schema
is what lets the skills stay cheap and auditable.

A factlist is built for ONE issuer on ONE date, from data already in the store.
Financials (six-pillar inputs) are stubbed until Slice 4; the schema reserves
their place so skills written against it don't change shape later.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "1.0"


@dataclass
class BondFacts:
    secid: str
    short_name: str | None
    isin: str | None
    mat_date: str | None
    coupon_pct: float | None
    ytm_pct: float | None
    g_spread_bp: float | None
    g_spread_delta_bp: float | None   # day-over-day, None if no prior
    yield_source: str | None          # 'computed' or 'moex' (floater proxy)
    mod_dur: float | None
    tenor_yrs: float | None
    last_pct: float | None
    volume: float | None
    num_trades: int | None


@dataclass
class Factlist:
    schema_version: str
    issuer_id: str
    issuer_name: str
    sector: str | None
    snap_date: str
    bonds: list[dict] = field(default_factory=list)
    # change-detection: what the diff engine flagged today, by detector
    flags: list[dict] = field(default_factory=list)
    stat_warming_up: bool = True      # True until statistical detector is live
    # reserved for later slices — schema-stable placeholders
    events_since_last: list[dict] = field(default_factory=list)   # Slice 3
    six_pillars: dict = field(default_factory=dict)               # Slice 4
    thesis_status: str | None = None                              # Slice 4
    # Default Probability Layer (see DEFAULT_PROB_FEATURE.md). None until a
    # rating is available -- rating ingestion into six_pillars is a separate
    # future step; do not block the factlist on it.
    default_prob_1yr: float | None = None
    implied_fair_spread_bp: float | None = None
    mispricing_bp: float | None = None       # actual G-spread minus implied fair

    def to_json(self) -> dict:
        return asdict(self)


def build_factlist(
    con,
    issuer_id: str,
    snap_date: str,
    flags: list[dict],
    stat_warming_up: bool,
) -> Factlist:
    """Assemble one issuer's factlist from the store. `flags` are the diff
    engine's Flag objects for this issuer, already dict-ified."""
    from ..compute import default_probs
    from ..store import db

    db.init_rating(con)  # idempotent; guards ad-hoc callers that skip it

    irow = con.execute("SELECT name, sector FROM issuer WHERE issuer_id=?",
                       (issuer_id,)).fetchone()
    name = irow["name"] if irow else issuer_id
    sector = irow["sector"] if irow else None

    bond_rows = con.execute(
        "SELECT secid, isin, short_name, mat_date, coupon_pct FROM bond "
        "WHERE issuer_id=?", (issuer_id,)).fetchall()

    bonds: list[dict] = []
    for b in bond_rows:
        secid = b["secid"]
        sp = db.latest_spread(con, secid, snap_date)
        pr = db.latest_price(con, secid, snap_date)
        # prior spread for the delta
        hist = db.spread_history(con, secid, snap_date, limit=1)
        prior = hist[0]["g_spread_bp"] if hist else None
        cur_g = sp["g_spread_bp"] if sp else None
        delta = (cur_g - prior) if (cur_g is not None and prior is not None) else None

        bonds.append(asdict(BondFacts(
            secid=secid,
            short_name=b["short_name"],
            isin=b["isin"],
            mat_date=b["mat_date"],
            coupon_pct=b["coupon_pct"],
            ytm_pct=round(sp["ytm"] * 100, 2) if sp and sp["ytm"] is not None else None,
            g_spread_bp=round(cur_g, 0) if cur_g is not None else None,
            g_spread_delta_bp=round(delta, 0) if delta is not None else None,
            yield_source=sp["yield_source"] if sp else None,
            mod_dur=round(sp["mod_dur"], 2) if sp and sp["mod_dur"] is not None else None,
            tenor_yrs=None,  # available in spread_snapshot; add if a skill needs it
            last_pct=pr["last_pct"] if pr else None,
            volume=pr["volume"] if pr else None,
            num_trades=pr["num_trades"] if pr else None,
        )))

    six_pillars = _latest_pillars(con, issuer_id)

    # Default Probability Layer (see DEFAULT_PROB_FEATURE.md). Rating comes
    # from the `rating` table (db.current_rating) -- the same source of
    # truth pipeline.py's spread screen uses, not six_pillars (which never
    # carried a rating field; that was a dead path).
    default_prob_1yr = implied_fair = misprice = None
    rating = db.current_rating(con, issuer_id)
    if rating:
        probs = default_probs.lookup(rating)
        default_prob_1yr = probs["yr1"] if probs else None
        implied_fair = default_probs.implied_fair_spread_bp(rating)
        if implied_fair is not None:
            # widest bond = most liquid signal in ВДО, where thinly traded
            # series can be stale
            priced = [b["g_spread_bp"] for b in bonds if b["g_spread_bp"] is not None]
            if priced:
                misprice = max(priced) - implied_fair

    return Factlist(
        schema_version=SCHEMA_VERSION,
        issuer_id=issuer_id,
        issuer_name=name,
        sector=sector,
        snap_date=snap_date,
        bonds=bonds,
        flags=flags,
        stat_warming_up=stat_warming_up,
        events_since_last=_events_since_last(con, issuer_id, snap_date),
        six_pillars=six_pillars,
        thesis_status=_thesis_status(con, issuer_id),
        default_prob_1yr=default_prob_1yr,
        implied_fair_spread_bp=implied_fair,
        mispricing_bp=misprice,
    )


def _events_since_last(con, issuer_id: str, snap_date: str) -> list[dict]:
    """Classified events for this issuer in a recent window (Slice 3). Tolerates
    the event table not existing yet."""
    from datetime import date as _date, timedelta as _td
    from ..store import db
    try:
        since = (_date.fromisoformat(snap_date) - _td(days=30)).isoformat()
    except ValueError:
        return []
    try:
        rows = db.events_since(con, issuer_id, since)
    except Exception:  # noqa: BLE001 — table may not exist pre-Slice-3
        return []
    return [{"occurred": r["occurred"], "category": r["category"],
             "severity": r["severity"], "summary": r["summary"]} for r in rows]


def _latest_pillars(con, issuer_id: str) -> dict:
    """Most recent computed six-pillar snapshot (Slice 4), or {} if none/absent."""
    import json as _json
    from ..store import db
    try:
        row = db.latest_pillars(con, issuer_id)
    except Exception:  # noqa: BLE001 — pillars_snapshot may not exist pre-Slice-4
        return {}
    if not row:
        return {}
    try:
        return _json.loads(row["payload"])
    except (ValueError, TypeError, KeyError):
        return {}


def _thesis_status(con, issuer_id: str) -> str | None:
    from ..store import db
    try:
        row = db.get_thesis(con, issuer_id)
    except Exception:  # noqa: BLE001
        return None
    return row["status"] if row else None
