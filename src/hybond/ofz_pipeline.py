"""OFZ (Russian government bond) pipeline: real MOEX data + real CBR
scenarios feeding compute/ofz_carry.py's carry + roll-down projection.

Mirror image of universe_pipeline.py's OFZ exclusion, not a new
integration: universe_pipeline.py deliberately excludes board TQOB/SPOB and
SU-prefixed secids (OFZ was out of scope for the corporate screener -- see
its `_passes_screen` docstring, verified empirically against the live
universe). This module reuses the exact same two bulk MOEX calls
(sources.fetch_bond_master / fetch_marketdata, which are NOT board-filtered
-- they already return every bond on every board, OFZ included) and applies
the OPPOSITE filter. It does not touch universe_pipeline.py's exclusion
logic at all.

Instrument type (ПД/ПК/ИН/АД) is derived from the SU-code numeric prefix,
a real, documented feature of Russian OFZ nomenclature (assigned by the
Ministry of Finance/NSD, not something this codebase invents) -- confirmed
empirically against the live MOEX bond master 2026-08-06:
  SU25xxx / SU26xxx -> ПД (fixed coupon, no amortization)
  SU29xxx           -> ПК (floater, RUONIA-linked; COUPONPERCENT may still
                        be populated for the CURRENTLY-known coupon period,
                        so this is more reliable than a coupon-null check)
  SU46xxx           -> АД (amortizing, fixed coupon)
  SU52xxx           -> ИН (inflation-linked: small fixed real coupon on a
                        CPI-indexed principal)
Any other/unrecognised prefix defaults to ПД with a flag, surfaced rather
than hidden, same discipline as universe_pipeline.py's coupon_type heuristic.

Duplicate boards: an OFZ secid commonly appears on BOTH the TQOB and SPOB
boards (same finding universe_pipeline.py's docstring documents for its own
exclusion) -- deduplicated here, preferring TQOB (the primary trading
board; empirically covers all 59 currently-active OFZ series as of
2026-08-06, with SPOB carrying the same secids as a secondary listing).

Only ПД/АД (fixed, known future cashflows) get a REAL computed YTM/modified
duration/convexity via compute/bondmath + compute/cashflows, fetching
bondization per bond -- affordable here because the OFZ universe is ~60
bonds, not the ~3400-bond corporate universe universe_pipeline.py
deliberately avoids per-bond calls for. ПК/ИН bonds fall back to MOEX's own
reported YIELD/DURATION as a proxy (yield_source='moex'), convexity=None --
same graceful-degradation pattern pipeline.py already uses for corporate
floaters, for the same reason (the future coupon path isn't fully known).

Dirty price approximation: clean ≈ dirty (accrued interest not subtracted),
the same approximation pipeline.py's watchlist path already uses ("approx:
clean~dirty for screen") -- MOEX's bulk marketdata endpoint does not
actually return ACCRUEDINT despite the column being requestable (confirmed
empirically: the column is silently absent from every row), so there is no
real ACI figure to use here either; kept consistent with the rest of the
codebase rather than inventing a separate estimate just for OFZ.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .compute import bondmath, cashflows, ofz_carry
from .ingest import sources
from . import portfolio
from .store import db

FACEUNIT_ALLOWED = "SUR"
MIN_DAYS_TO_MATURITY = 30
PREFERRED_BOARDS = ("TQOB", "SPOB")  # dedup preference order

_TYPE_BY_PREFIX = {
    "25": "ПД", "26": "ПД",
    "29": "ПК",
    "46": "АД",
    "52": "ИН",
}


def is_ofz(secid: str, board_id: str | None, face_unit: str | None) -> bool:
    """Inverse of universe_pipeline.py's OFZ exclusion check -- same two
    signals (SU-prefixed secid, TQOB/SPOB board), same source of truth."""
    if face_unit != FACEUNIT_ALLOWED:
        return False
    return secid.startswith("SU") or board_id in {"TQOB", "SPOB"}


def classify_instrument_type(secid: str) -> tuple[str, bool]:
    """(instrument_type, is_recognised). See module docstring for the
    SU-code convention this is based on."""
    prefix = secid[2:4] if secid.startswith("SU") and len(secid) >= 4 else ""
    t = _TYPE_BY_PREFIX.get(prefix)
    if t is None:
        return "ПД", False
    return t, True


def _dedup_by_board(rows: list[dict]) -> dict[str, dict]:
    """secid -> the single row for its preferred board, when the same
    secid appears on more than one (see module docstring)."""
    best: dict[str, dict] = {}
    for r in rows:
        secid = r["SECID"]
        cur = best.get(secid)
        if cur is None:
            best[secid] = r
            continue
        cur_rank = PREFERRED_BOARDS.index(cur.get("BOARDID")) if cur.get("BOARDID") in PREFERRED_BOARDS else 99
        new_rank = PREFERRED_BOARDS.index(r.get("BOARDID")) if r.get("BOARDID") in PREFERRED_BOARDS else 99
        if new_rank < cur_rank:
            best[secid] = r
    return best


def run_ofz(snap_date: date | None = None) -> dict:
    snap_date = snap_date or date.today()
    sd = snap_date.isoformat()

    master_rows = [r for r in sources.fetch_bond_master()
                   if is_ofz(r["SECID"], r.get("BOARDID"), r.get("FACEUNIT"))]
    master = _dedup_by_board(master_rows)

    md_rows = [r for r in sources.fetch_marketdata(list(master.keys()))]
    md = _dedup_by_board(md_rows) if md_rows and "BOARDID" in md_rows[0] else {r["SECID"]: r for r in md_rows}

    con = db.connect()
    db.init_db(con)
    db.init_ofz(con)
    db.init_cbr(con)

    stored = 0
    skipped_no_price = 0
    skipped_matured = 0
    unrecognised_type: list[str] = []
    by_type: dict[str, int] = {}
    computed_count = 0
    moex_proxy_count = 0

    for secid, m in master.items():
        mat = sources.parse_date(m.get("MATDATE"))
        if not mat or mat <= snap_date + timedelta(days=MIN_DAYS_TO_MATURITY):
            skipped_matured += 1
            continue
        q = md.get(secid)
        if not q or q.get("LAST") is None:
            skipped_no_price += 1
            continue

        instrument_type, recognised = classify_instrument_type(secid)
        if not recognised:
            unrecognised_type.append(secid)
        by_type[instrument_type] = by_type.get(instrument_type, 0) + 1

        face = float(m.get("FACEVALUE") or 1000.0)
        last_pct = float(q["LAST"])
        dirty_pct = last_pct  # see module docstring: clean ~= dirty here
        coupon_pct = m.get("COUPONPERCENT")
        tenor_yrs = (mat - snap_date).days / 365.0

        mod_dur = None
        convexity = None
        ytm = None
        yield_source = "moex"
        cashflows_json = None

        if instrument_type in ("ПД", "АД"):
            bz = sources.fetch_bondization(secid)
            coup = [(sources.parse_date(c.get("coupondate")), c.get("value"))
                    for c in bz["coupons"]]
            coup = [(d, v) for d, v in coup if d]
            amort = [(sources.parse_date(a.get("amortdate") or a.get("coupondate")),
                      a.get("value")) for a in bz["amortizations"]]
            amort = [(d, v) for d, v in amort if d]
            is_floater_like = any(d > snap_date and v is None for d, v in coup)
            if not is_floater_like:
                flows = cashflows.build_cashflows(coup, amort, snap_date, face)
                if flows:
                    pairs = cashflows.to_pairs(flows, snap_date)
                    dirty_price_rub = dirty_pct / 100.0 * face
                    try:
                        ytm = bondmath.ytm_from_price(dirty_price_rub, pairs)
                        mod_dur = bondmath.modified_duration(ytm, pairs)
                        convexity = bondmath.convexity(ytm, pairs)
                        yield_source = "computed"
                        computed_count += 1
                        # the exact schedule the YTM was solved on, for
                        # ofz_carry.horizon_return() at export time
                        cashflows_json = json.dumps([[round(t, 6), round(a, 4)] for t, a in pairs])
                    except (ValueError, ZeroDivisionError):
                        pass

        if yield_source == "moex":
            y = q.get("YIELD")
            d_days = q.get("DURATION")
            if y is not None:
                ytm = float(y) / 100.0
            if d_days:
                mod_dur = d_days / 365.0  # MOEX DURATION is Macaulay-ish, in days; no convexity available
            moex_proxy_count += 1

        db.upsert_ofz_snapshot(
            con, secid=secid, snap_date=sd, short_name=m.get("SHORTNAME"),
            isin=m.get("ISIN"), board_id=m.get("BOARDID"),
            instrument_type=instrument_type, mat_date=mat.isoformat(),
            coupon_pct=coupon_pct, face_value=face, last_pct=last_pct,
            accrued_int_pct=None, dirty_price_pct=dirty_pct, ytm=ytm,
            mod_dur=mod_dur, convexity=convexity, tenor_yrs=round(tenor_yrs, 3),
            volume=q.get("VALTODAY"), num_trades=q.get("NUMTRADES"),
            yield_source=yield_source, cashflows_json=cashflows_json,
        )
        stored += 1

    scenarios = portfolio.get_rate_scenarios(con)
    con.commit()
    con.close()

    return {
        "snap_date": sd,
        "total_ofz_pulled": len(master),
        "stored": stored,
        "skipped_matured_or_no_date": skipped_matured,
        "skipped_no_price": skipped_no_price,
        "by_instrument_type": by_type,
        "unrecognised_type_secids": unrecognised_type,
        "computed_yield_count": computed_count,
        "moex_proxy_yield_count": moex_proxy_count,
        "rate_scenarios": scenarios,
    }


MIN_TENOR_FOR_PROJECTION_YEARS = 1.0

# Scenario keys the horizon returns are computed for, in page order.
RETURN_SCENARIOS = ("base", "stress", "bull", "risky", "analysts")


def is_benchmark(row) -> bool:
    """Whether an OFZ issue can stand as the nominal sovereign benchmark for
    a corporate bond: a fixed schedule (ПД/АД) with a YTM solved on its own
    cash flows. ОФЗ-ИН (the yield is real, on an indexed principal) and
    ОФЗ-ПК (the coupon path is unknown; MOEX reports no YIELD for most) never
    qualify -- the old dashboard picked 52002 as the benchmark at duration
    ~1.7 and printed floaters' YTM as 0.0."""
    return (row["instrument_type"] in ("ПД", "АД") and row["yield_source"] == "computed"
            and row["ytm"] is not None and row["ytm"] > 0 and row["mod_dur"] is not None)


