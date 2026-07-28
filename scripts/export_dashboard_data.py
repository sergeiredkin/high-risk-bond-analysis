"""Export everything the dashboard needs into one JSON file:
data/dashboard_data.json.

Sources combined:
  - watchlist.json           issuer metadata (name, sector, risk_tier, rating)
  - data/factlists/*.json    latest per-issuer factlist (bonds, flags, events,
                              six_pillars, thesis_status) — Slice 2 output
  - data/db/hybond.sqlite    full thesis text (assumptions/invalidation),
                              flag history for the sparkline
  - data/report.json         day-1 spread screen + maturity wall (Slice 1)

Does not compute anything new — pure aggregation for the dashboard renderer
(scripts/build_dashboard.py) to consume.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from hybond.compute.default_probs import RECOVERY_RATE, implied_fair_spread_bp, lookup  # noqa: E402
from hybond.store import db  # noqa: E402
from hybond import portfolio  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FACTLIST_DIR = ROOT / "data" / "factlists"
OUT_PATH = ROOT / "data" / "dashboard_data.json"

# Rating tiers the market map draws fair-value overlay lines for (spec: AA/A/BBB/BB).
FAIR_VALUE_TIERS = ["ruAA", "ruA", "ruBBB", "ruBB"]


def latest_factlists() -> dict[str, dict]:
    """One factlist per issuer_id: the most recent snap_date on disk."""
    best: dict[str, tuple[str, dict]] = {}
    for p in FACTLIST_DIR.glob("*.json"):
        issuer_id, snap_date = p.stem.rsplit("_", 1)
        data = json.loads(p.read_text(encoding="utf-8"))
        if issuer_id not in best or snap_date > best[issuer_id][0]:
            best[issuer_id] = (snap_date, data)
    return {k: v[1] for k, v in best.items()}


def flag_history(con) -> dict[str, list[dict]]:
    rows = con.execute(
        "SELECT issuer_id, snap_date, detector, signal, metric, detail "
        "FROM flag_log ORDER BY snap_date"
    ).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["issuer_id"]].append({
            "snap_date": r["snap_date"], "detector": r["detector"],
            "signal": r["signal"], "metric": r["metric"], "detail": r["detail"],
        })
    return out


def filing_counts_by_issuer(con) -> dict[str, dict]:
    rows = con.execute(
        "SELECT issuer_id, COUNT(*) total, SUM(classified) classified "
        "FROM filing GROUP BY issuer_id"
    ).fetchall()
    return {r["issuer_id"]: {"total": r["total"], "classified": r["classified"] or 0}
            for r in rows}


def spread_history_by_secid(con) -> dict[str, list[dict]]:
    rows = con.execute(
        "SELECT secid, snap_date, g_spread_bp, ytm FROM spread_snapshot "
        "ORDER BY snap_date"
    ).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["secid"]].append({
            "snap_date": r["snap_date"], "g_spread_bp": r["g_spread_bp"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
        })
    return out


def _nearest_snap_date_on_or_before(con, cutoff: str) -> str | None:
    row = con.execute(
        "SELECT snap_date FROM universe_snapshot WHERE snap_date<=? "
        "ORDER BY snap_date DESC LIMIT 1", (cutoff,)).fetchone()
    return row["snap_date"] if row else None


def _bonds_for_snap_date(con, snap_date: str, issuer_sectors: dict[str, str]) -> list[dict]:
    """One time-bucket of universe_snapshot, shaped for the screener AND the
    market-map tabs. Coupon type is a best-effort heuristic (COUPONPERCENT
    null -> floater), NOT a reliable classification -- confirmed false
    negatives exist (e.g. some known floaters report a current-period
    percent and read as 'fixed' here). Getting this right needs per-bond
    bondization, which universe_pipeline.py deliberately never calls.
    Surfaced in the UI as a caveat, not hidden.

    Market-map fields (pd_market/pd_rating/pd_gap/mod_dur): pd_market is
    the default probability implied by today's market price alone (same
    g_spread_bp -> PD inversion the mispricing signal already uses, just
    exposed as a probability instead of a spread); pd_rating is the
    agency-implied PD looked up from the same default_probs table
    mispricing_bp is built from, so pd_gap is arithmetically identical
    information to mispricing_bp -- just rescaled for the map's PD axis
    options. mod_dur reuses duration_moex (MOEX's own reported modified
    duration) -- this pipeline never computes duration from cashflows for
    universe-wide bonds, same tradeoff as the g_spread proxy itself.
    """
    rows = db.universe_snapshot_rows(con, snap_date)
    bonds = []
    for r in rows:
        signal_excluded = bool(r["signal_excluded"])
        rating = r["rating"]
        g_spread_bp = r["g_spread_bp"]
        # implied_fair_spread_bp/mispricing_bp are already NULL in the DB
        # when signal_excluded (universe_pipeline.py nulls them before
        # insert) -- but pd_market/pd_rating/pd_gap are recomputed here from
        # the still-present raw rating/g_spread_bp, so they need the same
        # exclusion applied explicitly or they'd silently un-exclude the
        # signal on the PD axis while the bp axis stays correctly excluded.
        pd_rating = lookup(rating)["yr1"] if (rating and not signal_excluded) else None
        pd_market = ((g_spread_bp / 10000.0) / (1 - RECOVERY_RATE)
                     if (g_spread_bp is not None and not signal_excluded) else None)
        pd_gap = (pd_market - pd_rating) if (pd_market is not None and pd_rating is not None) else None
        mod_dur = round(r["duration_moex"] / 365.0, 2) if r["duration_moex"] else None
        bonds.append({
            "secid": r["secid"],
            "short_name": r["short_name"],
            "emitent_title": r["emitent_title"],
            "emitent_inn": r["emitent_inn"],
            "sector": issuer_sectors.get(r["emitent_inn"]),
            "mat_date": r["mat_date"],
            "offer_date": r["offer_date"],
            "coupon_pct": r["coupon_pct"],
            "coupon_type": "fixed" if r["coupon_pct"] is not None else "floater",
            "list_level": r["list_level"],
            "qualified": bool(r["qualified"]),
            "last_pct": r["last_pct"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
            "g_spread_bp": round(g_spread_bp, 0) if g_spread_bp is not None else None,
            "duration_yrs": mod_dur,
            "mod_dur": mod_dur,
            "tenor_yrs": round(r["tenor_yrs"], 2) if r["tenor_yrs"] is not None else None,
            "rf_rate_pct": round(r["rf_rate"] * 100, 3) if r["rf_rate"] is not None else None,
            "volume": r["volume"],
            "num_trades": r["num_trades"],
            "yield_source": r["yield_source"],
            "rating": rating,
            "implied_fair_spread_bp": (round(r["implied_fair_spread_bp"], 0)
                                        if r["implied_fair_spread_bp"] is not None else None),
            "mispricing_bp": (round(r["mispricing_bp"], 0)
                              if r["mispricing_bp"] is not None else None),
            "pd_market": round(pd_market, 5) if pd_market is not None else None,
            "pd_rating": round(pd_rating, 5) if pd_rating is not None else None,
            "pd_gap": round(pd_gap, 5) if pd_gap is not None else None,
            "in_watchlist": bool(r["in_watchlist"]),
            "signal_excluded": signal_excluded,
            "aey_base_pct": round(r["aey_base"] * 100, 2) if r["aey_base"] is not None else None,
            "aey_stress_pct": (round(r["aey_stress"] * 100, 2)
                                if r["aey_stress"] is not None else None),
            "aey_bull_pct": round(r["aey_bull"] * 100, 2) if r["aey_bull"] is not None else None,
            "aey_base_risk_adj_pct": (round(r["aey_base_risk_adj"] * 100, 2)
                                       if r["aey_base_risk_adj"] is not None else None),
        })
    return bonds


def universe_snapshot_bundle(con) -> dict:
    """Three time-buckets of universe data for the market map's time toggle
    (Сейчас / -1 нед. / -1 мес.), plus the derived curve/fair-value overlays.

    universe_snapshot only has history for days `hybond universe` actually
    ran -- on a fresh install that's a single date. Each bucket falls back
    to the nearest available snap_date ON OR BEFORE its target cutoff (never
    a future date relative to the bucket's intent) and reports how many days
    stale that fallback is, so the dashboard can show an honest "no history
    yet" note instead of silently mislabeling today's data as a week old.
    """
    db.init_universe(con)
    latest = db.latest_universe_snap_date(con)
    if not latest:
        empty = {"snap_date": None, "bonds": []}
        return {"universe_now": empty, "universe_week_ago": empty,
                "universe_month_ago": empty, "rf_curve": [],
                "fair_value_bp_by_tier": {}}

    issuer_sectors = {r["issuer_id"]: r["sector"]
                      for r in con.execute("SELECT issuer_id, sector FROM issuer")}
    latest_d = date.fromisoformat(latest)
    earliest = con.execute(
        "SELECT MIN(snap_date) m FROM universe_snapshot").fetchone()["m"]

    buckets = {}
    for key, days_back in (("universe_now", 0), ("universe_week_ago", 7),
                           ("universe_month_ago", 30)):
        cutoff = (latest_d - timedelta(days=days_back)).isoformat()
        sd = _nearest_snap_date_on_or_before(con, cutoff)
        no_history = False
        if not sd:
            # Not enough accumulated history for this lookback yet -- fall
            # back to the oldest snapshot we do have rather than showing an
            # empty chart; if that's the same day as "now" (fresh install,
            # one day of data total), there's genuinely no comparison point.
            sd = earliest
            no_history = (sd == latest) if days_back > 0 else False
        stale_days = (date.fromisoformat(latest) - date.fromisoformat(sd)).days
        buckets[key] = {
            "snap_date": sd,
            "bonds": _bonds_for_snap_date(con, sd, issuer_sectors),
            "stale_days": stale_days,
            "no_history": no_history,
        }

    # rf_curve: derived from the (tenor_yrs, rf_rate) pairs already
    # interpolated per bond for today's run -- no extra network fetch,
    # always in sync with the curve actually used for today's G-spreads.
    seen = {}
    for b in buckets["universe_now"]["bonds"]:
        if b["tenor_yrs"] is not None and b["rf_rate_pct"] is not None:
            seen[round(b["tenor_yrs"], 1)] = b["rf_rate_pct"]
    rf_curve = [{"tenor_yrs": t, "rf_pct": r} for t, r in sorted(seen.items())]

    fair_value_bp_by_tier = {
        tier[2:]: round(implied_fair_spread_bp(tier), 0)
        for tier in FAIR_VALUE_TIERS
    }

    return {**buckets, "rf_curve": rf_curve,
            "fair_value_bp_by_tier": fair_value_bp_by_tier}


def universe_for_dashboard(con) -> dict:
    """Back-compat shape for the existing 'Универсум' screener tab (single
    latest snapshot, no market-map fields it doesn't use)."""
    db.init_universe(con)
    snap_date = db.latest_universe_snap_date(con)
    if not snap_date:
        return {"snap_date": None, "bonds": []}
    issuer_sectors = {r["issuer_id"]: r["sector"]
                      for r in con.execute("SELECT issuer_id, sector FROM issuer")}
    db.init_cbr(con)
    scenarios = portfolio.get_rate_scenarios(con)
    return {
        "snap_date": snap_date,
        "bonds": _bonds_for_snap_date(con, snap_date, issuer_sectors),
        "aey_source_date": scenarios["source_date"],
        "aey_is_fallback": scenarios["is_fallback"],
        "aey_source_note": (
            f"AEY рассчитан по данным заседания ЦБ от "
            f"{scenarios['source_date'][8:10]}.{scenarios['source_date'][5:7]}."
            f"{scenarios['source_date'][0:4]}"
            if not scenarios["is_fallback"]
            else "AEY рассчитан по консервативным значениям по умолчанию "
                 "(нет данных последнего заседания ЦБ)"
        ),
    }


def cbr_export(con) -> dict:
    """CBR key-rate meeting tracker, shaped for the dashboard's fourth tab.
    Reads cbr_meeting + cbr_meeting_diff only -- no computation happens
    here, `hybond cbr --fetch-all`/`--diff-all` (cbr_tracker.py) already did
    it. current_rate/next_meeting/cbr_2026_forecast/cbr_2027_forecast come
    straight from cbr_meeting's own fields (rate_after, rate_2026_mid,
    rate_2027_mid) rather than analysis_json's cbr_2026_avg_forecast/
    cbr_2027_avg_forecast, which still carry a previously-flagged factual
    mismatch against the real forecast documents (analysis_json was seeded
    verbatim from a user-authored draft that predates that correction) --
    cbr_meeting's own extracted fields are the verified source of truth."""
    from hybond import cbr_tracker

    db.init_db(con)
    db.init_cbr(con)
    db.init_cbr_diff(con)

    today = date.today().isoformat()
    schedule = cbr_tracker.CBR_MEETINGS_2026

    latest = con.execute(
        "SELECT * FROM cbr_meeting WHERE rate_after IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1").fetchone()
    latest = dict(latest) if latest else None

    next_meeting = next((m["date"] for m in schedule if m["date"] > today), None)

    def _normalise_range(s):
        """"14,5–14,61" -> "14.5-14.61" -- display-only normalisation to the
        dashboard's Latin-decimal convention; does NOT touch the trailing
        digit itself (see cbr_tracker.py's module docstring on the
        occasional fused-footnote-marker caveat -- that's a data-quality
        note for whoever reads the number, not something to silently trim)."""
        return s.replace(",", ".").replace("–", "-").replace("—", "-") if s else s

    # Deltas come from portfolio.get_rate_scenarios(con) -- the SAME live
    # computation universe_pipeline.py uses for per-bond AEY -- rather than
    # analysis_json's static aey_delta_rate_* fields. Reading two different
    # sources for what should be one number was a correctness bug: this
    # tab and the universe/portfolio view could show different scenario
    # deltas for the same meeting date. is_fallback is always surfaced
    # (never silently swallowed) so the dashboard can show a warning badge
    # instead of presenting a fallback constant as a real forecast-derived one.
    scenarios = portfolio.get_rate_scenarios(con)
    rate_scenarios = {
        "base_annual_bp": round(scenarios["base"] * 10000),
        "stress_annual_bp": round(scenarios["stress"] * 10000),
        "bull_annual_bp": round(scenarios["bull"] * 10000),
        "source_meeting": scenarios["source_date"],
        "cbr_2026_forecast": _normalise_range(scenarios["cbr_2026"]),
        "cbr_2027_forecast": _normalise_range(scenarios["cbr_2027"]),
        "is_fallback": scenarios["is_fallback"],
    }

    meetings = []
    for m in schedule:
        row = con.execute("SELECT * FROM cbr_meeting WHERE meeting_date=?",
                          (m["date"],)).fetchone()
        row = dict(row) if row else {}
        diff_row = con.execute("SELECT * FROM cbr_meeting_diff WHERE meeting_date=?",
                               (m["date"],)).fetchone()
        diff = None
        if diff_row:
            diff_row = dict(diff_row)
            diff = {
                "pace_change": diff_row.get("pace_change"),
                "forecast_2026_shift_bp": diff_row.get("forecast_2026_shift_bp"),
                "forecast_2027_shift_bp": diff_row.get("forecast_2027_shift_bp"),
                "portfolio_signal": diff_row.get("portfolio_signal"),
                "signal_magnitude": diff_row.get("signal_magnitude"),
                "tone_shift": diff_row.get("tone_shift"),
                "diff_note_ru": diff_row.get("diff_note_ru"),
            }
        meetings.append({
            "date": m["date"],
            "is_anchor": m["anchor"],
            "rate_after": row.get("rate_after"),
            "rate_change_bp": row.get("rate_change_bp"),
            "is_past": m["date"] <= today,
            "has_press": bool(row.get("press_release_text")),
            "has_forecast": bool(row.get("forecast_summary")),
            "press_release_excerpt": (row.get("press_release_text") or "")[:400] or None,
            "press_release_url": (f"https://cbr.ru/press/pr/?file="
                                   f"{m['date'][8:10]}{m['date'][5:7]}{m['date'][0:4]}_133000key.htm"
                                   if row.get("press_release_text") else None),
            "diff": diff,
        })

    recent_diffs = []
    for r in con.execute(
            "SELECT * FROM cbr_meeting_diff ORDER BY meeting_date DESC LIMIT 3").fetchall():
        r = dict(r)
        recent_diffs.append({
            "date": r["meeting_date"],
            "note": r.get("diff_note_ru"),
            "signal": (r.get("portfolio_signal") or "neutral").upper(),
            "signal_magnitude": r.get("signal_magnitude"),
        })

    return {
        "current_rate": latest.get("rate_after") if latest else None,
        "next_meeting": next_meeting,
        "rate_scenarios": rate_scenarios,
        "meetings": meetings,
        "recent_diffs": recent_diffs,
    }


def main() -> None:
    watchlist = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    watchlist = {k: v for k, v in watchlist.items() if not k.startswith("_")}

    report = json.loads((ROOT / "data" / "report.json").read_text(encoding="utf-8"))
    factlists = latest_factlists()

    con = db.connect()
    db.init_db(con)
    universe = universe_for_dashboard(con)
    market_map = universe_snapshot_bundle(con)
    cbr = cbr_export(con)
    fhist = flag_history(con)
    shist = spread_history_by_secid(con)
    fcounts = filing_counts_by_issuer(con)

    issuers = []
    for issuer_id, meta in watchlist.items():
        fl = factlists.get(issuer_id, {})
        thesis_row = db.get_thesis(con, issuer_id)
        thesis = None
        if thesis_row:
            thesis = {
                "thesis": thesis_row["thesis"],
                "assumptions": thesis_row["assumptions"],
                "invalidation": thesis_row["invalidation"],
                "status": thesis_row["status"],
                "updated": thesis_row["updated"],
            }
        bonds = fl.get("bonds", [])
        for b in bonds:
            b["spread_history"] = shist.get(b["secid"], [])

        issuers.append({
            "issuer_id": issuer_id,
            "name": meta.get("name"),
            "sector": meta.get("sector"),
            "risk_tier": meta.get("risk_tier", "core"),
            "rating": meta.get("rating"),
            "note": meta.get("note"),
            "edisclosure_id": meta.get("edisclosure_id"),
            "snap_date": fl.get("snap_date"),
            "bonds": bonds,
            "flags_today": fl.get("flags", []),
            "flag_history": fhist.get(issuer_id, []),
            "stat_warming_up": fl.get("stat_warming_up", True),
            "events": fl.get("events_since_last", []),
            "six_pillars": fl.get("six_pillars", {}),
            "thesis": thesis,
            "filing_counts": fcounts.get(issuer_id, {"total": 0, "classified": 0}),
        })
    con.close()

    out = {
        "generated_at": report.get("snap_date"),
        "day1_snap_date": report.get("snap_date"),
        "day2_snap_date": issuers[0]["snap_date"] if issuers else None,
        "spread_screen_day1": report.get("spread_screen", []),
        "maturity_wall": report.get("maturity_wall", []),
        "redemptions_by_issuer": report.get("redemptions_by_issuer", {}),
        "issuers": issuers,
        "universe": universe,
        "universe_now": market_map["universe_now"],
        "universe_week_ago": market_map["universe_week_ago"],
        "universe_month_ago": market_map["universe_month_ago"],
        "rf_curve": market_map["rf_curve"],
        "fair_value_bp_by_tier": market_map["fair_value_bp_by_tier"],
        "cbr": cbr,
        "data_notes": {
            "slice1_slice2": "Real data: MOEX ISS + CBR ZCYC (day 1), "
                              "synthetic jittered day 2 for diff-engine demo "
                              "(scripts/seed_day2_demo.py) — real trading day 2 "
                              "will replace this on the next live `hybond run`. "
                              "Floaters with a not-yet-fixed next coupon can't be "
                              "priced from cashflows (see pipeline.py) — those use "
                              "MOEX's own reported yield as a G-spread proxy instead "
                              "(marked '~'/'*' in the dashboard, yield_source='moex' "
                              "in spread_snapshot). A bond still shows no price only "
                              "if it had zero trades on the snapshot date.",
            "slice3": "All [SYNTHETIC DEMO] placeholder events have been removed "
                      "platform-wide (2026-07-27 cleanup). 7 issuers have real "
                      "filings scraped from e-disclosure.azipi.ru (see "
                      "filing_counts): ЕвроТранс, МГКЛ, Селигдар, Контрол Лизинг, "
                      "ЛК Европлан, ВУШ, Брусника — their events are real "
                      "classified filings only. ЕвроТранс's thesis_status=broken "
                      "is a real, human-confirmed verdict. The remaining 13 "
                      "issuers have no AZIPI coverage found (likely "
                      "e-disclosure.ru-only issuers, which this environment's IP "
                      "can't reach) and now show an honest empty event history "
                      "rather than synthetic placeholders.",
            "slice4": "Slice 4 financials: MIXED. 11 issuers have real ГИР БО "
                      "financials (fetched via parser-api.com, `hybond "
                      "financials`) feeding the real hybond.compute.pillars "
                      "module. The other 9 issuers' pillars are still fabricated "
                      "from risk_tier (scripts/seed_demo_slice34.py) pending a "
                      "successful ГИР БО fetch — check payload['_synthetic'] "
                      "before treating any issuer's six_pillars as real.",
        },
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(issuers)} issuers)")


if __name__ == "__main__":
    main()
