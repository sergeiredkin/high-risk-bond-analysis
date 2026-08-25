"""Tests for watchlist_health.py + `hybond scan-watchlist-health`
(Watchlist Health Monitoring task, 2026-08-24; thresholds revised 2026-08-24
to ВДО-appropriate levels with a green/yellow/orange/red color scale
replacing the earlier needs_review boolean)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import watchlist_health  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "111"
TODAY = "2026-08-24"


def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_pillars(con)
    db.init_market_note(con)
    db.init_issuer_bond(con)
    db.init_rating(con)
    db.add_issuer_at_tier(con, ISSUER, "Test Co", watchlist_tier=1)
    con.commit()
    return con


def _bond(con, secid="B1", volume=1000.0):
    db.upsert_bond(con, secid=secid, isin="RU1", issuer_id=ISSUER, short_name=secid,
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated=TODAY)
    db.insert_price_snapshot(con, secid=secid, snap_date=TODAY, last_pct=100,
                              yield_moex=15.0, duration=400, volume=volume,
                              num_trades=10, systime="t")
    con.execute(
        "INSERT INTO issuer_bond(issuer_id, secid, added_at, source, delisted) "
        "VALUES(?,?,?,?,0)", (ISSUER, secid, TODAY, "manual"))
    return secid


def _universe_row(con, secid, snap_date, ytm, g_spread=300.0, rating="ruBB"):
    db.insert_universe_snapshot(
        con, secid=secid, snap_date=snap_date, short_name=secid, emitent_inn=ISSUER,
        emitent_title="Test Co", mat_date="2028-01-01", offer_date=None,
        coupon_pct=15.0, list_level=3, qualified=1, last_pct=100.0, yield_moex=ytm * 100,
        yield_to_offer=None, accrued_int=0.0, volume=1000.0, num_trades=10,
        duration_moex=365.0, ytm=ytm, tenor_yrs=1.5, rf_rate=0.14, g_spread_bp=g_spread,
        yield_source="moex", rating=rating, implied_fair_spread_bp=None,
        mispricing_bp=None, in_watchlist=1, signal_excluded=0, mod_dur=1.0,
        aey_base=ytm, aey_stress=ytm, aey_bull=ytm, aey_base_risk_adj=ytm)


def _event(con, occurred, category, severity, summary="test event"):
    con.execute(
        "INSERT INTO event(issuer_id, occurred, category, severity, summary) "
        "VALUES (?,?,?,?,?)", (ISSUER, occurred, category, severity, summary))


# ---- auto-demotion (hard, confirmed facts only) -----------------------------

def test_hard_demote_on_critical_insolvency_event(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    _event(con, TODAY, "insolvency", "critical", "Дефолт")
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"]
    assert any("insolvency" in h for h in r["hard_reasons"])
    assert r["red_reasons"] == r["orange_reasons"] == r["yellow_reasons"] == []


def test_hard_demote_on_ytm_over_300(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=3.50)  # 350% -- effective default pricing
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert any("YTM" in h and "300" in h and "порог" in h for h in r["hard_reasons"])


def test_hard_demote_on_delisting(tmp_path):
    con = _con(tmp_path)
    secid = "B1"
    db.upsert_bond(con, secid=secid, isin="RU1", issuer_id=ISSUER, short_name=secid,
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated=TODAY)
    con.execute(
        "INSERT INTO issuer_bond(issuer_id, secid, added_at, source, delisted) "
        "VALUES(?,?,?,?,1)", (ISSUER, secid, TODAY, "manual"))  # already delisted
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert any("делистинг" in h for h in r["hard_reasons"])


def test_no_bonds_at_all_is_not_treated_as_delisted(tmp_path):
    con = _con(tmp_path)
    con.commit()
    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []


def test_ytm_over_150_does_not_auto_demote(tmp_path):
    """RED-level distress (YTM 150-300%) must NOT trigger the tier change --
    only a confirmed default/delisting/effective-default (>300%) pricing
    does."""
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=1.60)  # 160% -- RED, not hard
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "red"


# ---- RED ---------------------------------------------------------------------

def test_red_on_ytm_over_150(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=1.60)  # 160%
    con.commit()
    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "red"
    assert any("YTM" in s for s in r["red_reasons"])


def test_red_on_gspread_move_7d(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    for d, sp in [("2026-08-18", 800.0), (TODAY, 2500.0)]:  # +212% in 6 days, abs 2500bp
        db.insert_spread_snapshot(con, secid=secid, snap_date=d, ytm=0.15, tenor_yrs=1.5,
                                  rf_rate=0.14, g_spread_bp=sp, mod_dur=1.0, convexity=None)
    _universe_row(con, secid, TODAY, ytm=0.15, g_spread=2500.0)
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "red"
    assert any("G-спред" in s and "7д" in s for s in r["red_reasons"])


def test_red_on_critical_event_3d(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    _event(con, TODAY, "governance", "critical", "Критическое раскрытие")  # not insolvency/default
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []  # only insolvency/default categories are hard
    assert r["health_status"] == "red"
    assert any("критической важности" in s for s in r["red_reasons"])


def test_red_on_fns_account_block_keyword(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    _event(con, TODAY, "governance", "high", "ФНС приостановил операции по расчётному счёту")
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "red"
    assert any("ФНС" in s for s in r["red_reasons"])


# ---- ORANGE --------------------------------------------------------------------

def test_orange_on_ytm_60_150(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=1.00)  # 100%
    con.commit()
    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "orange"
    assert any("YTM" in s for s in r["orange_reasons"])


def test_orange_on_gspread_move_30d(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    for d, sp in [("2026-07-25", 800.0), (TODAY, 1300.0)]:  # +62.5% over 30d
        db.insert_spread_snapshot(con, secid=secid, snap_date=d, ytm=0.15, tenor_yrs=1.5,
                                  rf_rate=0.14, g_spread_bp=sp, mod_dur=1.0, convexity=None)
    _universe_row(con, secid, TODAY, ytm=0.15, g_spread=1300.0)
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "orange"
    assert any("G-спред" in s and "30д" in s for s in r["orange_reasons"])


def test_orange_on_rating_drop_2_notches(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    db.upsert_rating(con, issuer_id=ISSUER, agency="АКРА", rating="ruBBB",
                     outlook="stable", action="confirmed", rated_on="2026-05-01",
                     source="manual")
    db.upsert_rating(con, issuer_id=ISSUER, agency="АКРА", rating="ruB",  # BBB -> B, 6 notches
                     outlook="stable", action="downgraded", rated_on="2026-08-20",
                     source="manual")
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "orange"
    assert any("рейтинг снижен" in s for s in r["orange_reasons"])


def test_orange_on_high_severity_event_14d(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    _event(con, TODAY, "operations", "high", "Потеря крупного клиента")
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "orange"
    assert any("высокой важности" in s for s in r["orange_reasons"])


# ---- YELLOW ----------------------------------------------------------------------

def test_yellow_on_ytm_30_60(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.40)  # 40%
    con.commit()
    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "yellow"
    assert any("YTM" in s for s in r["yellow_reasons"])


def test_yellow_on_gspread_move_30d(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    for d, sp in [("2026-07-25", 800.0), (TODAY, 1050.0)]:  # +31.25% over 30d
        db.insert_spread_snapshot(con, secid=secid, snap_date=d, ytm=0.15, tenor_yrs=1.5,
                                  rf_rate=0.14, g_spread_bp=sp, mod_dur=1.0, convexity=None)
    _universe_row(con, secid, TODAY, ytm=0.15, g_spread=1050.0)
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "yellow"
    assert any("G-спред" in s and "30д" in s for s in r["yellow_reasons"])


def test_yellow_on_rating_outlook_negative(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    db.upsert_rating(con, issuer_id=ISSUER, agency="АКРА", rating="ruBB",
                     outlook="stable", action="confirmed", rated_on="2026-05-01",
                     source="manual")
    db.upsert_rating(con, issuer_id=ISSUER, agency="АКРА", rating="ruBB",  # same rating, outlook only
                     outlook="negative", action="outlook changed", rated_on="2026-08-20",
                     source="manual")
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "yellow"
    assert any("прогноз" in s and "негативный" in s for s in r["yellow_reasons"])


def test_yellow_on_medium_severity_event_7d(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    _event(con, TODAY, "reporting_delay", "medium", "Задержка отчётности")
    con.commit()

    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "yellow"
    assert any("средней важности" in s for s in r["yellow_reasons"])


# ---- GREEN -----------------------------------------------------------------------

def test_green_when_no_signals(tmp_path):
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    con.commit()
    r = watchlist_health.scan_issuer(con, ISSUER, today=TODAY)
    con.close()
    assert r["hard_reasons"] == []
    assert r["health_status"] == "green"
    assert r["health_reason"] is None


# ---- CLI: actions applied + health_status clears -----------------------------

def test_cli_demotes_and_writes_market_note_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=3.50)  # hard: YTM > 300%
    con.commit()
    con.close()

    rc = cli.cmd_scan_watchlist_health(None)
    assert rc == 0

    con = db.connect()
    row = con.execute("SELECT watchlist_tier, health_status FROM issuer WHERE issuer_id=?",
                      (ISSUER,)).fetchone()
    log = con.execute("SELECT reason FROM watchlist_tier_log WHERE issuer_id=?",
                      (ISSUER,)).fetchone()
    note = con.execute(
        "SELECT severity, source FROM market_note WHERE issuer_id=? AND source='watchlist_health_scan'",
        (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 2
    assert row["health_status"] == "red"
    assert "YTM" in log["reason"]
    assert note is not None
    assert note["severity"] == "critical"


def test_cli_orange_signal_does_not_demote(tmp_path, monkeypatch):
    """Confirms auto-demote fires only on confirmed defaults/delisting/
    YTM>300% -- an ORANGE-level signal must leave tier unchanged."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=1.00)  # 100% -- ORANGE only
    con.commit()
    con.close()

    rc = cli.cmd_scan_watchlist_health(None)
    assert rc == 0
    con = db.connect()
    row = con.execute(
        "SELECT watchlist_tier, health_status, health_reason FROM issuer WHERE issuer_id=?",
        (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 1
    assert row["health_status"] == "orange"
    assert "YTM" in row["health_reason"]


def test_cli_sets_green_and_keeps_tier1_when_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    con.commit()
    con.close()

    rc = cli.cmd_scan_watchlist_health(None)
    assert rc == 0
    con = db.connect()
    row = con.execute("SELECT watchlist_tier, health_status, health_reason FROM issuer WHERE issuer_id=?",
                      (ISSUER,)).fetchone()
    con.close()
    assert row["watchlist_tier"] == 1
    assert row["health_status"] == "green"
    assert row["health_reason"] is None


class _FakeDate(date):
    _frozen = TODAY

    @classmethod
    def today(cls):
        return date.fromisoformat(cls._frozen)


def test_health_status_clears_when_signals_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(cli, "date", _FakeDate)
    con = _con(tmp_path)
    secid = _bond(con)
    _universe_row(con, secid, TODAY, ytm=0.15)
    _event(con, TODAY, "governance", "critical", "Критическое раскрытие")  # RED, 3d lookback
    con.commit()
    con.close()
    cli.cmd_scan_watchlist_health(None)

    con = db.connect()
    row = con.execute("SELECT health_status FROM issuer WHERE issuer_id=?", (ISSUER,)).fetchone()
    assert row["health_status"] == "red"
    con.close()

    # Signal resolves 5 days later: the event is now outside the 3-day
    # RED lookback and nothing else is wrong -- advance the fake "today" so
    # the window actually moves, same as a real day passing would.
    _FakeDate._frozen = "2026-08-29"
    con = db.connect()
    _universe_row(con, secid, "2026-08-29", ytm=0.15)
    con.commit()
    con.close()
    cli.cmd_scan_watchlist_health(None)

    con = db.connect()
    row = con.execute(
        "SELECT health_status, health_reason FROM issuer WHERE issuer_id=?",
        (ISSUER,)).fetchone()
    con.close()
    assert row["health_status"] == "green"
    assert row["health_reason"] is None


# ---- daily_run.sh wiring -----------------------------------------------------

def test_daily_run_sh_contains_scan_watchlist_health():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "daily_run.sh").read_text()
    assert "hybond scan-watchlist-health" in text
    # must run AFTER `hybond universe` (that's what actually populates
    # universe_snapshot -- see the ordering comment in daily_run.sh itself)
    assert text.index("hybond universe") < text.index("hybond scan-watchlist-health")
