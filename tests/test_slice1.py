"""Slice 1 tests. Run: python -m pytest tests/ -q  (from repo root, with src on path)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.compute import bondmath, cashflows  # noqa: E402
from hybond.calendar import wall  # noqa: E402
from hybond.store import db  # noqa: E402
from fixtures import sample  # noqa: E402


# --- bondmath: textbook anchors -------------------------------------------

def test_ytm_textbook():
    # 3y, 5% annual coupon, price 950 -> YTM ~6.90%
    cfs = [(1, 50), (2, 50), (3, 1050)]
    y = bondmath.ytm_from_price(950, cfs)
    assert abs(y - 0.0690) < 1e-3


def test_duration_textbook():
    cfs = [(1, 50), (2, 50), (3, 1050)]
    y = bondmath.ytm_from_price(950, cfs)
    assert abs(bondmath.macaulay_duration(y, cfs) - 2.8555) < 1e-2
    assert abs(bondmath.modified_duration(y, cfs) - 2.6711) < 1e-2


def test_price_ytm_roundtrip():
    cfs = [(0.5, 45), (1.0, 45), (1.5, 1045)]
    for y in (0.05, 0.12, 0.20, 0.35):
        p = bondmath.price_from_ytm(y, cfs)
        assert abs(bondmath.ytm_from_price(p, cfs) - y) < 1e-4


def test_curve_interp_and_clamp():
    c = sample.CURVE
    # exact node
    assert abs(bondmath.interp_curve(1.0, c) - 0.144) < 1e-9
    # midpoint between 1y (.144) and 2y (.140) -> .142
    assert abs(bondmath.interp_curve(1.5, c) - 0.142) < 1e-9
    # clamp below and above
    assert bondmath.interp_curve(0.1, c) == c[0][1]
    assert bondmath.interp_curve(50, c) == sorted(c)[-1][1]


def test_g_spread_positive_for_hy():
    cfs = cashflows.build_cashflows(sample.COUPONS, sample.AMORTS,
                                    sample.SETTLEMENT, 1000.0)
    pairs = cashflows.to_pairs(cfs, sample.SETTLEMENT)
    dirty = sample.LAST_PCT / 100.0 * 1000.0
    ytm = bondmath.ytm_from_price(dirty, pairs)
    tenor = (cashflows.final_maturity(cfs) - sample.SETTLEMENT).days / 365.0
    gsp = bondmath.g_spread(ytm, tenor, sample.CURVE) * 1e4
    # discounted ~18% coupon bond must yield well above the ~14% RF -> wide spread
    assert ytm > 0.14
    assert gsp > 500  # comfortably triple-digit-plus bp


# --- cashflows -------------------------------------------------------------

def test_cashflows_filter_and_sort():
    cfs = cashflows.build_cashflows(sample.COUPONS, sample.AMORTS,
                                    sample.SETTLEMENT, 1000.0)
    assert all(f.when > sample.SETTLEMENT for f in cfs)
    assert cfs == sorted(cfs, key=lambda f: f.when)
    assert cashflows.final_maturity(cfs) == date(2028, 7, 25)
    nc = cashflows.next_coupon(cfs)
    assert nc.when == date(2026, 10, 25)


# --- calendar --------------------------------------------------------------

def test_maturity_wall_buckets():
    buckets = wall.build_wall(sample.FLOWS_BY_ISSUER, horizon_months=24,
                              today=date(2026, 7, 25))
    by_month = {b.month: b for b in buckets}
    # Feb 2027 has issuer A's 1000 redemption
    assert by_month["2027-02"].principal == 1000.0
    # Sep 2026 has issuer B's 500 amortization + 25 coupon
    assert by_month["2026-09"].principal == 500.0
    assert by_month["2026-09"].coupon == 25.0
    assert "7707654321" in by_month["2026-09"].issuers


# --- registered-vs-placed volume (Task 3 follow-up) ------------------------

def test_upsert_bond_tracks_placement_partial_since(tmp_path):
    con = db.connect(tmp_path / "placement.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "7701234567", "ТестЭмитент", "industrials")

    # first observation: 11.78% placed -> partial_since set to today's snap date
    db.upsert_bond(con, secid="RU000PART1", isin="RU000PART1", issuer_id="7701234567",
                   short_name="Тест2", mat_date="2029-01-01", face_value=1000.0,
                   face_unit="SUR", coupon_pct=20.0, list_level=3, qualified=1,
                   updated="2026-07-01", issue_size=300000, issue_size_placed=35325,
                   placement_pct=11.78)
    con.commit()
    row = con.execute("SELECT * FROM bond WHERE secid='RU000PART1'").fetchone()
    assert row["placement_pct"] == 11.78
    assert row["placement_partial_since"] == "2026-07-01"

    # second observation, still partial a month later -> partial_since NOT
    # bumped forward (it tracks when partial placement was FIRST seen)
    db.upsert_bond(con, secid="RU000PART1", isin="RU000PART1", issuer_id="7701234567",
                   short_name="Тест2", mat_date="2029-01-01", face_value=1000.0,
                   face_unit="SUR", coupon_pct=20.0, list_level=3, qualified=1,
                   updated="2026-08-01", issue_size=300000, issue_size_placed=60000,
                   placement_pct=20.0)
    con.commit()
    row = con.execute("SELECT * FROM bond WHERE secid='RU000PART1'").fetchone()
    assert row["placement_pct"] == 20.0
    assert row["placement_partial_since"] == "2026-07-01"

    # third observation, fully placed -> partial_since cleared
    db.upsert_bond(con, secid="RU000PART1", isin="RU000PART1", issuer_id="7701234567",
                   short_name="Тест2", mat_date="2029-01-01", face_value=1000.0,
                   face_unit="SUR", coupon_pct=20.0, list_level=3, qualified=1,
                   updated="2026-09-01", issue_size=300000, issue_size_placed=300000,
                   placement_pct=100.0)
    con.commit()
    row = con.execute("SELECT * FROM bond WHERE secid='RU000PART1'").fetchone()
    assert row["placement_partial_since"] is None


def test_upsert_bond_omitted_placement_fields_dont_erase_prior_data(tmp_path):
    """A caller that doesn't pass placement fields at all (existing tests,
    or a run where MOEX temporarily drops the columns) must not wipe out
    previously-stored real placement data, nor reset the stall clock."""
    con = db.connect(tmp_path / "placement2.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "7701234567", "ТестЭмитент", "industrials")
    db.upsert_bond(con, secid="RU000PART2", isin="RU000PART2", issuer_id="7701234567",
                   short_name="Тест3", mat_date="2029-01-01", face_value=1000.0,
                   face_unit="SUR", coupon_pct=20.0, list_level=3, qualified=1,
                   updated="2026-07-01", issue_size=100000, issue_size_placed=10000,
                   placement_pct=10.0)
    con.commit()
    db.upsert_bond(con, secid="RU000PART2", isin="RU000PART2", issuer_id="7701234567",
                   short_name="Тест3", mat_date="2029-01-01", face_value=1000.0,
                   face_unit="SUR", coupon_pct=20.0, list_level=3, qualified=1,
                   updated="2026-07-15")  # placement fields omitted this run
    con.commit()
    row = con.execute("SELECT * FROM bond WHERE secid='RU000PART2'").fetchone()
    assert row["placement_pct"] == 10.0  # preserved, not wiped to NULL
    assert row["issue_size"] == 100000
    assert row["placement_partial_since"] == "2026-07-01"  # stall clock untouched


def test_placement_is_stalled():
    # not stalled: still None/missing partial_since
    assert db.placement_is_stalled(20.0, None) is False
    # not stalled: already >= 50%
    assert db.placement_is_stalled(60.0, "2026-01-01", as_of=date(2026, 8, 1)) is False
    # not stalled: below 50% but not yet 4 weeks since first seen partial
    assert db.placement_is_stalled(20.0, "2026-07-25", as_of=date(2026, 8, 1)) is False
    # stalled: below 50%, exactly 4 weeks (28 days) since first seen partial
    assert db.placement_is_stalled(20.0, "2026-07-04", as_of=date(2026, 8, 1)) is True
    # stalled: below 50%, well past 4 weeks
    assert db.placement_is_stalled(12.0, "2026-01-01", as_of=date(2026, 8, 1)) is True
    # not stalled: no placement_pct at all
    assert db.placement_is_stalled(None, "2026-01-01", as_of=date(2026, 8, 1)) is False


def test_redemptions_by_issuer():
    reds = wall.redemptions_by_issuer(sample.FLOWS_BY_ISSUER,
                                      today=date(2026, 7, 25))
    assert reds["7701234567"] == [(date(2027, 2, 15), 1000.0)]
    assert len(reds["7707654321"]) == 2  # two amortization tranches


# --- store -----------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "7701234567", "ТестЭмитент", "industrials")
    db.upsert_bond(con, secid="RU000TEST01", isin="RU000TEST01",
                   issuer_id="7701234567", short_name="Тест1",
                   mat_date="2028-07-25", face_value=1000.0, face_unit="SUR",
                   coupon_pct=18.0, list_level=3, qualified=1, updated="2026-07-25")
    db.insert_spread_snapshot(con, secid="RU000TEST01", snap_date="2026-07-25",
                              ytm=0.19, tenor_yrs=2.0, rf_rate=0.14,
                              g_spread_bp=500.0, mod_dur=1.6, convexity=3.1)
    con.commit()
    row = con.execute("SELECT g_spread_bp FROM spread_snapshot").fetchone()
    assert row["g_spread_bp"] == 500.0
    # snapshot is append-only keyed by (secid, date): re-insert same day replaces,
    # different day coexists
    db.insert_spread_snapshot(con, secid="RU000TEST01", snap_date="2026-07-26",
                              ytm=0.20, tenor_yrs=1.99, rf_rate=0.14,
                              g_spread_bp=600.0, mod_dur=1.59, convexity=3.0)
    con.commit()
    n = con.execute("SELECT COUNT(*) c FROM spread_snapshot").fetchone()["c"]
    assert n == 2
    con.close()
