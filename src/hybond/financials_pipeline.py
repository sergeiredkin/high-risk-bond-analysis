"""Slice 4 financials pipeline: fetch ГИР БО financials by ИНН -> compute the six
pillars deterministically -> store both. No LLM here — the scorecard NARRATIVE is
the skill's job; this produces the auditable numbers it explains.

Uses issuer_id as ИНН (the watchlist convention). parser-api backend by default
(200 free req/month covers an annual watchlist); direct bo.nalog backend is a
documented stub.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .compute import pillars as P
from .ingest import nalog
from .store import db


def load_watchlist(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_financials(
    watchlist_path: Path,
    api_key: str | None = None,
    snap_date: date | None = None,
) -> dict:
    snap_date = snap_date or date.today()
    api_key = api_key or os.environ.get("PARSER_API_KEY")
    if not api_key:
        return {"error": "no PARSER_API_KEY set and none passed; "
                         "financials backend needs a key (200/mo free tier)"}
    wl = load_watchlist(watchlist_path)
    backend = nalog.ParserApiBackend(api_key)

    con = db.connect()
    db.init_db(con)
    db.init_pillars(con)

    summary = {"snap_date": snap_date.isoformat(), "issuers": {}, "errors": {}}

    for issuer_id, v in wl.items():
        if issuer_id.startswith("_"):
            continue
        # issuer_id is the ИНН by convention
        try:
            rec = nalog.fetch_financials(issuer_id, backend)
        except Exception as e:  # noqa: BLE001
            summary["errors"][issuer_id] = str(e)
            continue
        if rec is None:
            summary["errors"][issuer_id] = "no financials found on ГИР БО"
            continue

        db.upsert_issuer(con, issuer_id, v.get("name", rec.org_name or issuer_id),
                        v.get("sector"))
        db.upsert_financials(
            con, issuer_id=issuer_id, period=rec.period,
            fetched_on=snap_date.isoformat(),
            has_consolidated=rec.has_consolidated, has_audit=rec.has_audit,
            payload_json=json.dumps(rec.to_json(), ensure_ascii=False))

        pil = P.compute_pillars(rec.balance, rec.financial_result,
                                rec.funds_movement, rec.balance_prev)
        pj = pil.to_json()
        # surface the consolidated-coverage caveat into the pillars payload
        if rec.has_consolidated and not pj.get("coverage_note"):
            pj["coverage_note"] = ("issuer has consolidated statements (has_ks); "
                                   "single-entity РСБУ used — group debt may be understated")

        # Leasing companies fold interest into cost-of-leasing (РСБУ 2330 = 0),
        # not a separate line -- debt_to_ebit/interest_coverage are computed
        # from that same zero and read as misleadingly clean. Flag it rather
        # than ship a leverage score that looks great for the wrong reason.
        if v.get("sector") == "leasing" and pil.inputs.interest_expense == 0:
            pj["sector_note"] = (
                "Лизинговая компания: РСБУ код 2330 (проценты к уплате) = 0 — "
                "процентные расходы учитываются в составе себестоимости лизинга, "
                "а не отдельной строкой. debt_to_ebit и interest_coverage "
                "недостоверны для данного сектора. Ключевые метрики: "
                "ocf_to_debt и short_term_debt_share."
            )
            pj["coverage_note"] = (
                "Leasing sector: interest_expense=0 in РСБУ — "
                "debt_to_ebit/interest_coverage unreliable. "
                "Use ocf_to_debt and short_term_debt_share instead."
            )
        db.upsert_pillars(con, issuer_id=issuer_id, period=rec.period,
                          computed_on=snap_date.isoformat(),
                          payload_json=json.dumps(pj, ensure_ascii=False))

        summary["issuers"][issuer_id] = {
            "period": rec.period,
            "has_consolidated": rec.has_consolidated,
            "scores": {k: pj.get(k) for k in
                       ("score_leverage", "score_liquidity",
                        "score_refinancing", "score_operations")},
            "debt_to_ebit": pj.get("debt_to_ebit"),
            "coverage_note": pj.get("coverage_note", ""),
        }

    con.commit()
    con.close()
    return summary
