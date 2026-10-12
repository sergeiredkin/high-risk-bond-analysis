"""Read-only, source-aware issuer overview from existing HyBond tables."""
from __future__ import annotations

import json
import math
import sqlite3

from hybond.ratings import resolve as resolve_rating

from .financial_reports import financial_report_summary


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


_RATIO_KEYS = (
    "debt_to_ebit", "interest_coverage", "debt_to_assets", "current_ratio",
    "cash_to_short_term_debt", "short_term_debt_share", "net_margin", "ocf_to_debt",
)


def is_issuer_brief_question(question: str) -> bool:
    """Recognize an explicit request for a compact issuer dossier."""
    value = question.casefold().replace("ё", "е")
    markers = (
        "расскажи о", "расскажи об", "что известно о", "что известно об",
        "профиль эмитент", "обзор эмитент",
        "досье эмитент", "сводка по эмитент", "дай обзор по эмитент",
        "собери информацию об эмитент", "финансовые показатели эмитент",
        "финансовое состояние эмитент", "финансовый профиль", "financial metrics",
        "issuer profile",
    )
    return any(marker in value for marker in markers)


def issuer_brief_summary(con: sqlite3.Connection, issuer_id: str,
                         *, bond_limit: int = 12, event_limit: int = 8,
                         filing_limit: int = 5) -> dict:
    """Return available facts with their as-of dates and source classes.

    This is an SQL dossier, not a generated credit opinion. Events and news
    remain labeled with their source/review status; missing data stays absent.
    """
    issuer_columns = {row["name"] for row in con.execute("PRAGMA table_info(issuer)")}
    selected_columns = [
        name for name in (
            "issuer_id", "name", "sector", "active", "watchlist_tier", "needs_review",
            "needs_review_reason", "health_status", "health_reason",
        ) if name in issuer_columns
    ]
    issuer_row = con.execute(
        f"SELECT {','.join(selected_columns)} FROM issuer WHERE issuer_id=?", (issuer_id,)
    ).fetchone()
    if issuer_row is None:
        return {"status": "not_found", "issuer_id": issuer_id}
    issuer = dict(issuer_row)

    snapshot_date = con.execute(
        "SELECT MAX(snap_date) FROM universe_snapshot WHERE emitent_inn=?",
        (issuer_id,),
    ).fetchone()[0] if _table_exists(con, "universe_snapshot") else None
    bond_count = watchlist_count = vdo_count = 0
    bonds = []
    if snapshot_date:
        counts = con.execute(
            """SELECT COUNT(DISTINCT secid),COUNT(DISTINCT CASE WHEN in_watchlist=1 THEN secid END),
                      COUNT(DISTINCT CASE WHEN vdo_scope=1 THEN secid END)
               FROM universe_snapshot WHERE emitent_inn=? AND snap_date=?""",
            (issuer_id, snapshot_date),
        ).fetchone()
        bond_count, watchlist_count, vdo_count = counts
        rows = con.execute(
            """SELECT u.secid,b.isin,COALESCE(u.short_name,b.short_name) AS short_name,
                      u.mat_date,u.offer_date,u.coupon_pct,u.ytm,u.yield_to_offer,
                      u.g_spread_bp,u.mispricing_bp,u.last_pct,u.volume,u.num_trades,
                      u.mod_dur,u.yield_source,u.in_watchlist,u.vdo_scope,u.health_status
               FROM universe_snapshot u LEFT JOIN bond b ON b.secid=u.secid
               WHERE u.emitent_inn=? AND u.snap_date=?
               ORDER BY u.g_spread_bp DESC,u.ytm DESC,u.secid LIMIT ?""",
            (issuer_id, snapshot_date, bond_limit),
        ).fetchall()
        for row in rows:
            item = dict(row)
            ytm = item.pop("ytm")
            yto = item.pop("yield_to_offer")
            item["ytm_pct"] = round(float(ytm) * 100, 2) if ytm is not None else None
            item["yield_to_offer_pct"] = round(float(yto) * 100, 2) if yto is not None else None
            rating = resolve_rating(con, inn=issuer_id, secid=item["secid"])
            item["rating"] = rating["grade"]
            item["rating_source"] = rating["source"]
            item["rating_stale"] = rating["stale"]
            bonds.append(item)

    rating = resolve_rating(con, inn=issuer_id)
    recent_events = [dict(row) for row in con.execute(
        """SELECT occurred,category,severity,summary,source_url,confidence,
                  classified_by,needs_review
           FROM event WHERE issuer_id=? AND category<>'routine' AND severity<>'noise'
           ORDER BY occurred DESC,event_id DESC LIMIT ?""",
        (issuer_id, event_limit),
    ).fetchall()] if _table_exists(con, "event") else []
    rating_changes = [dict(row) for row in con.execute(
        """SELECT detected_on,rated_on,agency,kind,old_grade,new_grade,severity,source
           FROM rating_change WHERE inn=? ORDER BY detected_on DESC,change_id DESC LIMIT 5""",
        (issuer_id,),
    ).fetchall()] if _table_exists(con, "rating_change") else []
    news_leads = [dict(row) for row in con.execute(
        """SELECT occurred,source,category,severity,headline,url,confidence
           FROM market_note WHERE issuer_id=? AND severity IN ('high','critical')
           ORDER BY occurred DESC,note_id DESC LIMIT 5""",
        (issuer_id,),
    ).fetchall()] if _table_exists(con, "market_note") else []
    filings = [dict(row) for row in con.execute(
        """SELECT published,title,url FROM filing WHERE issuer_id=?
           AND url LIKE 'https://e-disclosure.azipi.ru/messages/%'
           ORDER BY published DESC,title,url LIMIT ?""",
        (issuer_id, filing_limit),
    ).fetchall()] if _table_exists(con, "filing") else []

    pillar = con.execute(
        """SELECT period,computed_on,payload FROM pillars_snapshot
           WHERE issuer_id=? ORDER BY CAST(period AS INTEGER) DESC,computed_on DESC LIMIT 1""",
        (issuer_id,),
    ).fetchone() if _table_exists(con, "pillars_snapshot") else None
    financials = None
    if pillar:
        try:
            payload = json.loads(pillar["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        statement = con.execute(
            """SELECT fetched_on,has_consolidated,has_audit,payload
               FROM financials_snapshot WHERE issuer_id=? AND period=?
               ORDER BY fetched_on DESC LIMIT 1""",
            (issuer_id, pillar["period"]),
        ).fetchone() if _table_exists(con, "financials_snapshot") else None
        statement_payload = {}
        if statement:
            try:
                statement_payload = json.loads(statement["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
        financial_doc = con.execute(
            """SELECT doc_sha256,source_url,local_path,authority_level
               FROM rag_documents WHERE issuer_inn=? AND doc_type='nalog_financials'
               ORDER BY local_path DESC LIMIT 1""",
            (issuer_id,),
        ).fetchone() if _table_exists(con, "rag_documents") else None
        financials = {
            "period": pillar["period"],
            "computed_on": pillar["computed_on"],
            "actual_date": statement_payload.get("actual_date"),
            "fetched_on": statement["fetched_on"] if statement else None,
            "has_consolidated": bool(statement["has_consolidated"]) if statement else None,
            "has_audit": bool(statement["has_audit"]) if statement else None,
            "ratios": {key: payload.get(key) for key in _RATIO_KEYS if key in payload},
            "source": "HyBond pillars_snapshot from РСБУ statement data; ratios are not consolidated unless explicitly stated.",
            "source_document": {
                "doc_sha256": financial_doc["doc_sha256"],
                "source_url": financial_doc["source_url"]
                if (financial_doc["source_url"] or "").startswith("https://bo.nalog.gov.ru/")
                else None,
                "source_title": financial_doc["local_path"].rsplit("/", 1)[-1],
                "source_path": financial_doc["local_path"],
                "authority_level": financial_doc["authority_level"],
            } if financial_doc else None,
        }
        financials["report_metadata"] = financial_report_summary(con, issuer_id, limit=5)

    return _json_safe({
        "status": "found",
        "route": "issuer_brief",
        "issuer": dict(issuer),
        "market": {
            "as_of": snapshot_date,
            "bond_count": bond_count,
            "watchlist_count": watchlist_count,
            "vdo_scope_count": vdo_count,
            "bonds_ordered_by_g_spread_desc": bonds,
            "source": "HyBond universe_snapshot; YTM values converted from stored fractions to percent.",
        },
        "rating": rating,
        "financials": financials,
        "recent_events": recent_events,
        "rating_changes": rating_changes,
        "secondary_news_leads": news_leads,
        "official_filing_metadata": filings,
        "disclaimer": "Сводка из существующих таблиц HyBond, не кредитное заключение. События могут быть классифицированы автоматически; записи каталога содержат метаданные, а не проверенный текст. Сверяйте существенные выводы с первичными источниками.",
    })
