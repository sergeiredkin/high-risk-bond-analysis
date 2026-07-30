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


def test_current_rating_prefers_more_recent_date_among_real_agencies(tmp_path):
    con = _base_con(tmp_path, "r6.sqlite")
    db.upsert_rating(con, ISSUER, "АКРА", "ruBBB-", None, None, "2026-01-01")
    db.upsert_rating(con, ISSUER, "NKR", "ruB+", None, None, "2026-07-01")
    con.commit()
    # both real agencies -- strictly more recent date wins
    assert db.current_rating(con, ISSUER) == "ruB+"
    con.close()


def test_current_rating_real_agency_beats_more_recent_manual_row(tmp_path):
    con = _base_con(tmp_path, "r8.sqlite")
    db.upsert_rating(con, ISSUER, "АКРА", "ruBBB-", None, None, "2026-01-01")
    db.upsert_rating(con, ISSUER, "manual", "ruB+", None, None, "2026-07-01")
    con.commit()
    # manual row is strictly more recent but must NOT override a real
    # agency's rating -- manual is an unverified bootstrap, not a signal
    # that should ever outrank a genuine agency action.
    assert db.current_rating(con, ISSUER) == "ruBBB-"
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


def test_new_rating_defaults_needs_verification_null(tmp_path):
    # upsert_rating's own default (None) is neither 0 nor 1 -- callers must
    # decide explicitly; only init_rating's one-time backfill sets it based
    # on agency='manual'.
    con = _base_con(tmp_path, "r10.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruBB", None, None, "2026-07-01")
    con.commit()
    row = con.execute("SELECT verified_on, needs_verification FROM rating "
                      "WHERE issuer_id=?", (ISSUER,)).fetchone()
    assert row["verified_on"] is None
    assert row["needs_verification"] is None
    con.close()


def test_upsert_rating_can_set_verified_on_and_clear_flag(tmp_path):
    con = _base_con(tmp_path, "r11.sqlite")
    db.upsert_rating(con, ISSUER, "Эксперт РА", "ruA+", "developing", "downgraded",
                     "2026-06-30", verified_on="2026-07-30", needs_verification=0)
    con.commit()
    row = con.execute("SELECT verified_on, needs_verification FROM rating "
                      "WHERE issuer_id=?", (ISSUER,)).fetchone()
    assert row["verified_on"] == "2026-07-30"
    assert row["needs_verification"] == 0
    con.close()


def test_retire_rating_flips_all_rows_under_that_agency(tmp_path):
    # Needed when superseding a stale 'manual' bootstrap row with a real,
    # differently-dated agency row -- upsert_rating's own supersession only
    # retires rows under the SAME agency, so a stale 'manual' row would
    # otherwise keep is_current=1 forever once a different agency is used.
    con = _base_con(tmp_path, "r12.sqlite")
    db.upsert_rating(con, ISSUER, "manual", "ruA-", None, None, "2026-07-27")
    con.commit()
    db.retire_rating(con, ISSUER, "manual")
    db.upsert_rating(con, ISSUER, "Эксперт РА", "ruA+", "developing", "downgraded",
                     "2026-06-30", verified_on="2026-07-30", needs_verification=0)
    con.commit()
    hist = db.rating_history(con, ISSUER)
    current = [r for r in hist if r["is_current"] == 1]
    assert len(current) == 1
    assert current[0]["rating"] == "ruA+"
    # earlier rated_on than the retired manual row -- would lose the
    # date-first tiebreak if the manual row hadn't been explicitly retired.
    assert db.current_rating(con, ISSUER) == "ruA+"
    con.close()


def test_init_rating_migrates_and_backfills_pre_existing_table(tmp_path):
    # Simulates a DB created before verified_on/needs_verification existed
    # (the real production DB's actual situation on 2026-07-30) -- a bare
    # CREATE TABLE matching the OLD schema, populated directly via SQL
    # (bypassing upsert_rating, which already knows about the new columns),
    # then init_rating() must both add the columns AND backfill by agency.
    con = db.connect(tmp_path / "r13.sqlite")
    con.execute("""
        CREATE TABLE rating (
            issuer_id TEXT, secid TEXT, agency TEXT NOT NULL, rating TEXT NOT NULL,
            outlook TEXT, action TEXT, rated_on TEXT NOT NULL,
            source TEXT DEFAULT 'manual', is_current INTEGER DEFAULT 1,
            PRIMARY KEY (issuer_id, agency, rated_on)
        )
    """)
    con.execute("INSERT INTO rating(issuer_id,agency,rating,rated_on) "
               "VALUES(?,?,?,?)", (ISSUER, "manual", "ruBB", "2026-07-01"))
    con.execute("INSERT INTO rating(issuer_id,agency,rating,rated_on) "
               "VALUES(?,?,?,?)", (ISSUER, "АКРА", "ruBBB-", "2026-07-01"))
    con.commit()

    db.init_rating(con)  # should ALTER TABLE + backfill, since columns are new

    rows = {r["agency"]: r["needs_verification"] for r in
            con.execute("SELECT agency, needs_verification FROM rating")}
    assert rows["manual"] == 1
    assert rows["АКРА"] == 0
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
