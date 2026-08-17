"""Tests for scripts/export_dashboard_data.py -- Вотчлист Restructure task
(2026-08-16), spread_history_by_issuer() (per-issuer G-spread history for
the new Рынок tab delta table + chart)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hybond.store import db  # noqa: E402
from export_dashboard_data import spread_history_by_issuer  # noqa: E402

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
