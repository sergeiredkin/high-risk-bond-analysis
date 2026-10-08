"""Resolve human-entered company names to HyBond issuer IDs."""
from __future__ import annotations


def resolve_company(con, name: str, limit: int = 10) -> list[dict]:
    """Return issuer candidates using exact-then-substring matching."""
    value = name.strip()
    if not value:
        return []
    rows = con.execute(
        "SELECT issuer_id, name, watchlist_tier FROM issuer ORDER BY watchlist_tier, name"
    ).fetchall()
    def normalize(text: str) -> str:
        """Normalize Russian case and ё/е spelling variants."""
        return text.casefold().replace("ё", "е")

    folded = normalize(value)
    exact = [row for row in rows if normalize(row[1]) == folded]
    matches = exact or [row for row in rows if folded in normalize(row[1])]
    return [{"issuer_id": row[0], "name": row[1]} for row in matches[:limit]]
