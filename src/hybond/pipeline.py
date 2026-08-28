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
import logging
from datetime import date
from pathlib import Path

import requests

from .calendar.wall import build_wall, redemptions_by_issuer
from .compute import bondmath, cashflows, default_probs, expected_return
from .health import log_health, log_health_once_today
from .ingest import sources
from .store import db

logger = logging.getLogger(__name__)

# Garbage-vs-real distinction for a floater's MOEX-reported YIELD (see the
# is_floater branch in run() below) -- deliberately much looser than
# rank_candidates()'s own 0.80 ranking sanity bound, since a real ВДО
# distress bond can legitimately show 30-45% YTM; this only rejects values
# no real bond could ever have (0%, or hundreds of thousands of percent).
FLOATER_YIELD_SANITY_MAX = 5.0
FLOATER_YIELD_SANITY_MIN = -0.50  # -50% -- below this is garbage data


def load_watchlist(path: Path) -> dict[str, list[str]]:
    """JSON: {"issuer_id": {"name": ..., "sector": ..., "secids": [...]}}

    Top-level keys starting with "_" (e.g. "_comment", "_meta") are metadata,
    not issuers, and are skipped.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def run(watchlist_path: Path | None, snap_date: date | None = None) -> dict:
    """Issuer scope is `issuer` WHERE watchlist_tier IN (1, 2) -- tier=1
    (active watchlist) and tier=2 (passive monitoring) both get identical
    data collection, per the Watchlist Tier System task. watchlist.json is
    now a fallback enrichment source only: if it exists, any secid listed
    there gets folded into issuer_bond (the persistent secid source of
    truth) alongside whatever universe_snapshot/bond already know -- it's
    no longer what drives the loop. Its absence does not raise; the
    pipeline just has one fewer source to union that run."""
    snap_date = snap_date or date.today()
    wl = {}
    if watchlist_path is not None and Path(watchlist_path).exists():
        wl = load_watchlist(watchlist_path)

    con = db.connect()
    db.init_db(con)
    db.init_rating(con)
    db.init_expected_return(con)
    db.init_issuer_bond(con)
    db.sync_issuer_bond(con, wl)

    issuer_rows = db.list_issuers_by_tier(con, (1, 2))
    issuer_secids = {r["issuer_id"]: db.list_bonds_for_issuer(con, r["issuer_id"])
                      for r in issuer_rows}
    secids = [s for lst in issuer_secids.values() for s in lst]

    master = {r["SECID"]: r for r in sources.fetch_bond_master()}
    db.refresh_delisted_flags(con, set(master.keys()))
    md = {r["SECID"]: r for r in sources.fetch_marketdata(secids)}
    if secids and not md:
        # health logging
        log_health(con, 'moex', 'ingest', 'error', 'MOEX_WATCHLIST_EMPTY',
                   f'fetch_marketdata() returned 0 rows for {len(secids)} '
                   f'watchlist secids on {snap_date.isoformat()}')
    curve = sources.fetch_cbr_zcyc()

    screen_rows = []
    flows_by_issuer: dict[str, list] = {}
    # Per-bond mirror of flows_by_issuer, same (when, amount, kind) tuples --
    # feeds maturity_wall_by_secid below (Task: portfolio-level coupon/
    # maturity-wall aggregate in the dashboard's Портфель tab, which needs
    # each SELECTED bond's own real cashflow timing, not just the
    # per-issuer aggregate the existing watchlist-wide wall uses -- an
    # issuer with 2 series in the portfolio would otherwise double up or
    # misattribute cashflows if only one series is actually held). Same
    # floater exclusion as flows_by_issuer (floaters never populate `flows`
    # at all, see the `else` branch below -- not a new gap this introduces).
    flows_by_secid: dict[str, list] = {}

    for row in issuer_rows:
        issuer_id, name, sector = row["issuer_id"], row["name"], row["sector"]
        db.upsert_issuer(con, issuer_id, name, sector)
        for secid in issuer_secids[issuer_id]:
            m = master.get(secid)
            q = md.get(secid)
            if not m:
                continue
            face = float(m.get("FACEVALUE") or 1000.0)
            mat = sources.parse_date(m.get("MATDATE"))
            # fetch_bond_master()'s ISQUALIFIEDINVESTORS is always dropped by
            # MOEX (see its docstring) -- fetch the real flag per-security
            # instead. Only ~25-30 calls here (watchlist size), not viable
            # at universe scale (~1000 bonds), see fetch_security_qualified.
            try:
                qualified = sources.fetch_security_qualified(secid)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning("network error fetching qualified-investor flag for %s: %s", secid, e)
                qualified = None
            # Registered-vs-placed volume: same fetch_bond_master() call, two
            # more columns (see sources.py) -- no new endpoint. issue_size is
            # the registered size, issue_size_placed how much has actually
            # sold so far; a bond still in book-building sits below 100%.
            issue_size = m.get("ISSUESIZE")
            issue_size_placed = m.get("ISSUESIZEPLACED")
            placement_pct = (round(issue_size_placed / issue_size * 100, 2)
                             if issue_size not in (None, 0) and issue_size_placed is not None
                             else None)
            # Descriptor + placement data comes from `m` (bond master) alone
            # and is upserted regardless of whether `q` (marketdata/price)
            # is available this run -- deliberately NOT folded into the
            # price guard below. A bond early in book-building can have
            # ISSUESIZEPLACED without a single trade yet (confirmed live:
            # МоторТ2Р01 at ~12% placed had LAST=None the same day its
            # placement data was already populated) -- exactly the "thinner
            # initial liquidity" case this field exists to flag, so it must
            # not be skipped for the same reason a price is missing.
            db.upsert_bond(
                con, secid=secid, isin=m.get("ISIN"), issuer_id=issuer_id,
                short_name=m.get("SHORTNAME"), mat_date=mat.isoformat() if mat else None,
                face_value=face, face_unit=m.get("FACEUNIT"),
                coupon_pct=m.get("COUPONPERCENT"), list_level=m.get("LISTLEVEL"),
                qualified=1 if qualified else 0,
                updated=snap_date.isoformat(),
                issue_size=issue_size, issue_size_placed=issue_size_placed,
                placement_pct=placement_pct,
            )

            if not q or q.get("LAST") in (None, 0):
                continue

            db.insert_price_snapshot(
                con, secid=secid, snap_date=snap_date.isoformat(),
                last_pct=q.get("LAST"), yield_moex=q.get("YIELD"),
                duration=q.get("DURATION"), volume=q.get("VALTODAY"),
                num_trades=q.get("NUMTRADES"), systime=q.get("SYSTIME"),
            )

            try:
                bz = sources.fetch_bondization(secid)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning("network error fetching bondization for %s: %s", secid, e)
                continue
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
                # Sanity bound (confirmed live, 2026-08-27): MOEX's own YIELD
                # field for some floaters occasionally returns an implausible
                # sentinel/garbage value on a given day -- a genuine
                # thin-liquidity ВДО distress bond can legitimately show
                # 30-45% YTM (see rank_candidates()'s own, much stricter 0.80
                # ranking bound for that), but 0% or 696,957% is not a real
                # market yield, it's a feed glitch. Found 9 such rows across
                # 3 secids (АПРИ 2Р10, СНХТ БО-03, МОНОП 1P01), all
                # yield_source='moex' i.e. this exact branch -- skip writing
                # the row entirely rather than poisoning spread_snapshot with
                # a value that then propagates into g_spread_bp/mod_dur too.
                # Lower-bound check must come BEFORE the ytm<=0 catch-all
                # below -- that check already swallows every negative value,
                # so a ytm < FLOATER_YIELD_SANITY_MIN branch placed after it
                # would be unreachable. Also fixes a real mislabeled-message
                # bug: the catch-all's "exceeds FLOATER_YIELD_SANITY_MAX"
                # text was wrong for negative ytm (confirmed live: a real
                # ytm=-0.83 row was logged with that exact incorrect message).
                if ytm < FLOATER_YIELD_SANITY_MIN:
                    logger.warning(
                        "skipping implausible floater YIELD for %s on %s: "
                        "raw=%s -> ytm=%.4f (below %.2f)",
                        secid, snap_date, yld, ytm, FLOATER_YIELD_SANITY_MIN)
                    log_health_once_today(con, 'moex', 'compute', 'warn',
                                          'YTM_SANITY_FAIL',
                                          f'ytm={ytm:.2f} rejected for {secid} — below FLOATER_YIELD_SANITY_MIN',
                                          issuer_id=issuer_id)
                    continue
                if ytm <= 0 or ytm > FLOATER_YIELD_SANITY_MAX:
                    logger.warning(
                        "skipping implausible floater YIELD for %s on %s: "
                        "raw=%s -> ytm=%.4f (outside (0, %.1f])",
                        secid, snap_date, yld, ytm, FLOATER_YIELD_SANITY_MAX)
                    log_health(con, 'moex', 'compute', 'warn',
                               'YTM_SANITY_FAIL',
                               f'ytm={ytm:.2f} rejected for {secid} — exceeds FLOATER_YIELD_SANITY_MAX',
                               issuer_id=issuer_id)
                    continue
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
                flows_by_secid.setdefault(secid, []).extend(
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

            if mdur is None:
                # health logging (the АПРИ 2Р10 case: MOEX's DURATION field
                # missing for a floater with no dur_days to fall back on)
                log_health(con, 'pipeline', 'compute', 'warn', 'MODDUR_NULL',
                           f'mod_dur is NULL for {secid} (yield_source={yield_source})',
                           issuer_id=issuer_id)

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
                "secid": secid, "issuer": name, "ytm_pct": round(ytm * 100, 2),
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
    # Per-bond wall: build_wall() takes any dict[key, flows] and buckets by
    # month regardless of what the key means -- calling it once per secid
    # with a single-entry dict reuses the exact same bucketing function,
    # just scoped to one bond's own real cashflows instead of a whole
    # issuer's. Feeds the Портфель tab's weight-scaled combined wall
    # (dashboard/template.html), which needs real per-bond timing to be
    # meaningful for a specific selected basket, not the issuer aggregate.
    wall_by_secid = {
        secid: [{"month": b.month, "principal": round(b.principal, 0),
                 "coupon": round(b.coupon, 0)} for b in build_wall({secid: flows}, today=snap_date)]
        for secid, flows in flows_by_secid.items()
    }

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
        "maturity_wall_by_secid": wall_by_secid,
    }
