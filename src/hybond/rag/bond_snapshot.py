"""Non-generative SQL answers for current bond metrics with explicit dates."""
from __future__ import annotations

import re

_ISIN = re.compile(r"\bRU[0-9A-Z]{10}\b", re.IGNORECASE)


def answer_bond_snapshot(con, question: str, *, isin: str | None = None) -> dict | None:
    """Return requested bond metrics only when an exact instrument is identified.

    Values come from the latest stored universe snapshot. Missing fields remain
    null; this helper never estimates coupon dates or fills values from prose.
    """
    query = question.casefold()
    instrument = isin or next(iter(_ISIN.findall(question)), None)
    if not instrument:
        return None

    requested: list[str] = []
    if any(term in query for term in ("купон", "coupon")):
        requested.extend(("coupon_rate_pct", "coupon_period_days"))
    if any(term in query for term in ("ytm", "доходност", "yield")):
        requested.append("ytm_pct")
    if any(term in query for term in ("погашен", "maturity")):
        requested.append("maturity_date")
    if any(term in query for term in ("оферт", "offer")):
        requested.append("offer_date")
    if any(term in query for term in ("g-spread", "спред", "spread")):
        requested.append("g_spread_bp")
    if any(term in query for term in ("следующ", "next")) and any(
        term in query for term in ("выплат", "купон", "payment")
    ):
        requested.append("next_coupon_date")
    if not requested:
        return None

    table_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_snapshot'"
    ).fetchone()
    if not table_exists:
        return None

    bond = con.execute(
        "SELECT secid,isin,short_name FROM bond WHERE isin=? OR secid=? LIMIT 1",
        (instrument.upper(), instrument.upper()),
    ).fetchone()
    secid = bond["secid"] if bond else instrument.upper()
    row = con.execute(
        """SELECT secid,snap_date,coupon_pct,coupon_period,ytm,mat_date,offer_date,
                  g_spread_bp,yield_source
           FROM universe_snapshot WHERE secid=? ORDER BY snap_date DESC LIMIT 1""",
        (secid,),
    ).fetchone()
    if row is None:
        return None

    values = {
        "coupon_rate_pct": row["coupon_pct"],
        "coupon_period_days": row["coupon_period"],
        "ytm_pct": round(float(row["ytm"]) * 100, 2) if row["ytm"] is not None else None,
        "maturity_date": row["mat_date"],
        "offer_date": row["offer_date"],
        "g_spread_bp": row["g_spread_bp"],
        "next_coupon_date": None,
    }
    labels = {
        "coupon_rate_pct": "ставка купона",
        "coupon_period_days": "период купона",
        "ytm_pct": "доходность к погашению",
        "maturity_date": "дата погашения",
        "offer_date": "дата оферты",
        "g_spread_bp": "G-спред",
        "next_coupon_date": "дата следующей выплаты",
    }
    missing = [labels[key] for key in requested if values[key] is None]
    available = {key: values[key] for key in requested if values[key] is not None}
    return {
        "status": "partial" if missing else "found",
        "source": "HyBond universe_snapshot; не цитата из документа",
        "as_of": row["snap_date"],
        "secid": row["secid"],
        "isin": bond["isin"] if bond else instrument.upper(),
        "short_name": bond["short_name"] if bond else None,
        "values": available,
        "missing_fields": missing,
        "yield_source": row["yield_source"],
    }
