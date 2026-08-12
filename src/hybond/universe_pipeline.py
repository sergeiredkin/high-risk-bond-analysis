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

from .compute import bondmath, default_probs, inflection
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

# Near-offer annualization amplification: investigated as part of the
# "implausible 1000s-of-bp mispricing for IG-adjacent names" report. Most
# such cases (e.g. Самолёт/АПРИ bonds with 24-30.5% real coupons trading
# near par) turned out to be genuine -- a very high real coupon plus a real
# discount legitimately produces a very high YTM, and comparing that to
# implied_fair_spread_bp (a pure default-probability benchmark with no
# illiquidity/duration/term-premium term) is a model-calibration gap, not a
# bug (confirmed separately: average mispricing_bp is monotonically
# positive across EVERY rating tier, even for un-excluded, liquid bonds).
#
# One genuine artifact was found among them though: RU000A109874/СамолетP15
# (offer in 8 days, LAST=99.43% of face, coupon 19.5%) reports ytm=61.4% --
# its whole ~1% real return over an 8-day holding period is what's being
# annualized into that number. The tell is ytm far exceeding the bond's own
# coupon rate while the horizon to the next put/maturity event is short:
# for a near-par bond that isn't happening, annualized ytm tracks the
# coupon rate itself (see the many 24-30.5%-coupon comparables above, whose
# ytm sits within ~30% of their own coupon, not multiples of it).
NEAR_OFFER_AMPLIFIED = lambda b: (  # noqa: E731
    b.get("tenor_yrs") is not None and b.get("tenor_yrs") < 0.3 and
    b.get("coupon_pct") is not None and b.get("ytm") is not None and
    b.get("ytm") > 2 * (b.get("coupon_pct") / 100.0)
)


# --- broad ВДО screen (Phase 1): a flag/filter over universe_snapshot, not a
# separate table -- it's the same daily-screened bonds, just a narrower scope
# definition on top. Computed inline in run_universe() below and stored as
# `vdo_scope`, so it's already fresh every daily cron run (no separate
# schedule needed).

# Bank subordinated/AT1/T2/perpetual debt has different risk mechanics
# (loss-absorption on regulatory trigger, call risk instead of a fixed
# maturity, issuer usually investment-grade already) -- excluded from ВДО
# scope regardless of rating/spread. No bulk MOEX field flags this (the
# per-security `description` endpoint that would is a per-bond call, the
# exact cost this pipeline exists to avoid at universe scale -- see
# fetch_security_qualified()'s docstring for the same tradeoff). Keyword
# heuristic instead, same pattern as COMMODITY_LINKED above. Verified
# against the live bond master: matches 32 SECIDs, 100% genuine bank
# subordinated/AT1/T2/perpetual issues (ВТБ, Сбербанк, ГПБ, Совкомбанк,
# ДОМ.РФ, ТКБ, ТБанк/TCS) with zero false positives on ordinary corporate
# names -- broader single-token patterns like bare "т2" were tried and
# rejected (they matched inside ordinary tickers, e.g. "Роснфт2P1").
_BANK_SUB_PERP_PATTERNS = (
    "суб", "бессроч", "вечн", "perp", " т1", " т2", "-т1", "-т2", "ат1", "ат2",
)


def is_bank_subordinated_perpetual(short_name: str | None, secname: str | None) -> bool:
    name = f"{short_name or ''} {secname or ''}".lower()
    return any(p in name for p in _BANK_SUB_PERP_PATTERNS)


# "ruBB and below, or unrated".
VDO_RATING_CUTOFF = "ruBB"


def is_in_vdo_rating_band(rating: str | None, cutoff: str = VDO_RATING_CUTOFF) -> bool:
    """True for unrated (None) bonds and for any rating at or below `cutoff`
    on Эксперт РА's scale (worse rank = higher index = riskier). Ratings not
    on that scale (shouldn't happen; the scale spans ruAAA..ruC) are treated
    as out-of-band rather than silently included."""
    if rating is None:
        return True
    rank = default_probs.rating_rank(rating)
    if rank is None:
        return False
    return rank >= default_probs.rating_rank(cutoff)


# G-spread and daily-volume thresholds for the first-cut ВДО scope. Volume
# floor of 300 (same raw `volume` units already stored, i.e. MOEX's VALTODAY)
# is the lowest non-zero volume seen among the CURRENTLY TRACKED 26-issuer
# watchlist as of 2026-07-31 (RU000A10B396/МОНОП 1P04 at 303) -- a floor at
# that level doesn't exclude anything as illiquid as what's already being
# actively monitored.
VDO_MIN_G_SPREAD_BP = 500
VDO_MIN_VOLUME = 300