def horizon_returns_for_snapshot(con, snap_date: str, scenarios: dict) -> dict[str, dict]:
    """secid -> {scenario: [horizon result, ...]} from each benchmark-grade
    issue's stored cash flows (ofz_carry.horizon_return), under every
    scenario in RETURN_SCENARIOS that `scenarios` carries a delta for, with
    the same GRADUATED_SHIFT taper the old carry model used (the parallel
    shift itself is unchanged -- plan finding O5). A horizon on or after the
    issue's last cash flow is {'horizon_years': h, 'matured': True}, never a
    number. Rows without cash flows (floaters, linkers, snapshots written
    before the column existed) are absent."""
    out: dict[str, dict] = {}
    for r in db.ofz_snapshot_rows(con, snap_date):
        if not is_benchmark(r) or not r["cashflows_json"] or not r["dirty_price_pct"]:
            continue
        pairs = [(float(t), float(a)) for t, a in json.loads(r["cashflows_json"])]
        face = r["face_value"] or 1000.0
        price = r["dirty_price_pct"] / 100.0 * face
        by_scenario = {}
        for key in RETURN_SCENARIOS:
            total = scenarios.get(key)
            if total is None:
                continue
            by_scenario[key] = [
                ofz_carry.horizon_return(cashflows=pairs, dirty_price=price, ytm=r["ytm"],
                                         delta_y=total * ofz_carry.GRADUATED_SHIFT[h], horizon_years=h)
                or {"horizon_years": h, "matured": True}
                for h in ofz_carry.HORIZONS_YEARS
            ]
        out[r["secid"]] = by_scenario
    return out


