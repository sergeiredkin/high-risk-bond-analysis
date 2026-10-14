from pathlib import Path

from hybond.rag.bond_history import bond_history_summary, is_bond_trend_question
from hybond.rag.router import classify_query
from hybond.store import db


def _db():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    db.init_universe(con)
    con.execute(
        "INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Тест")
    )
    con.execute(
        "INSERT INTO bond(secid,isin,issuer_id,short_name) VALUES(?,?,?,?)",
        ("TESTBOND", "RU000A000001", "1234567890", "Тест 1"),
    )
    return con


def _snapshot(con, day, *, ytm, spread, price, source="computed"):
    con.execute(
        """INSERT INTO spread_snapshot(secid,snap_date,ytm,g_spread_bp,yield_source)
           VALUES(?,?,?,?,?)""",
        ("TESTBOND", day, ytm, spread, source),
    )
    con.execute(
        """INSERT INTO price_snapshot(secid,snap_date,last_pct)
           VALUES(?,?,?)""",
        ("TESTBOND", day, price),
    )


def test_exact_bond_trend_reports_windowed_changes_with_explicit_units():
    con = _db()
    _snapshot(con, "2026-09-01", ytm=0.15, spread=400, price=99)
    _snapshot(con, "2026-09-12", ytm=0.20, spread=500, price=98)
    _snapshot(con, "2026-10-12", ytm=0.25, spread=700, price=96)
    con.commit()

    result = bond_history_summary(
        con, "ru000a000001", "Как изменились доходность и G-спред за 30 дней по RU000A000001?"
    )

    assert result["status"] == "found"
    assert result["cutoff"] == "2026-09-12"
    assert result["as_of"] == "2026-10-12"
    assert result["observation_dates"] == 2
    assert result["metrics"]["ytm"] == {
        "start_pct": 20.0, "end_pct": 25.0, "change_pp": 5.0,
        "start_date": "2026-09-12", "end_date": "2026-10-12", "observations": 2,
    }
    assert result["metrics"]["g_spread"]["change_bp"] == 200.0
    assert result["metrics"]["clean_price"]["change_points"] == -2.0
    con.close()


def test_yield_change_is_withheld_when_calculation_source_changes():
    con = _db()
    _snapshot(con, "2026-10-01", ytm=0.20, spread=500, price=98, source="computed")
    _snapshot(con, "2026-10-12", ytm=0.24, spread=650, price=97, source="moex")
    con.commit()

    result = bond_history_summary(con, "RU000A000001", "динамика YTM за месяц RU000A000001")

    assert result["metrics"]["ytm"]["change_pp"] is None
    assert "Источник расчёта" in result["metrics"]["ytm"]["comparability_note"]
    assert result["metrics"]["g_spread"]["change_bp"] is None
    assert "Источник расчёта" in result["metrics"]["g_spread"]["comparability_note"]
    con.close()


def test_unknown_yield_source_also_withholds_spread_and_ytm_deltas():
    con = _db()
    _snapshot(con, "2026-10-01", ytm=0.20, spread=500, price=98, source="computed")
    _snapshot(con, "2026-10-12", ytm=0.24, spread=650, price=97, source=None)
    con.commit()

    result = bond_history_summary(con, "RU000A000001", "динамика G-спред за месяц RU000A000001")

    assert result["metrics"]["ytm"]["change_pp"] is None
    assert result["metrics"]["g_spread"]["change_bp"] is None
    assert "неизвестен" in result["metrics"]["g_spread"]["comparability_note"]
    con.close()


def test_trend_router_requires_explicit_exact_instrument_and_metric_intent():
    question = "Как изменился G-спред за 90 дней у RU000A000001?"
    assert is_bond_trend_question(question)
    assert classify_query(question) == "bond_trend"
    assert not is_bond_trend_question("Как изменился G-спред у эмитента?")
    assert classify_query("Сделай профиль облигации RU000A000001") == "bond_brief"
