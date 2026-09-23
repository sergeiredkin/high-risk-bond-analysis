"""Tests for scripts/export_dashboard_data.py -- Вотчлист Restructure task
(2026-08-16), spread_history_by_issuer() (per-issuer G-spread history for
the new Рынок tab delta table + chart)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond.store import db  # noqa: E402
from export_dashboard_data import (  # noqa: E402
    spread_history_by_issuer, passive_issuers_export, passive_events_since,
    universe_snapshot_bundle, RATING_TREND_VALUE, rating_trend,
    rating_history_by_issuer, _latest_spread_snap_date, portfolio_data_freshness,
    pipeline_health_export, cleanup_export_stale_duplicates,
    export_portfolio_allocation, SECTOR_COLORS)
from hybond import health  # noqa: E402

ISSUER = "7701234567"


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, ISSUER, "ООО Очень Длинное Название Компании")
    db.upsert_bond(con, secid="BOND1", isin="RU1", issuer_id=ISSUER,
                    short_name="B1", mat_date="2028-01-01", face_value=1000.0,
                    face_unit="SUR", coupon_pct=15.0, list_level=3, qualified=1,
                    updated="2026-08-14")
    db.upsert_bond(con, secid="BOND2", isin="RU2", issuer_id=ISSUER,
                    short_name="B2", mat_date="2029-01-01", face_value=1000.0,
                    face_unit="SUR", coupon_pct=16.0, list_level=3, qualified=1,
                    updated="2026-08-14")
    return con


def test_picks_highest_avg_volume_bond_as_representative(tmp_path):
    con = _con(tmp_path)
    # BOND1: low volume; BOND2: high volume -- BOND2 must be picked.
    db.insert_price_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                              last_pct=100, yield_moex=15, duration=400,
                              volume=1000, num_trades=5, systime="t")
    db.insert_price_snapshot(con, secid="BOND2", snap_date="2026-08-01",
                              last_pct=100, yield_moex=16, duration=400,
                              volume=500000, num_trades=50, systime="t")
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=100.0, mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-02",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=105.0, mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND2", snap_date="2026-08-01",
                               ytm=0.16, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=300.0, mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND2", snap_date="2026-08-02",
                               ytm=0.16, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=310.0, mod_dur=1.5, convexity=None)
    con.commit()

    history, labels = spread_history_by_issuer(con, [ISSUER], "2026-08-02")
    con.close()

    assert ISSUER in history
    assert [r["g_spread_bp"] for r in history[ISSUER]] == [300.0, 310.0]


def test_fewer_than_2_points_excludes_issuer(tmp_path):
    con = _con(tmp_path)
    db.insert_price_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                              last_pct=100, yield_moex=15, duration=400,
                              volume=1000, num_trades=5, systime="t")
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=100.0, mod_dur=1.5, convexity=None)
    con.commit()

    history, labels = spread_history_by_issuer(con, [ISSUER], "2026-08-01")
    con.close()
    assert ISSUER not in history


def test_90_day_window_excludes_older_rows(tmp_path):
    con = _con(tmp_path)
    db.insert_price_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                              last_pct=100, yield_moex=15, duration=400,
                              volume=1000, num_trades=5, systime="t")
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2025-01-01",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=100.0, mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=200.0, mod_dur=1.5, convexity=None)
    con.commit()

    history, labels = spread_history_by_issuer(con, [ISSUER], "2026-08-16")
    con.close()
    # Only 1 point within the 90-day window (2026-08-01) -- 2025-01-01 is
    # excluded -- so the issuer must NOT qualify (needs >= 2 points).
    assert ISSUER not in history


def test_label_truncated_to_20_chars(tmp_path):
    con = _con(tmp_path)
    db.insert_price_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                              last_pct=100, yield_moex=15, duration=400,
                              volume=1000, num_trades=5, systime="t")
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=100.0, mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-02",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=105.0, mod_dur=1.5, convexity=None)
    con.commit()

    history, labels = spread_history_by_issuer(con, [ISSUER], "2026-08-02")
    con.close()
    assert len(labels[ISSUER]) == 20
    assert labels[ISSUER] == "ООО Очень Длинное На"


def test_zero_or_negative_g_spread_excluded(tmp_path):
    con = _con(tmp_path)
    db.insert_price_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                              last_pct=100, yield_moex=15, duration=400,
                              volume=1000, num_trades=5, systime="t")
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-01",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=0.0, mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-02",
                               ytm=0.15, tenor_yrs=2, rf_rate=0.14,
                               g_spread_bp=-5.0, mod_dur=1.5, convexity=None)
    con.commit()

    history, labels = spread_history_by_issuer(con, [ISSUER], "2026-08-02")
    con.close()
    assert ISSUER not in history


# ---- passive_issuers_export (Watchlist Tier System, 2026-08-18) ------------

def _bare_con(tmp_path, name="passive.sqlite"):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    db.init_rating(con)
    db.init_universe(con)
    return con


def test_passive_issuers_export_basic_shape(tmp_path):
    con = _bare_con(tmp_path)
    db.add_issuer_at_tier(con, ISSUER, "ООО Очень Длинное Название Компании",
                           watchlist_tier=2, sector="leasing")
    con.commit()
    rows = passive_issuers_export(con, db.list_issuers_by_tier(con, 2), {}, None)
    con.close()
    assert len(rows) == 1
    r = rows[0]
    assert r["issuer_id"] == ISSUER
    assert r["sector"] == "leasing"
    # watchlist_tier is a pipeline-internal scoping field and is no longer
    # handed to the dashboard -- being in this list IS the tier=2 signal
    # (Купон Лаб redesign stage 3); see tests/test_dashboard_pages.py.
    assert "watchlist_tier" not in r
    assert "bonds" not in r  # no verdict/bond payload for tier=2, by design
    assert "six_pillars" not in r


def test_passive_issuers_export_picks_max_aey_for_snap_date(tmp_path):
    con = _bare_con(tmp_path)
    db.add_issuer_at_tier(con, ISSUER, "Some Co", watchlist_tier=2)
    con.commit()
    snap_kwargs = dict(short_name=None, emitent_title=None, mat_date=None, offer_date=None,
                        coupon_pct=None, list_level=None, qualified=None, last_pct=None,
                        yield_moex=None, yield_to_offer=None, accrued_int=None, volume=None,
                        num_trades=None, duration_moex=None, ytm=None, tenor_yrs=None,
                        rf_rate=None, g_spread_bp=None, yield_source=None, rating=None,
                        implied_fair_spread_bp=None, mispricing_bp=None, in_watchlist=0,
                        signal_excluded=0, mod_dur=None, aey_stress=None, aey_bull=None)
    db.insert_universe_snapshot(con, secid="BOND1", snap_date="2026-08-18",
                                 emitent_inn=ISSUER, aey_base=0.20, aey_base_risk_adj=0.18,
                                 **snap_kwargs)
    db.insert_universe_snapshot(con, secid="BOND2", snap_date="2026-08-18",
                                 emitent_inn=ISSUER, aey_base=0.25, aey_base_risk_adj=0.30,
                                 **snap_kwargs)
    con.commit()
    rows = passive_issuers_export(con, db.list_issuers_by_tier(con, 2), {}, "2026-08-18")
    con.close()
    assert rows[0]["last_aey_base_risk_adj_pct"] == 30.0  # max(18, 30) -> 30%


def test_passive_issuers_export_falls_back_to_watchlist_json_name(tmp_path):
    con = _bare_con(tmp_path)
    db.add_issuer_at_tier(con, ISSUER, "DB Name", watchlist_tier=2)
    con.commit()
    watchlist = {ISSUER: {"name": "Watchlist.json Name", "sector": "fuel_retail"}}
    rows = passive_issuers_export(con, db.list_issuers_by_tier(con, 2), watchlist, None)
    con.close()
    assert rows[0]["name"] == "Watchlist.json Name"
    assert rows[0]["sector"] == "fuel_retail"


# ---- tier=2 events in Лента событий (event-feed-filtering task, 2026-08-21) -

def test_passive_issuers_export_includes_recent_events(tmp_path):
    con = _bare_con(tmp_path)
    db.add_issuer_at_tier(con, ISSUER, "Some Co", watchlist_tier=2)
    con.commit()
    db.insert_event(con, issuer_id=ISSUER, occurred=date.today().isoformat(),
                     category="litigation", severity="high",
                     summary="Иск подан", source_url=None)
    con.commit()
    rows = passive_issuers_export(con, db.list_issuers_by_tier(con, 2), {}, None)
    con.close()
    assert len(rows[0]["events"]) == 1
    assert rows[0]["events"][0]["severity"] == "high"


def test_passive_events_since_respects_since_date(tmp_path):
    con = _bare_con(tmp_path)
    db.add_issuer_at_tier(con, ISSUER, "Some Co", watchlist_tier=2)
    con.commit()
    old = (date.today() - timedelta(days=60)).isoformat()
    recent = date.today().isoformat()
    db.insert_event(con, issuer_id=ISSUER, occurred=old, category="litigation",
                     severity="critical", summary="Старое", source_url=None)
    db.insert_event(con, issuer_id=ISSUER, occurred=recent, category="litigation",
                     severity="critical", summary="Свежее", source_url=None)
    con.commit()
    since = (date.today() - timedelta(days=30)).isoformat()
    events = passive_events_since(con, ISSUER, since)
    con.close()
    assert len(events) == 1
    assert events[0]["summary"] == "Свежее"


# ---- day1_snap_date fix (2026-08-23): universe_snapshot_bundle() must
# surface the TRUE earliest snap_date (MIN(snap_date) across all history),
# not just today's run date -- main() reads this back as
# market_map["earliest_snap_date"] for the dashboard header's "Первый день".

def _snapshot_row(**overrides):
    row = dict(
        secid="X", snap_date="2026-07-30", short_name="X", emitent_inn="INN1",
        emitent_title="Issuer 1", mat_date="2028-01-01", offer_date=None,
        coupon_pct=15.0, list_level=1, qualified=0, last_pct=95.0, yield_moex=20.0,
        yield_to_offer=None, accrued_int=0.0, volume=1000.0, num_trades=50,
        duration_moex=365.0, ytm=0.20, tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=600.0,
        yield_source="moex", rating="ruBB", implied_fair_spread_bp=153.0,
        mispricing_bp=447.0, in_watchlist=1, signal_excluded=0,
        mod_dur=1.0, aey_base=0.20, aey_stress=0.19, aey_bull=0.22,
        aey_base_risk_adj=0.174,
    )
    row.update(overrides)
    return row


def test_earliest_snap_date_is_min_not_latest(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    # Real gap between the true history start and today's run date --
    # exactly the bug scenario (2026-07-27 earliest vs 2026-08-06 "current run").
    db.insert_universe_snapshot(con, **_snapshot_row(snap_date="2026-07-27"))
    db.insert_universe_snapshot(con, **_snapshot_row(snap_date="2026-08-06"))
    con.commit()

    bundle = universe_snapshot_bundle(con)
    con.close()

    assert bundle["earliest_snap_date"] == "2026-07-27"
    assert bundle["earliest_snap_date"] != "2026-08-06"  # must not be the latest/current run date


def test_earliest_snap_date_matches_raw_min_query(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    for d in ("2026-07-27", "2026-07-30", "2026-08-06"):
        db.insert_universe_snapshot(con, **_snapshot_row(secid=f"X-{d}", snap_date=d))
    con.commit()

    bundle = universe_snapshot_bundle(con)
    raw_min = con.execute("SELECT MIN(snap_date) m FROM universe_snapshot").fetchone()["m"]
    con.close()
    assert bundle["earliest_snap_date"] == raw_min


def test_earliest_snap_date_none_when_no_universe_history(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    bundle = universe_snapshot_bundle(con)
    con.close()
    assert bundle["earliest_snap_date"] is None


# ---- Universe Health Scan (2026-08-29): health_status/health_source exported -

def test_bond_export_carries_health_status_and_source(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.insert_universe_snapshot(con, **_snapshot_row())
    con.execute(
        "UPDATE universe_snapshot SET health_status='orange', health_source='quantitative' "
        "WHERE secid='X' AND snap_date='2026-07-30'")
    con.commit()

    bundle = universe_snapshot_bundle(con)
    con.close()

    bonds = bundle["universe_now"]["bonds"]
    assert len(bonds) == 1
    assert bonds[0]["health_status"] == "orange"
    assert bonds[0]["health_source"] == "quantitative"


def test_bond_export_health_fields_null_before_scan_has_run(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.insert_universe_snapshot(con, **_snapshot_row())
    con.commit()

    bundle = universe_snapshot_bundle(con)
    con.close()

    bonds = bundle["universe_now"]["bonds"]
    assert bonds[0]["health_status"] is None
    assert bonds[0]["health_source"] is None


# ---- day2_snap_date fix (2026-08-26): main() previously read
# issuers[0]["snap_date"] -- whichever issuer happened to be first in the
# per-issuer export loop -- as the dashboard's "how fresh is our data"
# signal, not guaranteed to be the most recent date in the DB. Fixed to
# MAX(snap_date) FROM spread_snapshot, the table `hybond run` (the daily
# pipeline) actually writes.

def _spread_bond(con, secid):
    """spread_snapshot.secid has a FOREIGN KEY to bond -- these tests only
    care about snap_date, so a minimal bond row (and its issuer) is enough
    to satisfy the constraint."""
    db.upsert_issuer(con, ISSUER, "Test Co")
    db.upsert_bond(con, secid=secid, isin=f"RU{secid}", issuer_id=ISSUER,
                    short_name=secid, mat_date="2028-01-01", face_value=1000.0,
                    face_unit="SUR", coupon_pct=15.0, list_level=3, qualified=1,
                    updated="2026-08-01")


def test_latest_spread_snap_date_is_max_not_first_row(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    _spread_bond(con, "B1")
    _spread_bond(con, "B2")
    # Insertion order deliberately NOT date-sorted -- exactly the bug
    # scenario (whichever row happens to be "first" is not the latest).
    db.insert_spread_snapshot(con, secid="B2", snap_date="2026-08-06", ytm=0.15,
                               tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=300.0,
                               mod_dur=1.0, convexity=None)
    db.insert_spread_snapshot(con, secid="B1", snap_date="2026-08-20", ytm=0.15,
                               tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=250.0,
                               mod_dur=1.0, convexity=None)
    con.commit()

    result = _latest_spread_snap_date(con)
    con.close()
    assert result == "2026-08-20"


def test_latest_spread_snap_date_matches_raw_max_query(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    for secid, d in (("B1", "2026-07-27"), ("B2", "2026-07-30"), ("B3", "2026-08-06")):
        _spread_bond(con, secid)
        db.insert_spread_snapshot(con, secid=secid, snap_date=d, ytm=0.15,
                                   tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=300.0,
                                   mod_dur=1.0, convexity=None)
    con.commit()

    result = _latest_spread_snap_date(con)
    raw_max = con.execute("SELECT MAX(snap_date) m FROM spread_snapshot").fetchone()["m"]
    con.close()
    assert result == raw_max


def test_latest_spread_snap_date_none_when_no_history(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    result = _latest_spread_snap_date(con)
    con.close()
    assert result is None


# ---- Rating Trajectory task (2026-08-24) ------------------------------------

def test_rating_trend_value_covers_all_20_ratings():
    expected = {
        "ruAAA": 20, "ruAA+": 19, "ruAA": 18, "ruAA-": 17, "ruA+": 16, "ruA": 15, "ruA-": 14,
        "ruBBB+": 13, "ruBBB": 12, "ruBBB-": 11, "ruBB+": 10, "ruBB": 9, "ruBB-": 8,
        "ruB+": 7, "ruB": 6, "ruB-": 5, "ruCCC": 4, "ruCC": 3, "ruC": 2, "ruD": 1,
    }
    assert RATING_TREND_VALUE == expected
    assert len(RATING_TREND_VALUE) == 20


def test_rating_trend_none_for_empty_history():
    assert rating_trend([]) is None


def test_rating_trend_flat_for_single_data_point():
    history = [{"date": "2026-06-20", "rating": "ruBB+", "agency": "НКР"}]
    r = rating_trend(history)
    assert r["direction"] == "flat"
    assert r["tooltip"] == "ruBB+ (НКР)"


def test_rating_trend_up_on_upgrade():
    history = [
        {"date": "2024-06-01", "rating": "ruBB-", "agency": "НКР"},
        {"date": "2025-06-15", "rating": "ruBB", "agency": "НКР"},
        {"date": "2026-06-20", "rating": "ruBB+", "agency": "НКР"},
    ]
    r = rating_trend(history)
    assert r["direction"] == "up"
    assert r["tooltip"] == "ruBB- → ruBB → ruBB+ (НКР)"


def test_rating_trend_down_on_downgrade():
    history = [
        {"date": "2025-06-15", "rating": "ruBB+", "agency": "АКРА"},
        {"date": "2026-06-20", "rating": "ruBB-", "agency": "АКРА"},
    ]
    r = rating_trend(history)
    assert r["direction"] == "down"


def test_rating_trend_flat_when_unchanged():
    history = [
        {"date": "2025-06-15", "rating": "ruBBB", "agency": "АКРА"},
        {"date": "2026-06-20", "rating": "ruBBB", "agency": "АКРА"},
    ]
    r = rating_trend(history)
    assert r["direction"] == "flat"


def test_rating_trend_flat_for_unrecognised_rating():
    history = [
        {"date": "2025-06-15", "rating": "какой-то мусор", "agency": "АКРА"},
        {"date": "2026-06-20", "rating": "ruBBB", "agency": "АКРА"},
    ]
    r = rating_trend(history)
    assert r["direction"] == "flat"


RH_ISSUER = "7701234599"


def _rh_con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rating(con)
    db.upsert_issuer(con, RH_ISSUER, "Rating History Test Co")
    return con


def test_rating_history_by_issuer_ordered_ascending(tmp_path):
    con = _rh_con(tmp_path)
    db.upsert_rating(con, issuer_id=RH_ISSUER, agency="НКР", rating="ruBB+",
                     outlook="stable", action="confirmed", rated_on="2026-06-20", source="manual")
    db.upsert_rating(con, issuer_id=RH_ISSUER, agency="НКР", rating="ruBB-",
                     outlook="stable", action="assigned", rated_on="2024-06-01", source="manual")
    db.upsert_rating(con, issuer_id=RH_ISSUER, agency="НКР", rating="ruBB",
                     outlook="stable", action="confirmed", rated_on="2025-06-15", source="manual")
    con.commit()

    history = rating_history_by_issuer(con, [RH_ISSUER])[RH_ISSUER]
    con.close()
    assert [h["date"] for h in history] == ["2024-06-01", "2025-06-15", "2026-06-20"]
    assert [h["rating"] for h in history] == ["ruBB-", "ruBB", "ruBB+"]
    assert all(h["agency"] == "НКР" for h in history)


def test_rating_history_by_issuer_excludes_manual_agency(tmp_path):
    """agency='manual' is the watchlist-seed placeholder, not a real rating
    action -- must never appear in the exported trajectory."""
    con = _rh_con(tmp_path)
    db.upsert_rating(con, issuer_id=RH_ISSUER, agency="АКРА", rating="ruA-",
                     outlook="stable", action="confirmed", rated_on="2026-01-01", source="manual")
    db.upsert_rating(con, issuer_id=RH_ISSUER, agency="manual", rating="ruBB",
                     outlook=None, action=None, rated_on="2026-07-27", source="manual")
    con.commit()

    history = rating_history_by_issuer(con, [RH_ISSUER])[RH_ISSUER]
    con.close()
    assert len(history) == 1
    assert history[0]["agency"] == "АКРА"


def test_rating_history_by_issuer_empty_for_no_history(tmp_path):
    con = _rh_con(tmp_path)
    con.commit()
    history = rating_history_by_issuer(con, [RH_ISSUER])
    con.close()
    assert history.get(RH_ISSUER, []) == []


def test_rating_history_by_issuer_empty_issuer_ids_list(tmp_path):
    con = _rh_con(tmp_path)
    con.commit()
    assert rating_history_by_issuer(con, []) == {}


# Pre-Phase B fix: data-freshness strip for the Пресеты Builder needs both
# sources' latest snapshot dates exported (Fix 2).
def test_portfolio_data_freshness_both_sources_present(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer")
    db.upsert_bond(con, secid="BOND1", isin="RU1", issuer_id=ISSUER,
                    short_name="B1", mat_date="2028-01-01", face_value=1000.0,
                    face_unit="SUR", coupon_pct=15.0, list_level=3, qualified=1,
                    updated="2026-08-14")
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-20", ytm=0.2,
                               tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=500.0,
                               mod_dur=1.5, convexity=None)
    db.insert_spread_snapshot(con, secid="BOND1", snap_date="2026-08-25", ytm=0.2,
                               tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=500.0,
                               mod_dur=1.5, convexity=None)
    db.insert_universe_snapshot(
        con, secid="BOND1", snap_date="2026-08-01", short_name="B1",
        emitent_inn=ISSUER, emitent_title="Test Issuer", mat_date="2028-01-01",
        offer_date=None, coupon_pct=15.0, list_level=3, qualified=1,
        last_pct=100.0, yield_moex=0.2, yield_to_offer=0.2, accrued_int=0.0,
        volume=1000, num_trades=5, duration_moex=400, ytm=0.2, tenor_yrs=2.0,
        rf_rate=0.14, g_spread_bp=500.0, yield_source="moex", rating="ruBBB+",
        implied_fair_spread_bp=None, mispricing_bp=None, in_watchlist=1,
        signal_excluded=0, mod_dur=1.5, aey_base=0.2, aey_stress=0.18,
        aey_bull=0.22, aey_base_risk_adj=0.19)
    con.commit()
    result = portfolio_data_freshness(con)
    con.close()
    assert result == {"universe_snapshot_date": "2026-08-01",
                       "spread_snapshot_date": "2026-08-25"}


def test_portfolio_data_freshness_empty_db_returns_none(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    con.commit()
    result = portfolio_data_freshness(con)
    con.close()
    assert result == {"universe_snapshot_date": None, "spread_snapshot_date": None}


# --- pipeline_health_export (Pipeline Health Dashboard task, Phase 1) ------

def test_pipeline_health_export_shape_ok_when_clean(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_pipeline_health(con)
    con.commit()
    result = pipeline_health_export(con, latest_spread=None)
    con.close()
    assert result["status"] == "ok"
    assert result["unresolved_counts"] == {"warn": 0, "error": 0, "critical": 0}
    assert result["latest_error"] is None
    assert result["unresolved"] == []


def test_pipeline_health_export_includes_unresolved_entries(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_pipeline_health(con)
    health.log_health(con, 'moex', 'ingest', 'error', 'MOEX_EMPTY', 'No bonds returned')
    con.commit()
    result = pipeline_health_export(con, latest_spread=None)
    con.close()
    assert result["status"] == "error"
    assert len(result["unresolved"]) == 1
    assert result["unresolved"][0]["code"] == "MOEX_EMPTY"
    assert "id" in result["unresolved"][0]


def test_pipeline_health_export_logs_export_stale_when_spread_snapshot_old(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_pipeline_health(con)
    con.commit()
    result = pipeline_health_export(con, latest_spread="2020-01-01")
    con.close()
    codes = {r["code"] for r in result["unresolved"]}
    assert "EXPORT_STALE" in codes
    assert result["status"] == "warn"


def test_pipeline_health_export_does_not_duplicate_export_stale_same_day(tmp_path):
    """2026-08-28 fix: this export runs every 30 min via rebuild_dashboard.sh
    -- a stale snapshot must log EXPORT_STALE once per day, not once per
    cycle (log_health_once_today, not log_health)."""
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_pipeline_health(con)
    con.commit()
    pipeline_health_export(con, latest_spread="2020-01-01")
    pipeline_health_export(con, latest_spread="2020-01-01")
    rows = con.execute("SELECT * FROM pipeline_health WHERE code='EXPORT_STALE'").fetchall()
    con.close()
    assert len(rows) == 1


def test_cleanup_export_stale_duplicates_resolves_existing_unresolved(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_pipeline_health(con)
    health.log_health(con, 'dashboard', 'export', 'warn', 'EXPORT_STALE', 'dup 1')
    health.log_health(con, 'dashboard', 'export', 'warn', 'EXPORT_STALE', 'dup 2')
    con.commit()
    cleanup_export_stale_duplicates(con)
    unresolved = health.get_unresolved(con, min_level='warn')
    con.close()
    assert not any(r["code"] == "EXPORT_STALE" for r in unresolved)


def test_cleanup_export_stale_duplicates_does_not_raise_on_db_error(tmp_path, capsys):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_pipeline_health(con)
    con.close()  # force a DB error on the next operation
    cleanup_export_stale_duplicates(con)  # must not raise
    captured = capsys.readouterr()
    assert "skipped" in captured.err


# ---- export_portfolio_allocation (Portfolio donut charts, 2026-08-29) ------

def _pa_con(tmp_path, name="pa.sqlite"):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    db.init_universe(con)
    db.init_portfolio(con)
    db.init_issuer_bond(con)
    return con


def _pa_bond(con, secid, sector, health, rating, aey_base, snap_date="2026-08-27",
             issuer_id=None):
    issuer_id = issuer_id or secid  # one issuer per bond is fine for these tests
    db.upsert_issuer(con, issuer_id, f"Issuer {issuer_id}", sector=sector)
    if health is not None:
        db.set_issuer_health_status(con, issuer_id, health)
    db.add_issuer_bond(con, issuer_id, secid, source="manual")
    if rating is not None or aey_base is not None:
        db.insert_universe_snapshot(con, **_snapshot_row(
            secid=secid, snap_date=snap_date, emitent_inn=issuer_id,
            rating=rating, aey_base=aey_base))


def test_export_portfolio_allocation_no_active_portfolio_returns_empty(tmp_path):
    con = _pa_con(tmp_path)
    assert export_portfolio_allocation(con) == {}


def test_export_portfolio_allocation_active_portfolio_zero_bonds_returns_empty(tmp_path):
    con = _pa_con(tmp_path)
    db.create_portfolio(con, "Пустой", "conservative", status="active")
    con.commit()
    assert export_portfolio_allocation(con) == {}


def test_export_portfolio_allocation_basic_shape(tmp_path):
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB+", 0.25)
    _pa_bond(con, "B2", "leasing", "green", "ruBBB", 0.30)
    _pa_bond(con, "B3", "energy", "yellow", "ruBB+", 0.35)
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    for secid in ("B1", "B2", "B3"):
        db.add_portfolio_bond(con, pid, secid, weight=1 / 3)
    con.commit()

    result = export_portfolio_allocation(con)
    assert set(result.keys()) == {"bonds", "sector_allocation", "rating_allocation",
                                   "health_allocation", "summary"}
    assert len(result["bonds"]) == 3
    b1 = next(b for b in result["bonds"] if b["isin"] == "B1")
    assert b1["sector"] == "leasing"
    assert b1["rating"] == "ruBBB+"
    assert abs(b1["weight"] - 1 / 3) < 1e-9
    assert abs(b1["aey_base"] - 0.25) < 1e-9

    summary = result["summary"]
    assert summary["total_bonds"] == 3
    assert summary["n_sectors"] == 2
    assert summary["n_ratings"] == 3
    assert summary["preset_type"] == "conservative"
    assert summary["name"] == "Тест"
    assert abs(summary["avg_aey"] - (0.25 + 0.30 + 0.35) / 3) < 1e-9

    sectors = result["sector_allocation"]
    assert abs(sectors["leasing"]["weight"] - 2 / 3) < 1e-9
    assert sectors["leasing"]["bond_count"] == 2
    assert abs(sectors["leasing"]["avg_aey"] - (0.25 + 0.30) / 2) < 1e-9
    assert sectors["leasing"]["color"] == SECTOR_COLORS["leasing"]
    assert abs(sectors["energy"]["weight"] - 1 / 3) < 1e-9

    ratings = result["rating_allocation"]
    assert ratings["ruBBB+"]["bond_count"] == 1
    assert ratings["ruBB+"]["bond_count"] == 1

    health = result["health_allocation"]
    assert health["green"]["bond_count"] == 2
    assert health["yellow"]["bond_count"] == 1
    assert abs(health["green"]["weight"] - 2 / 3) < 1e-9


def test_export_portfolio_allocation_includes_universe_only_bond(tmp_path):
    con = _pa_con(tmp_path)
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="U1", short_name="Rated universe bond", emitent_inn="PASSIVE",
        emitent_title="Passive issuer", rating="ruAAA", aey_base=0.12,
        aey_base_risk_adj=0.12, in_watchlist=0))
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "U1", weight=1.0)
    con.commit()

    result = export_portfolio_allocation(con)

    assert result["summary"]["total_bonds"] == 1
    assert result["summary"]["n_issuers"] == 1
    assert result["bonds"][0]["issuer_id"] == "PASSIVE"
    assert result["bonds"][0]["sector"] == "other"
    assert result["bonds"][0]["rating"] == "ruAAA"
    assert result["bonds"][0]["aey_base"] == 0.12


def test_export_portfolio_allocation_missing_rating_excluded_from_n_ratings(tmp_path):
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.25)
    _pa_bond(con, "B2", "leasing", "green", None, None)  # no universe_snapshot row
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "B1", weight=0.5)
    db.add_portfolio_bond(con, pid, "B2", weight=0.5)
    con.commit()

    result = export_portfolio_allocation(con)
    assert result["summary"]["n_ratings"] == 1  # "no_rating" isn't a rating
    assert result["summary"]["total_bonds"] == 2
    ratings = result["rating_allocation"]
    assert "no_rating" in ratings
    assert ratings["no_rating"]["bond_count"] == 1
    assert ratings["no_rating"]["avg_aey"] is None
    # The missing-AEY bond doesn't silently pull the overall/bucket average
    # toward 0 -- it's simply excluded from the weighted-avg denominator.
    assert abs(result["summary"]["avg_aey"] - 0.25) < 1e-9
    assert abs(result["sector_allocation"]["leasing"]["avg_aey"] - 0.25) < 1e-9


def test_export_portfolio_allocation_unmapped_sector_falls_back_to_other_color(tmp_path):
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "technology", "green", "ruBBB", 0.25)
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "B1", weight=1.0)
    con.commit()

    result = export_portfolio_allocation(con)
    assert result["sector_allocation"]["technology"]["color"] == SECTOR_COLORS["other"]


def test_export_portfolio_allocation_only_latest_snap_date_used(tmp_path):
    con = _pa_con(tmp_path)
    db.upsert_issuer(con, "I1", "Issuer 1", sector="leasing")
    db.set_issuer_health_status(con, "I1", "green")
    db.add_issuer_bond(con, "I1", "B1", source="manual")
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="B1", snap_date="2026-08-01", emitent_inn="I1",
        rating="ruB", aey_base=0.50))  # stale, wrong rating/aey
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="B1", snap_date="2026-08-27", emitent_inn="I1",
        rating="ruBBB+", aey_base=0.25))  # latest, correct
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "B1", weight=1.0)
    con.commit()

    result = export_portfolio_allocation(con)
    assert result["summary"]["total_bonds"] == 1  # no fan-out across snap_dates
    assert "ruBBB+" in result["rating_allocation"]
    assert "ruB" not in result["rating_allocation"]
    assert abs(result["summary"]["avg_aey"] - 0.25) < 1e-9


def test_export_portfolio_allocation_weight_normalized_to_one(tmp_path):
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.25)
    _pa_bond(con, "B2", "energy", "green", "ruBBB", 0.25)
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    # Raw weights that DON'T sum to 1 -- allocation must still normalize to
    # fractions of the actual total, not assume equal-weight bookkeeping.
    db.add_portfolio_bond(con, pid, "B1", weight=1.0)
    db.add_portfolio_bond(con, pid, "B2", weight=3.0)
    con.commit()

    result = export_portfolio_allocation(con)
    assert abs(result["sector_allocation"]["leasing"]["weight"] - 0.25) < 1e-9
    assert abs(result["sector_allocation"]["energy"]["weight"] - 0.75) < 1e-9


def test_export_portfolio_allocation_picks_most_recently_created_active(tmp_path):
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.25)
    _pa_bond(con, "B2", "energy", "green", "ruBBB", 0.30)
    p_old = db.create_portfolio(con, "Старый", "conservative", status="active")
    db.add_portfolio_bond(con, p_old, "B1", weight=1.0)
    con.commit()
    con.execute("UPDATE portfolio SET created_at='2020-01-01' WHERE id=?", (p_old,))
    p_new = db.create_portfolio(con, "Новый", "aggressive", status="active")
    db.add_portfolio_bond(con, p_new, "B2", weight=1.0)
    con.commit()

    result = export_portfolio_allocation(con)
    assert result["summary"]["name"] == "Новый"
    assert result["summary"]["preset_type"] == "aggressive"
    assert "energy" in result["sector_allocation"]
    assert "leasing" not in result["sector_allocation"]


# ---- Multiple portfolio comparison (2026-08-31) ----------------------------

def test_export_portfolio_allocation_computes_n_issuers_and_max_duration(tmp_path):
    con = _pa_con(tmp_path)
    # Two bonds from the SAME issuer must count as one issuer, not two.
    db.upsert_issuer(con, "I1", "Issuer 1", sector="leasing")
    db.add_issuer_bond(con, "I1", "B1", source="manual")
    db.add_issuer_bond(con, "I1", "B2", source="manual")
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="B1", emitent_inn="I1", rating="ruBBB", aey_base=0.25, mod_dur=2.0))
    db.insert_universe_snapshot(con, **_snapshot_row(
        secid="B2", emitent_inn="I1", rating="ruBBB", aey_base=0.25, mod_dur=3.5))
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "B1", weight=0.5)
    db.add_portfolio_bond(con, pid, "B2", weight=0.5)
    con.commit()

    result = export_portfolio_allocation(con)
    assert result["summary"]["n_issuers"] == 1
    assert abs(result["summary"]["max_duration"] - 3.5) < 1e-9


def test_export_portfolio_allocation_expected_loss_and_net_return_from_snapshot(tmp_path):
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.30)
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "B1", weight=1.0)
    db.add_portfolio_snapshot(con, portfolio_id=pid, snapshot_date="2026-08-31",
                               aey=0.30, expected_loss=0.05,
                               metrics_json="{}")
    con.commit()

    result = export_portfolio_allocation(con)
    assert abs(result["summary"]["expected_loss"] - 0.05) < 1e-9
    assert abs(result["summary"]["net_return"] - (0.30 - 0.05)) < 1e-9


def test_export_portfolio_allocation_uses_latest_snapshot_not_first(tmp_path):
    """monitor_portfolios() writes a NEW snapshot row on every run -- the
    most recent one (highest id) must win, not the save-time one."""
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.30)
    pid = db.create_portfolio(con, "Тест", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "B1", weight=1.0)
    db.add_portfolio_snapshot(con, portfolio_id=pid, snapshot_date="2026-08-29",
                               aey=0.30, expected_loss=0.05, metrics_json="{}")
    db.add_portfolio_snapshot(con, portfolio_id=pid, snapshot_date="2026-08-31",
                               aey=0.28, expected_loss=0.02, metrics_json="{}")
    con.commit()

    result = export_portfolio_allocation(con)
    assert abs(result["summary"]["expected_loss"] - 0.02) < 1e-9


def test_export_portfolio_allocation_by_explicit_id_ignores_status(tmp_path):
    """portfolio_id= lets a caller pick a specific portfolio directly,
    distinct from the status='active'-only default lookup."""
    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.25)
    pid = db.create_portfolio(con, "Черновик", "conservative", status="draft")
    db.add_portfolio_bond(con, pid, "B1", weight=1.0)
    con.commit()

    assert export_portfolio_allocation(con) == {}  # no active portfolio
    result = export_portfolio_allocation(con, portfolio_id=pid)
    assert result["summary"]["name"] == "Черновик"


def test_export_all_active_portfolios_returns_up_to_three_newest(tmp_path):
    from export_dashboard_data import export_all_active_portfolios

    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.25)
    _pa_bond(con, "B2", "energy", "green", "ruBBB", 0.30)
    _pa_bond(con, "B3", "mfo", "green", "ruBB", 0.35)
    _pa_bond(con, "B4", "retail", "green", "ruB", 0.40)

    for i, (name, secid, created) in enumerate([
        ("Старый", "B4", "2020-01-01"),
        ("Консервативный", "B1", "2026-08-29"),
        ("Сбалансированный", "B2", "2026-08-30"),
        ("Агрессивный", "B3", "2026-08-31"),
    ]):
        pid = db.create_portfolio(con, name, "conservative", status="active")
        db.add_portfolio_bond(con, pid, secid, weight=1.0)
        con.execute("UPDATE portfolio SET created_at=? WHERE id=?", (created, pid))
    con.commit()

    result = export_all_active_portfolios(con)
    assert len(result) == 3  # capped at 3, oldest ("Старый") dropped
    names = [p["name"] for p in result]
    assert names == ["Агрессивный", "Сбалансированный", "Консервативный"]
    assert all("allocation" in p and p["allocation"] for p in result)


def test_export_all_active_portfolios_skips_active_with_zero_bonds(tmp_path):
    from export_dashboard_data import export_all_active_portfolios

    con = _pa_con(tmp_path)
    _pa_bond(con, "B1", "leasing", "green", "ruBBB", 0.25)
    db.create_portfolio(con, "Пустой", "conservative", status="active")  # no bonds
    pid2 = db.create_portfolio(con, "С бумагами", "conservative", status="active")
    db.add_portfolio_bond(con, pid2, "B1", weight=1.0)
    con.commit()

    result = export_all_active_portfolios(con)
    assert len(result) == 1
    assert result[0]["name"] == "С бумагами"