def compute_vdo_scope(*, rating: str | None, g_spread_bp: float | None,
                      volume: float | None, short_name: str | None,
                      secname: str | None, signal_excluded: bool) -> bool:
    """The broad-ВДО-screen membership test for one bond. Pure function (no
    DB/network) so it's directly unit-testable; run_universe() calls it
    inline per screened bond and stores the result in `vdo_scope`."""
    if signal_excluded:
        return False
    if is_bank_subordinated_perpetual(short_name, secname):
        return False
    if not is_in_vdo_rating_band(rating):
        return False
    if g_spread_bp is None or g_spread_bp <= VDO_MIN_G_SPREAD_BP:
        return False
    if volume is None or volume < VDO_MIN_VOLUME:
        return False
    return True


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
        offer = sources.parse_date(m.get("OFFERDATE"))
        tenor = (mat - snap_date).days / 365.0  # true years-to-maturity: stored/displayed as-is

        # MOEX's own YIELD/DURATION are computed to the nearest OFFER (put)
        # date when one exists and falls before maturity, NOT to final
        # maturity -- confirmed empirically: duration_moex for a bond with a
        # near offer matches days-to-offer, not days-to-maturity (e.g.
        # RU000A109874/СамолетP15: duration_moex=8 days vs 10 days to its
        # 2026-08-07 offer, vs 367 days to its real 2027-07-30 maturity).
        # Pairing that offer-horizon yield with a risk-free rate picked off
        # the MATURITY tenor mismatches the two horizons and inflates
        # g_spread/mispricing hugely for any bond with a near put -- this
        # yield_horizon (not `tenor`) is what must drive the rf lookup and
        # the annualization-noise exclusion check below. `tenor` itself is
        # left alone since it's stored as tenor_yrs for maturity-wall/
        # curve-coverage uses elsewhere, which do want true time-to-maturity.
        if offer and snap_date < offer < mat:
            yield_horizon = (offer - snap_date).days / 365.0
        else:
            yield_horizon = tenor

        yld = q.get("YIELD")
        if yld is None or yield_horizon <= 0:
            continue
        ytm = float(yld) / 100.0
        try:
            rf = bondmath.interp_curve(yield_horizon, curve)
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
        # SHORT_TENOR_THIN's "annualizing a handful of trades near the final
        # weeks is noise" reasoning applies to whichever event (put or
        # maturity) the yield is actually annualized over -- yield_horizon,
        # not the maturity-only tenor.
        exclusion_check = {"coupon_pct": m.get("COUPONPERCENT"), "ytm": ytm,
                            "tenor_yrs": yield_horizon, "num_trades": num_trades}
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
        elif NEAR_OFFER_AMPLIFIED(exclusion_check):
            signal_excluded = True
            yield_source = "near_offer_amplified"
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

        in_wl = 1 if emitent_inn in watchlist_inns else 0
        volume = q.get("VALTODAY")
        vdo_scope = compute_vdo_scope(
            rating=rating, g_spread_bp=gsp, volume=volume,
            short_name=m.get("SHORTNAME"), secname=m.get("SECNAME"),
            signal_excluded=signal_excluded)

        db.insert_universe_snapshot(
            con, secid=secid, snap_date=sd, short_name=m.get("SHORTNAME"),
            emitent_inn=emitent_inn, emitent_title=emitent_title,
            mat_date=mat.isoformat(), offer_date=offer.isoformat() if offer else None,
            coupon_pct=m.get("COUPONPERCENT"), list_level=m.get("LISTLEVEL"),
            qualified=1 if m.get("ISQUALIFIEDINVESTORS") else 0,
            last_pct=q.get("LAST"), yield_moex=q.get("YIELD"),
            yield_to_offer=None,  # not derivable without per-bond cashflows
            accrued_int=q.get("ACCRUEDINT"), volume=volume,
            num_trades=num_trades, duration_moex=dur_days,
            ytm=ytm, tenor_yrs=tenor, rf_rate=rf, g_spread_bp=gsp,
            yield_source=yield_source, rating=rating,
            implied_fair_spread_bp=implied_fair, mispricing_bp=misprice,
            in_watchlist=in_wl, signal_excluded=1 if signal_excluded else 0,
            mod_dur=mdur_yrs, aey_base=aey_base, aey_stress=aey_stress, aey_bull=aey_bull,
            aey_base_risk_adj=aey_base_risk_adj, vdo_scope=1 if vdo_scope else 0,
            coupon_period=m.get("COUPONPERIOD"),
        )
        n_priced += 1
        top_rows.append({
            "secid": secid, "short_name": m.get("SHORTNAME"),
            "emitent_title": emitent_title,
            "g_spread_bp": gsp, "ytm_pct": ytm * 100, "rating": rating,
            "mispricing_bp": misprice, "in_watchlist": in_wl,
            "signal_excluded": signal_excluded,
        })

    zone_counts = _classify_yield_zones(con, sd)

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
        "yield_zone_counts": zone_counts,
    }


