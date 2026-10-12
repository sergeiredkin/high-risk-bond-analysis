"""Read-only exact-instrument overview from HyBond bond/snapshot tables."""
from __future__ import annotations

import math
import re
import sqlite3

from hybond.ratings import resolve as resolve_rating

_ISIN = re.compile(r"\bRU[0-9A-Z]{10}\b", re.IGNORECASE)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def is_bond_brief_question(question: str) -> bool:
    """Recognize a profile request, not a document-search or metric query."""
    value = question.casefold().replace("ё", "е")
    intent = any(term in value for term in (
        "расскажи", "что известно", "профиль", "обзор", "сводка",
    ))
    if not intent:
        return False
    if _ISIN.search(question):
        return True
    explicit_bond_profile = (
        "профиль облигац", "обзор облигац", "сводка по облигац",
        "что известно об облигац", "расскажи об облигац",
        "расскажи о выпуске", "профиль выпуска", "обзор выпуска",
    )
    return any(term in value for term in explicit_bond_profile)


def bond_brief_summary(con: sqlite3.Connection, identifier: str) -> dict:
    """Return a compact exact-bond profile; do not estimate absent schedules."""
    value = identifier.strip().upper()
    if not value:
        return {"status": "identifier_required"}
    bond = con.execute(
        """SELECT b.secid,b.isin,b.issuer_id,b.short_name,b.mat_date,b.face_value,
                  b.face_unit,b.coupon_pct,b.list_level,b.qualified,b.updated,
                  b.issue_size,b.issue_size_placed,b.placement_pct,b.placement_partial_since,
                  i.name AS issuer_name,i.sector AS issuer_sector
           FROM bond b LEFT JOIN issuer i ON i.issuer_id=b.issuer_id
           WHERE upper(b.isin)=? OR upper(b.secid)=? LIMIT 1""",
        (value, value),
    ).fetchone()
    if bond is None:
        return {"status": "not_found", "identifier": value}
    result = dict(bond)
    row = con.execute(
        """SELECT snap_date,coupon_pct,coupon_period,ytm,yield_to_offer,mat_date,
                  offer_date,last_pct,volume,num_trades,duration_moex,mod_dur,
                  g_spread_bp,mispricing_bp,rf_rate,yield_source,yield_zone,
                  in_watchlist,vdo_scope,signal_excluded,health_status,health_source
           FROM universe_snapshot WHERE secid=? ORDER BY snap_date DESC LIMIT 1""",
        (result["secid"],),
    ).fetchone() if _table_exists(con, "universe_snapshot") else None
    if row:
        market = dict(row)
        market["ytm_pct"] = round(float(market["ytm"]) * 100, 2) if market["ytm"] is not None else None
        market["yield_to_offer_pct"] = (
            round(float(market["yield_to_offer"]) * 100, 2)
            if market["yield_to_offer"] is not None else None
        )
        market.pop("ytm", None)
        market.pop("yield_to_offer", None)
    else:
        market = None
    rating = resolve_rating(con, inn=result["issuer_id"], secid=result["secid"])
    return _json_safe({
        "status": "found",
        "route": "bond_brief",
        "bond": result,
        "market": market,
        "rating": rating,
        "issuer_context": {"issuer_id": result["issuer_id"],
                           "issuer_name": result["issuer_name"],
                           "sector": result["issuer_sector"]},
        "missing_coupon_schedule": "Следующая дата купона и график выплат не выводятся из этих snapshot-таблиц.",
        "disclaimer": "Рыночные метрики — snapshot HyBond на указанную дату; модельные поля и источник доходности показаны отдельно. Это не рекомендация и не замена первичным документам.",
    })
