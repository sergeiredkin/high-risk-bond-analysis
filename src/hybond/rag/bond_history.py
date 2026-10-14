"""Read-only exact-bond market history summaries from HyBond snapshots."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

_ISIN = re.compile(r"\bRU[0-9A-Z]{10}\b", re.IGNORECASE)
_PERIOD_DAYS = {
    "7": 7, "30": 30, "90": 90, "180": 180, "365": 365,
}


def is_bond_trend_question(question: str, *, require_isin: bool = True) -> bool:
    """Require a market metric and historical intent, normally with an exact ISIN."""
    value = question.casefold().replace("ё", "е")
    trend = any(term in value for term in (
        "динамик", "изменил", "изменен", "истори", "за последн",
        "за месяц", "за квартал", "за год", "вырос", "снизил",
        "сравни за",
    ))
    metric = any(term in value for term in (
        "спред", "g-spread", "g spread", "доходност", "ytm", "цен",
    ))
    return trend and metric and (not require_isin or bool(_ISIN.search(question)))


def _window_days(question: str) -> int:
    value = question.casefold().replace("ё", "е")
    match = re.search(r"\b(7|30|90|180|365)\s*(?:дн|день|дня|дней)\b", value)
    if match:
        return _PERIOD_DAYS[match.group(1)]
    if "за год" in value:
        return 365
    if "за квартал" in value:
        return 90
    if "за месяц" in value:
        return 30
    return 30


def _metric(rows: list[sqlite3.Row], key: str, *, scale: float = 1.0,
            start_key: str = "start", end_key: str = "end",
            delta_key: str = "change") -> dict | None:
    available = [row for row in rows if row[key] is not None]
    if not available:
        return None
    first, last = available[0], available[-1]
    start, end = float(first[key]) * scale, float(last[key]) * scale
    return {
        start_key: round(start, 4), end_key: round(end, 4),
        delta_key: round(end - start, 4) if len(available) >= 2 else None,
        "start_date": first["snap_date"], "end_date": last["snap_date"],
        "observations": len(available),
    }


def bond_history_summary(con: sqlite3.Connection, identifier: str,
                         question: str = "") -> dict:
    """Compare available market snapshots in a bounded window, without advice.

    The window is anchored to this bond's latest stored snapshot rather than
    wall-clock today, so stale corpora are disclosed rather than silently
    yielding an empty result. Price changes are clean-price percentage points,
    not total returns. YTM/G-spread changes are suppressed if the calculation
    source changes inside the window.
    """
    value = identifier.strip().upper()
    if not value:
        return {"status": "identifier_required"}
    bond = con.execute(
        "SELECT secid,isin,short_name FROM bond WHERE upper(isin)=? OR upper(secid)=? LIMIT 1",
        (value, value),
    ).fetchone()
    if bond is None:
        return {"status": "not_found", "identifier": value,
                "message": "Облигация с таким ISIN/SECID не найдена в HyBond."}

    days = _window_days(question)
    dates = con.execute(
        """SELECT snap_date FROM spread_snapshot WHERE secid=?
           UNION SELECT snap_date FROM price_snapshot WHERE secid=?
           ORDER BY snap_date""",
        (bond["secid"], bond["secid"]),
    ).fetchall()
    if not dates:
        return {"status": "history_not_found", "bond": dict(bond), "window_days": days,
                "message": "В snapshot-таблицах нет истории по этому выпуску."}
    as_of = dates[-1]["snap_date"]
    try:
        cutoff = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    except ValueError:
        return {"status": "invalid_snapshot_date", "bond": dict(bond), "as_of": as_of}

    rows = con.execute(
        """WITH snapshot_dates AS (
               SELECT snap_date FROM spread_snapshot WHERE secid=? AND snap_date>=?
               UNION
               SELECT snap_date FROM price_snapshot WHERE secid=? AND snap_date>=?
           )
           SELECT d.snap_date,s.ytm,s.g_spread_bp,s.yield_source,
                  p.last_pct AS clean_price_pct
           FROM snapshot_dates d
           LEFT JOIN spread_snapshot s ON s.secid=? AND s.snap_date=d.snap_date
           LEFT JOIN price_snapshot p ON p.secid=? AND p.snap_date=d.snap_date
           ORDER BY d.snap_date""",
        (bond["secid"], cutoff, bond["secid"], cutoff,
         bond["secid"], bond["secid"]),
    ).fetchall()
    if not rows:
        return {"status": "history_not_found", "bond": dict(bond),
                "window_days": days, "as_of": as_of, "cutoff": cutoff}

    ytm = _metric(rows, "ytm", scale=100, start_key="start_pct",
                  end_key="end_pct", delta_key="change_pp")
    spread = _metric(rows, "g_spread_bp", start_key="start_bp",
                     end_key="end_bp", delta_key="change_bp")
    price = _metric(rows, "clean_price_pct", start_key="start_pct",
                    end_key="end_pct", delta_key="change_points")
    spread_rows = [row for row in rows if row["ytm"] is not None or row["g_spread_bp"] is not None]
    sources = sorted({row["yield_source"] for row in spread_rows if row["yield_source"]})
    source_is_unknown = any(row["yield_source"] is None for row in spread_rows)
    if len(sources) > 1 or source_is_unknown:
        note = "Источник расчёта доходности менялся или неизвестен; изменения доходности и G-спред не рассчитаны."
        if ytm:
            ytm["change_pp"] = None
            ytm["comparability_note"] = note
        if spread:
            spread["change_bp"] = None
            spread["comparability_note"] = note
    return {
        "status": "found", "route": "bond_trend", "bond": dict(bond),
        "window_days": days, "cutoff": cutoff, "as_of": as_of,
        "observation_dates": len(rows),
        "metrics": {
            "ytm": ytm,
            "g_spread": spread,
            "clean_price": price,
        },
        "yield_sources": sources,
        "disclaimer": (
            "Арифметическое сравнение доступных snapshot HyBond, не total return и не вывод о причине изменения. "
            "Доходность и G-спред могут зависеть от способа расчёта; сверяйте исходные даты и источник."
        ),
    }
