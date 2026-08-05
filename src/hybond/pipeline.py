"""Slice 1 pipeline: ingest -> compute -> store -> spread screen + maturity wall.
No LLM anywhere in this file.

Flow:
  1. Load watchlist (issuer_id -> [secids]).
  2. Fetch bond master + marketdata + CBR curve (snapshot raw).
  3. For each watchlist bond: build cashflows, compute YTM/G-spread/duration.
  4. Persist bond descriptors + price + spread snapshots.
  5. Emit spread screen (rich/cheap ranking) and maturity wall.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .calendar.wall import build_wall, redemptions_by_issuer
from .compute import bondmath, cashflows, default_probs, expected_return
from .ingest import sources
from .store import db


def load_watchlist(path: Path) -> dict[str, list[str]]:
    """JSON: {"issuer_id": {"name": ..., "sector": ..., "secids": [...]}}

    Top-level keys starting with "_" (e.g. "_comment", "_meta") are metadata,
    not issuers, and are skipped.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def run(watchlist_path: Path, snap_date: date | None = None) -> dict:
    snap_date = snap_date or date.today()
    wl = load_watchlist(watchlist_path)
    secids = [s for v in wl.values() for s in v["secids"]]

    master = {r["SECID"]: r for r in sources.fetch_bond_master()}
    md = {r["SECID"]: r for r in sources.fetch_marketdata(secids)}
    curve = sources.fetch_cbr_zcyc()

    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.init_expected_return(con)

    screen_rows = []
    flows_by_issuer: dict[str, list] = {}

    for issuer_id, v in wl.items():
        db.upsert_issuer(con, issuer_id, v["name"], v.get("sector"))
        for secid in v["secids"]:
            m = master.get(secid)
            q = md.get(secid)
            if not m or not q or q.get("LAST") in (None, 0):
                continue
            face = float(m.get("FACEVALUE") or 1000.0)
            mat = sources.parse_date(m.get("MATDATE"))
            # fetch_bond_master()'s ISQUALIFIEDINVESTORS is always dropped by
            # MOEX (see its docstring) -- fetch the real flag per-security
            # instead. Only ~25-30 calls here (watchlist size), not viable
            # at universe scale (~1000 bonds), see fetch_security_qualified.
            qualified = sources.fetch_security_qualified(secid)
            db.upsert_bond(
                con, secid=secid, isin=m.get("ISIN"), issuer_id=issuer_id,
                short_name=m.get("SHORTNAME"), mat_date=mat.isoformat() if mat else None,
                face_value=face, face_unit=m.get("FACEUNIT"),
                coupon_pct=m.get("COUPONPERCENT"), list_level=m.get("LISTLEVEL"),
                qualified=1 if qualified else 0,
                updated=snap_date.isoformat(),
            )
            db.insert_price_snapshot(
                con, secid=secid, snap_date=snap_date.isoformat(),
                last_pct=q.get("LAST"), yield_moex=q.get("YIELD"),
                duration=q.get("DURATION"), volume=q.get("VALTODAY"),
                num_trades=q.get("NUMTRADES"), systime=q.get("SYSTIME"),
            )

            bz = sources.fetch_bondization(secid)
            coupons = [(sources.parse_date(c.get("coupondate")), c.get("value"))
                       for c in bz["coupons"]]
            coupons = [(d, val) for d, val in coupons if d]
            amorts = [(sources.parse_date(a.get("amortdate") or a.get("coupondate")),
                       a.get("value")) for a in bz["amortizations"]]
            amorts = [(d, val) for d, val in amorts if d]

            # Floaters: MOEX leaves `value` null for coupons whose rate isn't
            # fixed yet. build_cashflows correctly drops those (never fabricate
            # a value), but that silently turns "unknown coupon" into "zero
            # coupon" for YTM purposes — which can swing YTM deeply negative
            # for a perfectly healthy bond. Use MOEX's own reported yield as a
            # proxy instead of computing one from an incomplete schedule.
            is_floater = any(d > snap_date and val is None for d, val in coupons)

            if is_floater:
                yld = q.get("YIELD")
                if yld is None or not mat:
                    continue
                ytm = float(yld) / 100.0
                tenor = (mat - snap_date).days / 365.0
                rf = bondmath.interp_curve(tenor, curve)
                gsp = (ytm - rf) * 1e4
                dur_days = q.get("DURATION")
                mdur = dur_days / 365.0 if dur_days else None
                cvx = None
                yield_source = "moex"
            else:
                flows = cashflows.build_cashflows(coupons, amorts, snap_date, face)
                if not flows:
                    continue
                flows_by_issuer.setdefault(issuer_id, []).extend(
                    (f.when, f.amount, f.kind) for f in flows
                )
                pairs = cashflows.to_pairs(flows, snap_date)
                dirty = float(q["LAST"]) / 100.0 * face  # approx: clean~dirty for screen
                try:
                    ytm = bondmath.ytm_from_price(dirty, pairs)
                    tenor = (cashflows.final_maturity(flows) - snap_date).days / 365.0
                    rf = bondmath.interp_curve(tenor, curve)
                    gsp = (ytm - rf) * 1e4
                    mdur = bondmath.modified_duration(ytm, pairs)
                    cvx = bondmath.convexity(ytm, pairs)
                except (ValueError, ZeroDivisionError):
                    continue
                yield_source = "computed"

            db.insert_spread_snapshot(
                con, secid=secid, snap_date=snap_date.isoformat(), ytm=ytm,
                tenor_yrs=tenor, rf_rate=rf, g_spread_bp=gsp, mod_dur=mdur,
                convexity=cvx, yield_source=yield_source,
            )
            # Default Probability Layer (see DEFAULT_PROB_FEATURE.md): rating
            # comes from the `rating` table (db.current_rating), the single
            # source of truth shared with factlist/assemble.py -- not the
            # watchlist dict directly. None for issuers with no current
            # rating row, or whose rating string doesn't parse against the
            # ruXX table; lookup() degrades gracefully rather than raising.
            rating = db.current_rating(con, issuer_id)
            implied_fair = default_probs.implied_fair_spread_bp(rating or "")
            misprice = (gsp - implied_fair) if implied_fair is not None else None

            # Part B: survival-weighted expected-return IRR at 1yr/2yr/to-
            # maturity (compute/expected_return.py). Only possible for
            # non-floaters -- the real coupon/amortization schedule (`coupons`,
            # `amorts`, `dirty`) only exists in this branch; a floater has no
            # fixed future schedule to survival-weight.
            if yield_source == "computed":
                for label, horizon in (("1y", 1.0), ("2y", 2.0), ("maturity", None)):
                    proj = expected_return.project_expected_return(
                        coupons, amorts, snap_date, face, dirty, rating, horizon)
                    if proj is None:
                        continue
                    db.insert_expected_return_snapshot(
                        con, secid=secid, snap_date=snap_date.isoformat(),
                        horizon_label=label, horizon_yr=proj["horizon_yr"],
                        irr_pct=proj["irr_pct"],
                        undiscounted_npv=proj["undiscounted_npv"],
                        undiscounted_total_cashflow=proj["undiscounted_total_cashflow"],
                        horizon_capped_by_maturity=int(proj["horizon_capped_by_maturity"]),
                        uses_extrapolated_pd=int(proj["uses_extrapolated_pd"]),
                    )

            screen_rows.append({
                "secid": secid, "issuer": v["name"], "ytm_pct": round(ytm * 100, 2),
                "g_spread_bp": round(gsp, 0),
                "mod_dur": round(mdur, 2) if mdur is not None else None,
                "tenor_yrs": round(tenor, 2), "yield_source": yield_source,
                "implied_fair_spread_bp": round(implied_fair, 0) if implied_fair is not None else None,
                "mispricing_bp": round(misprice, 0) if misprice is not None else None,
            })

    con.commit()
    con.close()

    screen_rows.sort(key=lambda r: r["g_spread_bp"], reverse=True)
    wall = build_wall(flows_by_issuer, today=snap_date)
    redemptions = redemptions_by_issuer(flows_by_issuer, today=snap_date)

    return {
        "snap_date": snap_date.isoformat(),
        "spread_screen": screen_rows,
        "maturity_wall": [
            {"month": b.month, "principal": round(b.principal, 0),
             "coupon": round(b.coupon, 0), "n_issuers": len(b.issuers)}
            for b in wall
        ],
        "redemptions_by_issuer": {
            k: [(d.isoformat(), round(a, 0)) for d, a in v]
            for k, v in redemptions.items()
        },
    }
