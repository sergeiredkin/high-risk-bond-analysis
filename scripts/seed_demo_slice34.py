"""Seed SYNTHETIC Slice 3 (events) and Slice 4 (financials/pillars/thesis) data
for every issuer in watchlist.json.

This exists because live Slice 3 needs a ChromeDriver + selenium scrape of
e-disclosure.ru, and live Slice 4 needs a paid parser-api.com key for ГИР БО —
neither is available in this environment. Rather than leave those parts of the
dashboard empty, this generates plausible demo data *consistent with the
risk_tier/rating/note already present in watchlist.json* so the full system
(all 4 slices) can be exercised end-to-end.

Every event, financial line-item, and thesis this script writes is synthetic.
The dashboard must visibly label anything sourced from this script as DEMO DATA
— it is not real ГИР БО financials or real e-disclosure filings.

Pillar SCORES are still computed by the real hybond.compute.pillars.compute_pillars
function — only the *inputs* (balance/income/cashflow line items) are synthetic.

Run once. Re-running overwrites (idempotent per issuer/period).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, "src")

from hybond.store import db  # noqa: E402
from hybond.compute import pillars as pillars_mod  # noqa: E402

PERIOD = "2025"
COMPUTED_ON = "2026-07-28"

# target ratios per risk tier -> synthetic РСБУ line items (thousands RUB)
TIER_PROFILE = {
    "anchor": dict(revenue=50_000_000, ebit_margin=0.18, net_margin=0.12,
                   debt_to_ebit=1.6, current_ratio=1.9, st_debt_share=0.25,
                   ocf_to_ebit=0.9),
    "core": dict(revenue=12_000_000, ebit_margin=0.15, net_margin=0.07,
                 debt_to_ebit=3.0, current_ratio=1.35, st_debt_share=0.42,
                 ocf_to_ebit=0.6),
    "watch": dict(revenue=6_000_000, ebit_margin=0.12, net_margin=0.03,
                  debt_to_ebit=4.5, current_ratio=1.05, st_debt_share=0.55,
                  ocf_to_ebit=0.35),
    "stress": dict(revenue=4_000_000, ebit_margin=0.06, net_margin=-0.06,
                   debt_to_ebit=7.5, current_ratio=0.7, st_debt_share=0.85,
                   ocf_to_ebit=-0.1),
}

# a couple of representative categories per tier, aligned with the taxonomy
# and with each issuer's own `note` in watchlist.json (not invented content —
# just structured into the event schema).
TIER_EVENTS = {
    "anchor": [("rating_action", "low", "market", "better",
                "Рейтинг подтверждён на прежнем уровне, прогноз стабильный.")],
    "core": [("rating_action", "medium", "market", "neutral",
              "Рейтинговое агентство подтвердило рейтинг, прогноз стабильный.")],
    "watch": [("operations", "medium", "operations", "worse",
               "Рост долговой нагрузки отмечен в отраслевом обзоре.")],
    "stress": [("reporting_delay", "high", "governance", "worse",
                "Задержка публикации отчётности, повышенное внимание рынка."),
               ("covenant", "critical", "leverage", "worse",
                "Признаки давления на ковенанты по данным биржевой отчётности.")],
}

THESIS_BY_TIER = {
    "anchor": "strengthening",
    "core": "holding",
    "watch": "holding",
    "stress": "weakening",
}


def synth_financials(tier: str) -> dict[int, float]:
    p = TIER_PROFILE[tier]
    revenue = p["revenue"]
    ebit = revenue * p["ebit_margin"]
    net_income = revenue * p["net_margin"]
    debt = ebit * p["debt_to_ebit"] if ebit > 0 else revenue * 0.5
    st_debt = debt * p["st_debt_share"]
    lt_debt = debt - st_debt
    short_term_liab = revenue * 0.30
    current_assets_v = short_term_liab * p["current_ratio"]
    cash = current_assets_v * 0.25
    equity = revenue * 0.6
    total_assets = debt + equity + revenue * 0.1
    ocf = ebit * p["ocf_to_ebit"]
    interest_expense = debt * 0.16  # ~ policy-rate-ish coupon load

    balance = {
        1150: total_assets * 0.4,
        1170: total_assets * 0.05,
        1210: current_assets_v * 0.4,
        1230: current_assets_v * 0.25,
        1240: cash * 0.1,
        1250: cash * 0.9,
        1200: current_assets_v,
        1300: equity,
        1400: lt_debt,
        1410: lt_debt,
        1500: short_term_liab,
        1510: st_debt,
        1520: max(short_term_liab - st_debt, 0),
        1600: total_assets,
    }
    fin = {
        2110: revenue,
        2120: revenue * (1 - p["ebit_margin"] - 0.15),
        2200: ebit,
        2330: interest_expense,
        2400: net_income,
    }
    cf = {4100: ocf}
    return balance, fin, cf


def main() -> None:
    random.seed(20260728)
    watchlist = json.loads(Path("watchlist.json").read_text(encoding="utf-8"))
    watchlist = {k: v for k, v in watchlist.items() if not k.startswith("_")}

    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.init_pillars(con)

    n_pillars = n_events = n_thesis = 0
    for issuer_id, meta in watchlist.items():
        tier = meta.get("risk_tier", "core")

        # --- Slice 4: synthetic financials -> real compute_pillars() ---
        balance, fin, cf = synth_financials(tier)
        pil = pillars_mod.compute_pillars(balance, fin, cf)
        payload = pil.to_json()
        payload["_synthetic"] = True
        payload["_source"] = "scripts/seed_demo_slice34.py (demo data, not real GIR BO)"
        db.upsert_financials(con, issuer_id=issuer_id, period=PERIOD,
                              fetched_on=COMPUTED_ON, has_consolidated=False,
                              has_audit=False,
                              payload_json=json.dumps({"balance": balance, "fin": fin, "cf": cf}))
        db.upsert_pillars(con, issuer_id=issuer_id, period=PERIOD,
                           computed_on=COMPUTED_ON, payload_json=json.dumps(payload, ensure_ascii=False))
        n_pillars += 1

        # --- Slice 3: synthetic classified events ---
        for category, severity, pillar, direction, note_ru in TIER_EVENTS.get(tier, []):
            db.insert_event(
                con, issuer_id=issuer_id, occurred=COMPUTED_ON,
                category=category, severity=severity,
                summary=f"[SYNTHETIC DEMO] {note_ru} (pillar={pillar}, direction={direction})",
                source_url="demo:seed_demo_slice34.py",
            )
            n_events += 1

        # --- Slice 4: thesis, seeded from the user's own watchlist note ---
        status = THESIS_BY_TIER.get(tier, "holding")
        db.upsert_thesis(
            con, issuer_id=issuer_id,
            thesis=meta.get("note", "")[:300] or f"{meta['name']}: тезис не задан",
            assumptions=f"Рейтинг {meta.get('rating', 'n/a')}, риск-уровень {tier}.",
            invalidation="пропуск купона; понижение рейтинга на 2+ ступени; "
                          "просрочка отчётности > 30 дней",
            status=status, updated=COMPUTED_ON,
        )
        n_thesis += 1

    con.commit()
    con.close()
    print(f"Seeded SYNTHETIC pillars for {n_pillars} issuers, "
          f"{n_events} events, {n_thesis} thesis records.")


if __name__ == "__main__":
    main()
