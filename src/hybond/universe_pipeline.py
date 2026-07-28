"""Universe pipeline: pull the FULL MOEX corporate-bond universe (not just
the watchlist), screen it down to tradeable RUB corporate paper, compute a
G-spread proxy for every bond, join against our own `rating` table, and store
one row per bond per day in `universe_snapshot`. This is the raw material
for the screener dashboard tab and for spotting mispriced names outside the
current 20-issuer watchlist.

Three bulk ISS fetches, never one call per bond:
  1. fetch_bond_master()  -- descriptors (FACEUNIT, MATDATE, ISIN, BOARDID,
     OFFERDATE, COUPONPERCENT, LISTLEVEL), ~3400 rows, one call.
  2. fetch_marketdata()   -- LAST/YIELD/DURATION/NUMTRADES/VOLUME/ACCRUEDINT,
     one call.
  3. fetch_emitent_map()  -- SECID -> ИНН, ~105 paginated calls (~45s) because
     MOEX's single-call bond-master endpoint does not expose emitent_inn at
     all (see that function's docstring). Needed so a bond from a rated
     issuer is recognised even if it isn't one of the exact SECIDs already
     in our `bond` table -- e.g. a third Сегежа series we don't hold should
     still inherit Сегежа's rating. Confirmed by explicit user decision this
     cost is acceptable (still far cheaper than per-bond fetch_bondization).

     This map barely changes day to day (issuer identity, not price/yield),
     so it's cached in the `emitent_map` table and only re-paginated when the
     cache is empty, older than EMITENT_MAP_MAX_AGE_DAYS, or the caller passes
     refresh_emitent_map=True (`hybond universe --refresh-emitent-map`, or the
     weekly cron slot in daily_run.sh). This is what drops the daily universe
     run from ~55s to ~10s on every day but the weekly refresh.

G-spread here ALWAYS uses MOEX's own reported YIELD as the YTM proxy, never
computed from cashflows -- fetching bondization per bond to do that properly
is exactly the ~650-call cost this pipeline exists to avoid. Contrast with
pipeline.py's watchlist path, which computes precisely for fixed-coupon
bonds and only falls back to the MOEX-yield proxy for floaters; this is
deliberately the coarser, universe-wide version.

Liquidity screening ("NUMTRADES>=1 OR traded within 5 days"): the 5-day
clause can only be answered from OUR OWN accumulated universe_snapshot
history (MOEX's single-day snapshot has no per-bond last-trade-date field
without yet another call). On the very first run there is no history, so
screening degrades to today's NUMTRADES alone -- from the second run onward
db.universe_traded_recently() gives the 5-day clause real teeth.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .compute import bondmath, default_probs
from .ingest import sources
from . import portfolio
from .store import db

FACEUNIT_ALLOWED = "SUR"
EXCLUDED_BOARD = "TQOB"
MIN_DAYS_TO_MATURITY = 30
LIQUIDITY_LOOKBACK_DAYS = 5
EMITENT_MAP_MAX_AGE_DAYS = 7


EXCLUDED_BOARDS = {"TQOB", "SPOB"}


def _passes_screen(secid: str, m: dict, q: dict, snap_date: date) -> bool:
    """Descriptor + price checks only (no DB, no liquidity-history lookup --
    that needs `con` and is applied by the caller).

    OFZ exclusion note: the originally-specified criteria ("ISIN starts with
    SU", "board is TQOB") don't match MOEX's actual data model -- verified
    empirically against the live universe. No ISIN ever starts with "SU";
    that prefix is on SECID (e.g. SU26207RMFS9, ISIN RU000A0JS3W6). And OFZ
    bonds sit on BOTH the TQOB and SPOB boards, not just TQOB -- excluding
    only TQOB let ~half of them through. Both checks below use the fields
    that actually carry this signal.
    """
    if m.get("FACEUNIT") != FACEUNIT_ALLOWED:
        return False
    mat = sources.parse_date(m.get("MATDATE"))
    if not mat or mat <= snap_date + timedelta(days=MIN_DAYS_TO_MATURITY):
        return False
    if secid.startswith("SU"):
        return False
    if m.get("BOARDID") in EXCLUDED_BOARDS:
        return False
    if q.get("LAST") is None:
        return False
    return True


# Both lambdas below operate on a bond's ALREADY-COMPUTED fields (coupon_pct,
# ytm, tenor_yrs, num_trades) -- these run AFTER g_spread_bp/ytm are computed,
# unlike _passes_screen above which runs before any bond even enters the
# screened set. They only gate the mispricing SIGNAL (implied_fair_spread_bp,
# mispricing_bp, and the PD fields the export script derives from them) --
# the bond still appears in universe_snapshot with its raw ytm/g_spread_bp.

# Commodity-linked / structured notes (e.g. Селигдар's gold/silver bonds):
# real return is in principal appreciation, not the cash coupon, so a low
# coupon is paired with a MOEX-reported yield far below the risk-free rate
# -- not because the bond is a screaming buy, but because nominal-cashflow
# yield is the wrong lens for this instrument type entirely. A precise fix
# needs the per-security BOND_TYPE field (one call per bond, not affordable
# at universe scale, same tradeoff as the qualified-investor flag); this is
# the observable proxy instead.
COMMODITY_LINKED = lambda b: (  # noqa: E731
    b.get("coupon_pct") is not None and b.get("coupon_pct") < 8.0 and
    b.get("ytm") is not None and b.get("ytm") < 0.10
)

# Short-tenor, thin prints: annualizing a yield from a handful of trades in
# the bond's final weeks is noise, not signal -- a single off-market print
# can swing the annualized number arbitrarily.
SHORT_TENOR_THIN = lambda b: (  # noqa: E731
    b.get("tenor_yrs") is not None and b.get("tenor_yrs") < 0.5 and
    b.get("num_trades") is not None and b.get("num_trades") < 5
)

# MOEX reports YIELD=0.0 (never a small positive number) for bonds so deeply
# distressed that its own yield calc breaks down -- confirmed against
# RU000A10BWL7 (МОНОП 1P05): LAST=3.01% of face, 26 trades/day (actively
# traded, not stale), yet YIELD and DURATION both come back exactly 0.0. A
# real 0% yield would mean full redemption at par with zero return, which is
# incompatible with a price that low -- this is a MOEX sentinel for
# "uncomputable", not a genuine yield, so the mispricing signal must not
# trust it even though the bond stays in universe_snapshot with its raw
# (nonsensical) ytm=0.0 for visibility.
ZERO_YIELD_DEGENERATE = lambda b: b.get("ytm") == 0.0  # noqa: E731


def _load_emitent_map(con, snap_date: date, force_refresh: bool) -> dict:
    """Cached SECID -> {emitent_inn, emitent_title}, re-paginated only when
    empty, stale (>EMITENT_MAP_MAX_AGE_DAYS), or force_refresh is set."""
    db.init_emitent_map(con)
    fetched_on = db.emitent_map_fetched_on(con)
    stale = True
    if fetched_on:
        age = (snap_date - datetime.strptime(fetched_on, "%Y-%m-%d").date()).days
        stale = age > EMITENT_MAP_MAX_AGE_DAYS
    if force_refresh or stale or not fetched_on:
        rows = sources.fetch_emitent_map()
        db.replace_emitent_map(con, rows, snap_date.isoformat())
        return rows
    return db.get_emitent_map(con)


def run_universe(snap_date: date | None = None, refresh_emitent_map: bool = False) -> dict:
    snap_date = snap_date or date.today()
    sd = snap_date.isoformat()

    master_rows = sources.fetch_bond_master()
    master = {r["SECID"]: r for r in master_rows}
    md = {r["SECID"]: r for r in sources.fetch_marketdata()}
    curve = sources.fetch_cbr_zcyc()

    con = db.connect()
    db.init_db(con)
    db.init_universe(con)
    db.init_rating(con)
    db.init_cbr(con)

    scenarios = portfolio.get_rate_scenarios(con)
    emitent_map = _load_emitent_map(con, snap_date, refresh_emitent_map)

    # Match by emitent_inn against issuer, not by secid against bond -- an
    # issuer we track is "in the watchlist" for every series it has
    # outstanding, not just the specific one or two SECIDs we happen to hold
    # (e.g. all of Самолёт's/ВУШ's/МГКЛ's series should flag, not just the
    # tracked bond).
    watchlist_inns = {r["issuer_id"] for r in con.execute("SELECT issuer_id FROM issuer")}
    liquidity_since = (snap_date - timedelta(days=LIQUIDITY_LOOKBACK_DAYS)).isoformat()

    total_pulled = len(master_rows)
    screened_rows = []
    for secid, m in master.items():
        q = md.get(secid)
        if not q or not _passes_screen(secid, m, q, snap_date):
            continue
        num_trades = q.get("NUMTRADES") or 0
        if num_trades < 1 and not db.universe_traded_recently(
                con, secid, sd, liquidity_since):
            continue
        screened_rows.append((secid, m, q))

    n_priced = 0
    n_rated = 0
    n_excluded = 0
    top_rows: list[dict] = []

    for secid, m, q in screened_rows:
        mat = sources.parse_date(m.get("MATDATE"))
        tenor = (mat - snap_date).days / 365.0
        yld = q.get("YIELD")
        if yld is None or tenor <= 0:
            continue
        ytm = float(yld) / 100.0
        try:
            rf = bondmath.interp_curve(tenor, curve)
        except (ValueError, ZeroDivisionError):
            continue
        gsp = (ytm - rf) * 1e4
        dur_days = q.get("DURATION")
        mdur_yrs = dur_days / 365.0 if dur_days else None

        emit = emitent_map.get(secid, {})
        emitent_inn = emit.get("emitent_inn")
        emitent_title = emit.get("emitent_title") or m.get("SECNAME")

        rating = db.current_rating(con, emitent_inn) if emitent_inn else None
        implied_fair = default_probs.implied_fair_spread_bp(rating) if rating else None
        misprice = (gsp - implied_fair) if implied_fair is not None else None
        if rating:
            n_rated += 1

        num_trades = q.get("NUMTRADES")
        exclusion_check = {"coupon_pct": m.get("COUPONPERCENT"), "ytm": ytm,
                            "tenor_yrs": tenor, "num_trades": num_trades}
        yield_source = "moex"
        signal_excluded = False
        if COMMODITY_LINKED(exclusion_check):
            signal_excluded = True
            yield_source = "commodity_linked"
        elif SHORT_TENOR_THIN(exclusion_check):
            signal_excluded = True
            yield_source = "near_maturity_thin"
        elif ZERO_YIELD_DEGENERATE(exclusion_check):
            signal_excluded = True
            yield_source = "zero_yield_degenerate"
        if signal_excluded:
            implied_fair = None
            misprice = None
            n_excluded += 1

        # AEY (Adjusted Expected Yield): same signal_excluded gate as
        # implied_fair/misprice above -- a bond whose raw ytm is already
        # flagged unreliable (commodity-linked / near-maturity-thin /
        # zero-yield-degenerate) shouldn't get a duration-adjusted scenario
        # number built on top of that unreliable ytm either.
        if signal_excluded:
            aey_base = aey_stress = aey_bull = aey_base_risk_adj = None
        else:
            aey_base = portfolio.bond_aey(ytm, mdur_yrs, scenarios["base"])
            aey_stress = portfolio.bond_aey(ytm, mdur_yrs, scenarios["stress"])
            aey_bull = portfolio.bond_aey(ytm, mdur_yrs, scenarios["bull"])
            aey_base_risk_adj = portfolio.pd_adjusted_aey(aey_base, rating)

        offer = sources.parse_date(m.get("OFFERDATE"))
        in_wl = 1 if emitent_inn in watchlist_inns else 0

        db.insert_universe_snapshot(
            con, secid=secid, snap_date=sd, short_name=m.get("SHORTNAME"),
            emitent_inn=emitent_inn, emitent_title=emitent_title,
            mat_date=mat.isoformat(), offer_date=offer.isoformat() if offer else None,
            coupon_pct=m.get("COUPONPERCENT"), list_level=m.get("LISTLEVEL"),
            qualified=1 if m.get("ISQUALIFIEDINVESTORS") else 0,
            last_pct=q.get("LAST"), yield_moex=q.get("YIELD"),
            yield_to_offer=None,  # not derivable without per-bond cashflows
            accrued_int=q.get("ACCRUEDINT"), volume=q.get("VALTODAY"),
            num_trades=num_trades, duration_moex=dur_days,
            ytm=ytm, tenor_yrs=tenor, rf_rate=rf, g_spread_bp=gsp,
            yield_source=yield_source, rating=rating,
            implied_fair_spread_bp=implied_fair, mispricing_bp=misprice,
            in_watchlist=in_wl, signal_excluded=1 if signal_excluded else 0,
            mod_dur=mdur_yrs, aey_base=aey_base, aey_stress=aey_stress, aey_bull=aey_bull,
            aey_base_risk_adj=aey_base_risk_adj,
        )
        n_priced += 1
        top_rows.append({
            "secid": secid, "short_name": m.get("SHORTNAME"),
            "emitent_title": emitent_title,
            "g_spread_bp": gsp, "ytm_pct": ytm * 100, "rating": rating,
            "mispricing_bp": misprice, "in_watchlist": in_wl,
            "signal_excluded": signal_excluded,
        })

    con.commit()
    n_in_watchlist = sum(1 for r in top_rows if r["in_watchlist"])
    con.close()

    top_rows.sort(key=lambda r: r["g_spread_bp"], reverse=True)

    return {
        "snap_date": sd,
        "total_pulled": total_pulled,
        "after_screening": len(screened_rows),
        "with_g_spread": n_priced,
        "with_rating": n_rated,
        "in_watchlist": n_in_watchlist,
        "signal_excluded": n_excluded,
        "aey_source_date": scenarios["source_date"],
        "aey_is_fallback": scenarios["is_fallback"],
        "top_by_g_spread": top_rows[:10],
    }
