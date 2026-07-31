"""On-demand bond-discovery gap-check — periodic manual audit, not part of
daily_run.sh. Cross-checks each watchlist issuer's tracked `secids` against
every currently-traded MOEX series MOEX itself lists for that issuer's ИНН,
surfacing series the watchlist never picked up (new issues, restructured
series, anything missed at onboarding time).

Kept out of the daily pipeline deliberately: watchlist coverage drifts slowly
(new bond issues, not daily churn), so a live MOEX call per issuer belongs in
a manual `hybond discover` run, not the daily ingest path.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .ingest import sources


def discover_bonds_for_issuer(inn: str) -> list[dict[str, Any]]:
    """Every currently-traded MOEX SECID for one issuer's ИНН, with maturity
    and duration. Thin wrapper over sources.fetch_bonds_by_inn() -- that ИНН
    search (via /iss/securities.json, not the bond-master's ignored `q` param)
    is the same lookup already proven to surface every one of an issuer's
    outstanding series (e.g. all 34 of АФК Система's, cross-checked by hand)."""
    bonds = sources.fetch_bonds_by_inn(inn)
    if not bonds:
        return []
    secids = [b["SECID"] for b in bonds]
    md = {r["SECID"]: r for r in sources.fetch_marketdata(secids, snapshot=False)}
    today = date.today()
    out = []
    for b in bonds:
        mat = sources.parse_date(b.get("MATDATE"))
        years_to_maturity = (mat - today).days / 365.0 if mat else None
        dur_days = md.get(b["SECID"], {}).get("DURATION")
        mod_dur = dur_days / 365.0 if dur_days else None
        out.append({
            "secid": b["SECID"],
            "isin": b.get("ISIN"),
            "short_name": b.get("SHORTNAME"),
            "mat_date": b.get("MATDATE"),
            "years_to_maturity": years_to_maturity,
            "mod_dur": mod_dur,
        })
    return out


def run_gap_check(watchlist_path: Path) -> dict[str, Any]:
    """Per-issuer tracked-vs-MOEX-discovered comparison across the whole
    watchlist. `untracked` is what MOEX shows as outstanding for that ИНН
    that watchlist.json's `secids` list doesn't have."""
    wl = json.loads(Path(watchlist_path).read_text(encoding="utf-8"))
    issuers = [(k, v) for k, v in wl.items() if not k.startswith("_")]

    results = []
    for inn, meta in issuers:
        tracked = set(meta.get("secids", []))
        discovered = discover_bonds_for_issuer(inn)
        untracked = [d for d in discovered if d["secid"] not in tracked]
        untracked.sort(key=lambda d: d["mat_date"] or "9999-99-99")
        results.append({
            "issuer_id": inn,
            "name": meta.get("name"),
            "tracked_count": len(tracked),
            "discovered_count": len(discovered),
            "untracked": untracked,
        })

    return {
        "checked_on": date.today().isoformat(),
        "issuers_checked": len(results),
        "total_untracked": sum(len(r["untracked"]) for r in results),
        "issuers": results,
    }
