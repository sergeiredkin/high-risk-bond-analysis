"""Tests for compute/inflection.py -- the PD-adjusted expected-value
inflection curve (Task A). Pure arithmetic, no DB/network needed except
where noted."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.compute import default_probs, inflection  # noqa: E402
from hybond.store import db  # noqa: E402
from hybond.universe_pipeline import _classify_yield_zones  # noqa: E402


def test_floor_ytm_matches_hand_calc_ruBB_minus():
    # PD=0.03954, RECOVERY_RATE=0.35 -> 0.03954*0.65/(1-0.03954)
    expected = 0.03954 * 0.65 / (1 - 0.03954)
    assert inflection.floor_ytm("ruBB-") == expected
    assert round(expected, 4) == 0.0268


def test_floor_ytm_matches_hand_calc_ruCCC():
    expected = 0.13524 * 0.65 / (1 - 0.13524)
    assert inflection.floor_ytm("ruCCC") == expected
    assert round(expected, 3) == 0.102


def test_floor_ytm_unknown_rating_is_none():
    assert inflection.floor_ytm("not-a-rating") is None


def test_floor_ytm_always_positive_and_above_raw_expected_loss():
    # Regression / sanity check: break-even must always be strictly greater
    # than PD*(1-RECOVERY_RATE) (the raw expected loss with no 1/(1-PD)
    # denominator), and never zero or negative, for every rating tier.
    for rating, probs in default_probs.DEFAULT_PROBS.items():
        pd = probs["yr1"]
        raw_expected_loss = pd * (1 - default_probs.RECOVERY_RATE)
        floor = inflection.floor_ytm(rating)
        assert floor is not None
        assert floor > 0
        assert floor > raw_expected_loss


def test_ev_positive_true_above_floor_false_below():
    floor = inflection.floor_ytm("ruBB-")
    assert inflection.ev_positive(floor + 0.01, "ruBB-") is True
    assert inflection.ev_positive(floor - 0.01, "ruBB-") is False


def test_ev_positive_none_when_unrated_or_missing_aey():
    assert inflection.ev_positive(0.20, None) is None
    assert inflection.ev_positive(None, "ruBB-") is None


def test_ceiling_spread_bp_scales_with_multiple():
    fair = default_probs.implied_fair_spread_bp("ruBB-")
    assert inflection.ceiling_spread_bp("ruBB-", multiple=10.0) == fair * 10.0


def test_ceiling_multiples_from_distribution_small_sample_falls_back():
    ceiling, caution = inflection.ceiling_multiples_from_distribution([5.0, 6.0, 7.0])
    assert ceiling == inflection.FALLBACK_CEILING_MULTIPLE
    assert caution == inflection.FALLBACK_CAUTION_MULTIPLE


def test_ceiling_multiples_from_distribution_percentile_of_large_sample():
    # 100 evenly spaced ratios 1..100 -- P90 should land near 90, P75 near 75.
    ratios = [float(i) for i in range(1, 101)]
    ceiling, caution = inflection.ceiling_multiples_from_distribution(ratios)
    assert 88 <= ceiling <= 91
    assert 73 <= caution <= 76


def test_classify_yield_zone_unrated_uses_flat_cap():
    assert inflection.classify_yield_zone(0.50, 5000, None) == "negative_ev"  # 50% > 35%
    assert inflection.classify_yield_zone(0.20, 2000, None) == "unrated"  # 20% <= 35%, no rating


def test_classify_yield_zone_no_aey_is_unrated_regardless_of_rating():
    # signal_excluded bonds have aey_base=None even when a rating exists --
    # 'unrated' means "not classifiable", not literally "no rating string".
    assert inflection.classify_yield_zone(None, 5000, "ruB-") == "unrated"


def test_classify_yield_zone_below_floor_is_negative_ev():
    floor = inflection.floor_ytm("ruBB-")
    fair = default_probs.implied_fair_spread_bp("ruBB-")
    assert inflection.classify_yield_zone(floor - 0.001, fair, "ruBB-") == "negative_ev"


def test_classify_yield_zone_green_caution_negative_ev_by_ceiling():
    rating = "ruBB-"
    fair = default_probs.implied_fair_spread_bp(rating)
    floor = inflection.floor_ytm(rating)
    aey = floor + 0.20  # comfortably above the floor, so only the ceiling matters
    ceiling_mult, caution_mult = 10.0, 6.0
    ceiling = fair * ceiling_mult
    caution = fair * caution_mult
    assert inflection.classify_yield_zone(
        aey, caution - 1, rating, ceiling_mult, caution_mult) == "green"
    assert inflection.classify_yield_zone(
        aey, caution + 1, rating, ceiling_mult, caution_mult) == "caution"
    assert inflection.classify_yield_zone(
        aey, ceiling + 1, rating, ceiling_mult, caution_mult) == "negative_ev"


def test_classify_yield_zone_absolute_cap_catches_extreme_yield_even_under_ratio_ceiling():
    # ЭффТех1Р2 cross-check pattern: ruC's own implied_fair_spread_bp is
    # already huge (high PD), so an absurd absolute yield can still look
    # like an unremarkable RATIO to that already-wide fair spread. The
    # absolute backstop must catch it regardless of how loose the ratio
    # ceiling is.
    rating = "ruC"
    aey = 2.75  # 275%, ЭффТех1Р2's real live aey_base at time of writing
    fair = default_probs.implied_fair_spread_bp(rating)
    g_spread = fair * 7.7  # well under even a generous ceiling multiple
    assert inflection.classify_yield_zone(aey, g_spread, rating, 50.0, 40.0) == "negative_ev"


def test_classify_yield_zone_legitimate_high_b_minus_yield_stays_green():
    # АНТЕРРА cross-check pattern: ruB-, ~78% aey_base -- expected to clear
    # both the floor and the absolute backstop and land green/caution, not
    # negative_ev, given real observed spread ratios in its own peer group.
    rating = "ruB-"
    aey = 0.78
    fair = default_probs.implied_fair_spread_bp(rating)
    g_spread = fair * 7.3  # roughly ANTERRA-02's live ratio at time of writing
    zone = inflection.classify_yield_zone(aey, g_spread, rating,
                                           inflection.FALLBACK_CEILING_MULTIPLE,
                                           inflection.FALLBACK_CAUTION_MULTIPLE)
    assert zone in ("green", "caution")


def test_yield_inflection_by_rating_covers_every_tier():
    table = inflection.yield_inflection_by_rating()
    assert set(table.keys()) == set(default_probs.DEFAULT_PROBS.keys())
    for rating, row in table.items():
        assert row["floor_ytm"] is not None
        assert row["implied_fair_spread_bp"] is not None
        assert row["ceiling_spread_bp"] > row["caution_spread_bp"] > row["implied_fair_spread_bp"]


# ---- DB-integration: universe_pipeline._classify_yield_zones() -----------

def _row(secid, *, rating, g_spread_bp, aey_base, signal_excluded=0,
          emitent_inn="000", emitent_title="Test Co"):
    return dict(
        secid=secid, snap_date="2026-08-10", short_name=secid,
        emitent_inn=emitent_inn, emitent_title=emitent_title,
        mat_date="2028-01-01", offer_date=None, coupon_pct=10, list_level=3,
        qualified=0, last_pct=90, yield_moex=20, yield_to_offer=None,
        accrued_int=1, volume=100000, num_trades=5, duration_moex=300,
        ytm=0.2, tenor_yrs=1.5, rf_rate=0.15, g_spread_bp=g_spread_bp,
        yield_source="moex", rating=rating,
        implied_fair_spread_bp=(default_probs.implied_fair_spread_bp(rating)
                                 if rating else None),
        mispricing_bp=None, in_watchlist=0, signal_excluded=signal_excluded,
        mod_dur=1.3, aey_base=aey_base, aey_stress=aey_base, aey_bull=aey_base,
        aey_base_risk_adj=None, vdo_scope=1,
    )


def test_classify_yield_zones_writes_ev_positive_and_yield_zone(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)

    fair = default_probs.implied_fair_spread_bp("ruBB-")
    floor = inflection.floor_ytm("ruBB-")
    rows = [
        _row("A", rating="ruBB-", g_spread_bp=fair * 3, aey_base=floor + 0.10),  # clean
        _row("B", rating=None, g_spread_bp=None, aey_base=0.50),  # unrated, over 35%
        _row("C", rating="ruB-", g_spread_bp=None, aey_base=None, signal_excluded=1),  # unclassifiable
    ]
    for r in rows:
        db.insert_universe_snapshot(con, **r)
    con.commit()

    counts = _classify_yield_zones(con, "2026-08-10")
    con.commit()

    zones = {r["secid"]: (r["yield_zone"], r["ev_positive"])
             for r in con.execute(
                 "SELECT secid, yield_zone, ev_positive FROM universe_snapshot")}
    assert zones["A"][0] in ("green", "caution")
    assert zones["A"][1] == 1
    assert zones["B"] == ("negative_ev", None)
    assert zones["C"] == ("unrated", None)
    assert sum(counts.values()) == 3
