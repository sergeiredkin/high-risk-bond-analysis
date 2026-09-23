"""CLI-level tests for `hybond portfolio-build` -- the Phase A write-back
path for a portfolio built in the dashboard's Builder/Preview screens (which
only persist to localStorage on their own; see cmd_portfolio_build's own
docstring). Same Namespace-call + monkeypatched DB_PATH pattern as
test_promote_manual_cli.py."""
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import portfolio  # noqa: E402
from hybond.store import db  # noqa: E402

SECTORS = ["finance", "retail", "energy", "industrial", "transport",
           "construction", "telecom", "agriculture", "leasing", "metals"]

PRESET_SNAP_DATE = "2026-08-26"


def _args(preset_type="conservative", sizing_mode="equal", sector_cap=None,
          max_per_issuer=None, health_allowed=None, save=False, name=None):
    return Namespace(preset_type=preset_type, sizing_mode=sizing_mode,
                      sector_cap=sector_cap, max_per_issuer=max_per_issuer,
                      health_allowed=health_allowed, save=save, name=name)


def _seed_bond(con, idx, rating="ruBBB+", health="green", sector=None, ytm=0.20):
    """Seeds both the legacy tables and universe_snapshot -- _gather_
    candidates() (Portfolio preset from full universe task, 2026-08-29)
    reads exclusively from the latter now. Same pattern as
    test_portfolio.py's own _seed_bond()."""
    sector = sector or SECTORS[idx % len(SECTORS)]
    issuer_id = f"ISS{idx:04d}"
    secid = f"SECID{idx:04d}"
    isin = f"RU{idx:06d}"
    db.add_issuer_at_tier(con, issuer_id, f"Issuer {idx}", watchlist_tier=1, sector=sector)
    db.set_issuer_health_status(con, issuer_id, health)
    db.upsert_rating(con, issuer_id=issuer_id, agency="expert_ra", rating=rating,
                      outlook=None, action=None, rated_on="2026-01-01")
    db.add_issuer_bond(con, issuer_id=issuer_id, secid=secid, source="manual")
    db.upsert_bond(con, secid=secid, isin=isin, issuer_id=issuer_id, short_name=f"B{idx}",
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated="2026-08-01")
    db.insert_spread_snapshot(con, secid=secid, snap_date=PRESET_SNAP_DATE, ytm=ytm,
                               tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=500.0,
                               mod_dur=1.5, convexity=None)
    mod_dur = 1.5
    delta_rate = portfolio.get_rate_scenarios(con)["base"]
    aey_base = portfolio.bond_aey(ytm, mod_dur, delta_rate)
    aey_base_risk_adj = portfolio.pd_adjusted_aey(aey_base, rating, con=con)
    db.insert_universe_snapshot(
        con, secid=secid, snap_date=PRESET_SNAP_DATE, short_name=f"B{idx}",
        emitent_inn=issuer_id, emitent_title=f"Issuer {idx}", mat_date="2028-01-01",
        offer_date=None, coupon_pct=15.0, list_level=3, qualified=1,
        last_pct=100.0, yield_moex=ytm * 100, yield_to_offer=None, accrued_int=0.0,
        volume=1000.0, num_trades=10, duration_moex=mod_dur * 365.0, ytm=ytm,
        tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=500.0, yield_source="moex",
        rating=rating, implied_fair_spread_bp=None, mispricing_bp=None,
        in_watchlist=1, signal_excluded=0, mod_dur=mod_dur, aey_base=aey_base,
        aey_stress=aey_base, aey_bull=aey_base, aey_base_risk_adj=aey_base_risk_adj,
        vdo_scope=1)
    con.execute(
        "UPDATE universe_snapshot SET health_status=?, health_source='full' "
        "WHERE secid=? AND snap_date=?",
        (health, secid, PRESET_SNAP_DATE))
    con.commit()


def _seed_watchlist(con, n, **kwargs):
    for i in range(n):
        _seed_bond(con, i, **kwargs)


def test_portfolio_build_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_universe(con)
    db.init_cbr(con)
    _seed_watchlist(con, 25, rating="ruA-", health="green", ytm=0.20)
    con.close()

    rc = cli.cmd_portfolio_build(_args("conservative", save=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert '"preset_type": "conservative"' in out

    con = db.connect()
    db.init_portfolio(con)
    assert db.list_portfolios(con) == []
    con.close()


def test_portfolio_build_save_requires_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_universe(con)
    db.init_cbr(con)
    _seed_watchlist(con, 25, rating="ruA-", health="green", ytm=0.20)
    con.close()

    rc = cli.cmd_portfolio_build(_args("conservative", save=True, name=None))
    assert rc == 1
    con = db.connect()
    db.init_portfolio(con)
    assert db.list_portfolios(con) == []
    con.close()


def test_portfolio_build_save_writes_active_portfolio(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_universe(con)
    db.init_cbr(con)
    _seed_watchlist(con, 25, rating="ruA-", health="green", ytm=0.20)
    con.close()

    rc = cli.cmd_portfolio_build(_args("conservative", save=True, name="CLI Test"))
    assert rc == 0

    con = db.connect()
    rows = db.list_portfolios(con)
    assert len(rows) == 1
    assert rows[0]["name"] == "CLI Test"
    assert rows[0]["status"] == "active"
    bonds = db.list_portfolio_bonds(con, rows[0]["id"])
    assert len(bonds) >= portfolio.PRESETS["conservative"]["bond_count"][0]
    con.close()


def test_portfolio_build_fewer_than_min_bonds_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_universe(con)
    db.init_cbr(con)
    _seed_watchlist(con, 5, rating="ruA-", health="green", ytm=0.20)  # need 20
    con.close()

    rc = cli.cmd_portfolio_build(_args("conservative"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "conservative" in err


def test_portfolio_build_user_filter_overrides_applied(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.init_universe(con)
    db.init_cbr(con)
    # Only 4 sectors across 20 issuers (5 each = 25% equal-weight per
    # sector) -- breaches the 12% default cap, but not the 30% override
    # this test passes; the CLI override must be what makes this buildable.
    four_sectors = SECTORS[:4]
    for i in range(20):
        _seed_bond(con, i, rating="ruA-", health="green", ytm=0.20,
                   sector=four_sectors[i % 4])
    con.close()

    rc = cli.cmd_portfolio_build(_args("conservative", sector_cap=0.30, max_per_issuer=0.10))
    assert rc == 0
    out = capsys.readouterr().out
    assert '"sector_cap": 0.3' in out