def _classify_yield_zones(con, sd: str) -> dict[str, int]:
    """Second pass over this run's just-inserted universe_snapshot rows
    (Task A -- PD-adjusted expected-value inflection, compute/inflection.py):
    the ceiling/caution multiples are derived from THIS run's own live
    distribution of (g_spread_bp / implied_fair_spread_bp) across rated,
    non-excluded bonds (see inflection.py's module docstring for why a fixed
    multiplier doesn't work), so no single bond can be classified until the
    whole run's rated rows are known -- can't be folded into the per-bond
    loop above. Returns a zone -> count summary for the CLI report."""
    rows = con.execute(
        "SELECT secid, rating, g_spread_bp, aey_base, implied_fair_spread_bp, "
        "signal_excluded FROM universe_snapshot WHERE snap_date=?", (sd,)).fetchall()

    ratios = [
        r["g_spread_bp"] / r["implied_fair_spread_bp"]
        for r in rows
        if not r["signal_excluded"] and r["implied_fair_spread_bp"]
        and r["implied_fair_spread_bp"] > 0
        and r["g_spread_bp"] is not None and r["g_spread_bp"] > 0
    ]
    ceiling_mult, caution_mult = inflection.ceiling_multiples_from_distribution(ratios)

    updates = []
    zone_counts: dict[str, int] = {}
    for r in rows:
        zone = inflection.classify_yield_zone(
            r["aey_base"], r["g_spread_bp"], r["rating"], ceiling_mult, caution_mult)
        ev_pos = inflection.ev_positive(r["aey_base"], r["rating"])
        ev_pos_db = None if ev_pos is None else (1 if ev_pos else 0)
        updates.append((ev_pos_db, zone, r["secid"], sd))
        zone_counts[zone] = zone_counts.get(zone, 0) + 1

    db.bulk_update_yield_zone(con, updates)
    return zone_counts


def vdo_screen_report(con, snap_date: str | None = None, top_n: int = 20) -> dict:
    """Read-only report over the already-computed `vdo_scope` flag -- no
    network calls, just a query against the latest (or given) universe
    snapshot. Separate from `run_universe()` deliberately: the flag itself is
    computed daily as part of the existing cron (`hybond universe`), but
    reviewing/re-thresholding the scope definition shouldn't require a fresh
    MOEX pull every time."""
    sd = snap_date or db.latest_universe_snap_date(con)
    if not sd:
        return {"snap_date": None, "universe_size": 0, "top_by_aey_risk_adj": [],
                "rating_distribution": {}}

    rows = con.execute(
        "SELECT secid, short_name, emitent_title, emitent_inn, rating, "
        "g_spread_bp, mispricing_bp, aey_base_risk_adj, volume "
        "FROM universe_snapshot WHERE snap_date = ? AND vdo_scope = 1",
        (sd,)).fetchall()
    rows = [dict(r) for r in rows]

    ranked = sorted(
        (r for r in rows if r["aey_base_risk_adj"] is not None),
        key=lambda r: r["aey_base_risk_adj"], reverse=True)

    rating_dist: dict[str, int] = {}
    for r in rows:
        key = r["rating"] or "unrated"
        rating_dist[key] = rating_dist.get(key, 0) + 1

    return {
        "snap_date": sd,
        "universe_size": len(rows),
        "top_by_aey_risk_adj": [
            {"secid": r["secid"],
             "name": r["short_name"] or r["emitent_title"],
             "rating": r["rating"] or "unrated",
             "g_spread_bp": r["g_spread_bp"],
             "aey_base_risk_adj_pct": round(r["aey_base_risk_adj"] * 100, 2),
             "volume": r["volume"]}
            for r in ranked[:top_n]
        ],
        "rating_distribution": dict(
            sorted(rating_dist.items(), key=lambda kv: -kv[1])),
    }
