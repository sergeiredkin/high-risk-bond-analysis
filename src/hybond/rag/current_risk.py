"""Structured current-risk context from HyBond's live analytical tables."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hybond.ratings import resolve as resolve_rating


def is_current_risk_question(question: str) -> bool:
    """Detect questions that require current HyBond analytics, not only RAG text."""
    value = question.casefold()
    return ("риск" in value or "риски" in value or "risk" in value) and any(
        marker in value for marker in ("текущ", "сейчас", "данн", "сегодня", "момент")
    )


def current_risk_summary(con, issuer_id: str) -> dict:
    """Build a dated, non-generative risk summary from stored SQL facts."""
    issuer = con.execute(
        "SELECT issuer_id,name,health_status,health_reason FROM issuer WHERE issuer_id=?",
        (issuer_id,),
    ).fetchone()
    if issuer is None:
        return {"status": "not_found", "issuer_id": issuer_id}
    has_universe = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_snapshot'"
    ).fetchone() is not None
    latest = con.execute(
        """SELECT MAX(snap_date) FROM universe_snapshot WHERE emitent_inn=?""",
        (issuer_id,),
    ).fetchone()[0] if has_universe else None
    bonds = []
    if latest:
        bonds = [dict(row) for row in con.execute(
            """SELECT secid,short_name,ytm,g_spread_bp,mispricing_bp,mat_date,offer_date
               FROM universe_snapshot WHERE emitent_inn=? AND snap_date=?
               ORDER BY g_spread_bp DESC LIMIT 5""", (issuer_id, latest)
        ).fetchall()]
    cutoff = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    events = [dict(row) for row in con.execute(
        """SELECT occurred,category,severity,summary,source_url FROM event
           WHERE issuer_id=? AND occurred>=? ORDER BY occurred DESC LIMIT 10""",
        (issuer_id, cutoff),
    ).fetchall()]
    rating_resolution = resolve_rating(con, inn=issuer_id)
    ratings = [{
        "agency": row["agency"], "rating": row["grade"],
        "outlook": None, "action": None, "rated_on": row["rated_on"],
        "source": row["source"], "scale": row["scale"],
        "status": row["status"], "used": row["used"],
    } for row in rating_resolution["agencies"]]
    signals = []
    if issuer[2] and issuer[2].lower() != "green":
        signals.append(f"health_status={issuer[2]}: {issuer[3] or 'причина не указана'}")
    if bonds and bonds[0]["g_spread_bp"] is not None:
        signals.append(f"максимальный G-спред в последнем срезе: {bonds[0]['g_spread_bp']:.0f} б.п.")
    high_events = [event for event in events if event.get("severity") in {"high", "critical"}]
    if high_events:
        signals.append(f"событий высокой серьёзности за 30 дней: {len(high_events)}")
    return {
        "status": "found", "route": "current_risk", "issuer_id": issuer_id,
        "issuer_name": issuer[1], "as_of": latest, "health_status": issuer[2],
        "signals": signals, "bonds": bonds, "recent_events": events,
        "ratings": ratings, "rating_resolution": rating_resolution,
        "disclaimer": "Сводка построена из данных HyBond; подтверждайте выводы первичными документами.",
    }
