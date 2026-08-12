"""Tests for the event-table price-impact migration + fill_price_impact()
and skip_noise_filings() (Event Pipeline Audit + Price Impact Schema task).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.store import db  # noqa: E402

ISSUER = "1234567890"
SECID_LIQUID = "RU000A1LIQ01"   # higher avg volume -- should be picked
SECID_THIN = "RU000A1THN01"     # lower avg volume -- should NOT be picked


def _fresh_con(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "price_impact.sqlite")
    con = db.connect()
    db.init_db(con)
    return con


def test_event_table_has_price_impact_columns(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(event)")}
    for col in ("price_before_bp", "price_after_3d_bp", "price_after_7d_bp",
                "price_after_30d_bp", "spread_delta_30d_bp", "price_impact_filled_on"):
        assert col in cols
    con.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    db.init_db(con)  # second call must not raise "duplicate column"
    con.close()


def _seed_issuer_and_bonds(con):
    db.upsert_issuer(con, ISSUER, "Test Issuer", "industrial")
    db.upsert_bond(con, secid=SECID_LIQUID, isin="X1", issuer_id=ISSUER,
                    short_name="Test-01", mat_date="2028-01-01", face_value=1000,
                    face_unit="RUB", coupon_pct=12.0, list_level=1, qualified=0,
                    updated="2026-07-01")
    db.upsert_bond(con, secid=SECID_THIN, isin="X2", issuer_id=ISSUER,
                    short_name="Test-02", mat_date="2028-01-01", face_value=1000,
                    face_unit="RUB", coupon_pct=12.0, list_level=1, qualified=0,
                    updated="2026-07-01")
    # SECID_LIQUID: high volume every day -> must be the reference bond
    for d, vol in [("2026-07-01", 5_000_000), ("2026-07-02", 6_000_000), ("2026-07-03", 4_500_000)]:
        db.insert_price_snapshot(con, secid=SECID_LIQUID, snap_date=d, last_pct=101,
                                  yield_moex=12.0, duration=500, volume=vol, num_trades=10, systime=d)
    # SECID_THIN: low volume -> must NOT be picked over SECID_LIQUID
    for d, vol in [("2026-07-01", 1000), ("2026-07-02", 500), ("2026-07-03", 800)]:
        db.insert_price_snapshot(con, secid=SECID_THIN, snap_date=d, last_pct=99,
                                  yield_moex=15.0, duration=500, volume=vol, num_trades=1, systime=d)


def test_ref_bond_picks_highest_average_volume(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    _seed_issuer_and_bonds(con)
    assert db._price_impact_ref_bond(con, ISSUER) == SECID_LIQUID
    con.close()


def test_fill_price_impact_fills_before_and_after(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    _seed_issuer_and_bonds(con)
    # Spread history bracketing the event date on both sides.
    spreads = [
        ("2026-07-14", 400.0), ("2026-07-15", 410.0),  # before
        ("2026-07-16", 500.0),   # event day itself
        ("2026-07-17", 520.0), ("2026-07-20", 540.0), ("2026-07-21", 560.0),  # +1,+2,+3 trading days
        ("2026-08-15", 600.0),  # ~30 calendar days after
    ]
    for d, g in spreads:
        db.insert_spread_snapshot(con, secid=SECID_LIQUID, snap_date=d, ytm=0.2,
                                   tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=g,
                                   mod_dur=1.8, convexity=None)
    db.insert_event(con, issuer_id=ISSUER, occurred="2026-07-16", category="rating_action",
                     severity="high", summary="Test event", source_url="http://x")
    con.commit()

    result = db.fill_price_impact(con)
    assert result["updated"] == 1
    assert result["still_null"] == 0

    row = con.execute("SELECT * FROM event WHERE issuer_id=?", (ISSUER,)).fetchone()
    assert row["price_before_bp"] == 410.0  # nearest snap_date <= 2026-07-16
    assert row["price_after_30d_bp"] == 600.0
    assert row["spread_delta_30d_bp"] == 600.0 - 410.0
    assert row["price_impact_filled_on"] == date.today().isoformat()


def test_fill_price_impact_never_overwrites_existing_fill(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    _seed_issuer_and_bonds(con)
    ev_id = db.insert_event(con, issuer_id=ISSUER, occurred="2026-07-16",
                             category="rating_action", severity="high",
                             summary="Test event", source_url="http://x")
    con.execute("UPDATE event SET price_before_bp=999.0 WHERE event_id=?", (ev_id,))
    con.commit()
    db.fill_price_impact(con)
    row = con.execute("SELECT price_before_bp FROM event WHERE event_id=?", (ev_id,)).fetchone()
    assert row["price_before_bp"] == 999.0


def test_fill_price_impact_leaves_null_when_no_spread_data(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    db.upsert_issuer(con, "no-bond-issuer", "No Bond Issuer", None)
    db.insert_event(con, issuer_id="no-bond-issuer", occurred="2026-07-16",
                     category="rating_action", severity="high",
                     summary="Test event", source_url="http://x")
    con.commit()
    result = db.fill_price_impact(con)
    assert result["updated"] == 0
    assert result["still_null"] == 1
    row = con.execute("SELECT price_before_bp FROM event WHERE issuer_id=?",
                       ("no-bond-issuer",)).fetchone()
    assert row["price_before_bp"] is None


def test_skip_noise_filings_marks_classified_and_inserts_routine_event(tmp_path, monkeypatch):
    con = _fresh_con(tmp_path, monkeypatch)
    db.init_filing(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer", None)
    db.insert_filing(con, body_hash="h1", issuer_id=ISSUER, edisclosure_id=ISSUER,
                      published="2026-07-01", title="Плановая выплата купона по графику",
                      url="http://x", fetched_on="2026-07-01")
    con.commit()

    rows = db.unclassified_filings(con)
    assert len(rows) == 1
    n = db.skip_noise_filings(con, rows)
    assert n == 1

    remaining = db.unclassified_filings(con)
    assert remaining == []
    ev = con.execute("SELECT * FROM event WHERE issuer_id=?", (ISSUER,)).fetchone()
    assert ev["category"] == "routine"
    assert ev["severity"] == "noise"
    assert ev["summary"] == "Плановая выплата купона по графику"
    con.close()
