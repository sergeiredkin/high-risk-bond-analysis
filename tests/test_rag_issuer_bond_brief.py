import json
from pathlib import Path

from hybond.rag.bond_brief import bond_brief_summary
from hybond.rag.issuer_brief import is_issuer_brief_question, issuer_brief_summary
from hybond.store import db


def _db():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    db.init_universe(con)
    db.init_pillars(con)
    db.upsert_issuer(con, "1234567890", "Тест Эмитент", "industry")
    con.commit()
    return con


def test_issuer_brief_reports_dated_market_financial_and_source_context():
    con = _db()
    con.execute("UPDATE issuer SET health_status='yellow',health_reason='test reason' WHERE issuer_id=?",
                ("1234567890",))
    con.execute("INSERT INTO bond(secid,isin,issuer_id,short_name,mat_date,coupon_pct) VALUES(?,?,?,?,?,?)",
                ("RU000A000001", "RU000A000001", "1234567890", "Тест 1", "2028-01-01", 12.5))
    con.execute(
        """INSERT INTO universe_snapshot(secid,snap_date,emitent_inn,short_name,mat_date,
           coupon_pct,ytm,g_spread_bp,in_watchlist,vdo_scope,yield_source)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("RU000A000001", "2026-10-12", "1234567890", "Тест 1", "2028-01-01",
         12.5, 0.2, 500.0, 1, 1, "moex"),
    )
    con.execute("INSERT INTO event(issuer_id,occurred,category,severity,summary,source_url) VALUES(?,?,?,?,?,?)",
                ("1234567890", "2026-10-10", "coupon_event", "high", "coupon event", "https://example.org/e"))
    con.execute("INSERT INTO financials_snapshot(issuer_id,period,fetched_on,has_consolidated,has_audit,payload) VALUES(?,?,?,?,?,?)",
                ("1234567890", "2025", "2026-08-01", 0, 1,
                 json.dumps({"actual_date": "2026-03-31"})))
    con.execute("INSERT INTO pillars_snapshot(issuer_id,period,computed_on,payload) VALUES(?,?,?,?)",
                ("1234567890", "2025", "2026-08-01",
                 json.dumps({"debt_to_ebit": 4.5, "interest_coverage": 1.2})))
    con.commit()

    result = issuer_brief_summary(con, "1234567890")
    assert result["status"] == "found"
    assert result["market"]["as_of"] == "2026-10-12"
    assert result["market"]["bond_count"] == 1
    assert result["market"]["bonds_ordered_by_g_spread_desc"][0]["ytm_pct"] == 20.0
    assert result["financials"]["period"] == "2025"
    assert result["financials"]["has_audit"] is True
    assert result["financials"]["ratios"]["debt_to_ebit"] == 4.5
    assert result["recent_events"][0]["summary"] == "coupon event"
    con.close()


def test_bond_brief_uses_exact_identifier_and_keeps_missing_market_values_null():
    con = _db()
    con.execute("INSERT INTO bond(secid,isin,issuer_id,short_name,mat_date,coupon_pct) VALUES(?,?,?,?,?,?)",
                ("RU000A000001", "RU000A000001", "1234567890", "Тест 1", "2028-01-01", 12.5))
    con.execute(
        """INSERT INTO universe_snapshot(secid,snap_date,emitent_inn,short_name,ytm,g_spread_bp)
           VALUES(?,?,?,?,?,?)""",
        ("RU000A000001", "2026-10-12", "1234567890", "Тест 1", 0.2, None),
    )
    con.commit()

    result = bond_brief_summary(con, "ru000a000001")
    assert result["status"] == "found"
    assert result["bond"]["isin"] == "RU000A000001"
    assert result["market"]["ytm_pct"] == 20.0
    assert result["market"]["g_spread_bp"] is None
    assert "Следующая дата купона" in result["missing_coupon_schedule"]
    con.close()


def test_brief_intents_are_explicit_and_question_scope_is_preserved():
    assert is_issuer_brief_question("Расскажи об эмитенте")
    assert not is_issuer_brief_question("Какая ставка купона у выпуска?")