def carry_rolldown_for_snapshot(con, snap_date: str, scenarios: dict) -> list[dict]:
    """ofz_carry projections for every stored bond at `snap_date`, under
    every scenario in `scenarios` (base/stress/bull, from
    portfolio.get_rate_scenarios) -- pure post-processing over
    ofz_snapshot, no network. Bonds with no mod_dur (couldn't be priced at
    all) are skipped; convexity may be None (see ofz_carry.body_reeval_pct).

    Bonds maturing within MIN_TENOR_FOR_PROJECTION_YEARS are also skipped:
    projecting a "1-year carry" for a bond that redeems in 6 weeks is not
    meaningful (it will have matured before the shortest horizon), and the
    clean~=dirty price approximation (see module docstring) produces a
    wildly inflated YTM at very short tenors -- confirmed against real MOEX
    data 2026-08-05: SU26219RMFS4 (0.115yr to maturity, 99.315% clean price)
    computed YTM=47.6%, an artifact of annualizing a tiny price/accrued-
    interest gap over a few weeks, not a real yield. Full raw data (real
    YTM/price as computed) stays in ofz_snapshot regardless -- only the
    carry/roll-down PROJECTION excludes these, since the projection is the
    thing that would visibly misrepresent them."""
    rows = db.ofz_snapshot_rows(con, snap_date)
    out = []
    for r in rows:
        if r["mod_dur"] is None or r["coupon_pct"] is None:
            continue
        if (r["tenor_yrs"] or 0) < MIN_TENOR_FOR_PROJECTION_YEARS:
            continue
        projections = {}
        for scen_key in ("base", "stress", "bull"):
            total_delta_y = scenarios.get(scen_key)
            if total_delta_y is None:
                continue
            projections[scen_key] = ofz_carry.project_all_horizons(
                annual_coupon_pct=r["coupon_pct"], dirty_price_pct=r["dirty_price_pct"],
                mod_dur=r["mod_dur"], convexity=r["convexity"], total_delta_y=total_delta_y,
            )
        out.append({"secid": r["secid"], "projections": projections})
    return out
