"""Tests for the `rating` table -- the bridge feeding the Default
Probability Layer (see DEFAULT_PROB_FEATURE.md) from watchlist/manual/agency
sources into the factlist and spread screen.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.factlist.assemble import build_factlist  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "7701234567"


def _base_con(tmp_path, name):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    db.init_rating(con)
    db.upsert_issuer(con, ISSUER, "ООО ТестРейтинг", "microfinance")
    return con


def test_current_rating_none_when_no_rows(tmp_path):
    con = _base_con(tmp_path, "r1.sqlite")
    assert db.current_rating(con, ISSUER) is None
    con.close()


def test_upsert_and_current_rating(tmp_path):
    con = _base_con(tmp_path, "r2.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-07-01")
    con.commit()
    assert db.current_rating(con, ISSUER) == "ruBB"
    con.close()


def test_upsert_same_agency_new_date_flips_prior_to_not_current(tmp_path):
    con = _base_con(tmp_path, "r3.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-01-01")
    con.commit()
    db.upsert_rating(con, ISSUER, "manual", "ruBB+", "positive", "upgraded", "2026-07-01")
    con.commit()

    hist = db.rating_history(con, ISSUER)
    assert len(hist) == 2
    current = [r for r in hist if r["is_current"] == 1]
    assert len(current) == 1
    assert current[0]["rating"] == "ruBB+"
    assert db.current_rating(con, ISSUER) == "ruBB+"
    con.close()


def test_upsert_idempotent_on_same_pk(tmp_path):
    con = _base_con(tmp_path, "r4.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-07-01")
    con.commit()
    db.upsert_rating(con, ISSUER, "manual", "ruBB", "stable", "confirmed", "2026-07-01")
    con.commit()

    hist = db.rating_history(con, ISSUER)
    assert len(hist) == 1
    assert hist[0]["action"] == "confirmed"
    con.close()


def test_current_rating_prefers_agency_priority_on_tied_date(tmp_path):
    con = _base_con(tmp_path, "r5.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-07-01")
    db.upsert_rating(con, ISSUER, "АКРА", "ruBBB-", None, None, "2026-07-01")
    con.commit()
    # both is_current=1 (different agencies), same date -- АКРА should win
    assert db.current_rating(con, ISSUER) == "ruBBB-"
    con.close()


def test_current_rating_prefers_more_recent_date_over_agency(tmp_path):
    con = _base_con(tmp_path, "r6.sqlite")
    db.upsert_rating(con, ISSUER, "АКРА", "ruBBB-", None, None, "2026-01-01")
    db.upsert_rating(con, ISSUER, "manual", "ruB+", None, None, "2026-07-01")
    con.commit()
    # manual is lower priority but strictly more recent -- recency wins
    assert db.current_rating(con, ISSUER) == "ruB+"
    con.close()


def test_rating_history_ordered_newest_first(tmp_path):
    con = _base_con(tmp_path, "r7.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-01-01")
    db.upsert_rating(con, ISSUER, "manual", "ruBB+", None, None, "2026-07-01")
    con.commit()
    hist = db.rating_history(con, ISSUER)
    assert [r["rated_on"] for r in hist] == ["2026-07-01", "2026-01-01"]
    con.close()


def test_factlist_pulls_default_prob_from_rating_table(tmp_path):
    """Regression guard: this used to read six_pillars.get('rating'), which
    was always None because nothing ever wrote a rating there. Now it must
    read from the rating table via db.current_rating."""
    con = _base_con(tmp_path, "r8.sqlite")
    db.init_pillars(con); db.init_filing(con)
    db.upsert_bond(con, secid="RU000R8", isin="RU000R8", issuer_id=ISSUER,
                   short_name="ТестР8", mat_date="2028-07-25", face_value=1000.0,
                   face_unit="SUR", coupon_pct=18.0, list_level=3, qualified=1,
                   updated="2026-07-25")
    db.insert_spread_snapshot(con, secid="RU000R8", snap_date="2026-07-25",
                              ytm=0.2, tenor_yrs=2.0, rf_rate=0.14,
                              g_spread_bp=600.0, mod_dur=1.5, convexity=3.0)
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-07-01")
    con.commit()

    fl = build_factlist(con, ISSUER, "2026-07-25", flags=[],
                        stat_warming_up=True).to_json()
    assert fl["default_prob_1yr"] == 0.03056
    assert fl["implied_fair_spread_bp"] is not None
    assert fl["mispricing_bp"] is not None
    con.close()


def test_factlist_default_prob_none_when_no_rating(tmp_path):
    con = _base_con(tmp_path, "r9.sqlite")
    db.init_pillars(con); db.init_filing(con)
    fl = build_factlist(con, ISSUER, "2026-07-25", flags=[],
                        stat_warming_up=True).to_json()
    assert fl["default_prob_1yr"] is None
    assert fl["implied_fair_spread_bp"] is None
    assert fl["mispricing_bp"] is None
    con.close()
