from pathlib import Path

from hybond.rag.current_risk import current_risk_summary, is_current_risk_question
from hybond.store import agency_ratings, db


def test_current_risk_router_requires_current_language():
    assert is_current_risk_question("Какие риски у эмитента на данный момент?")
    assert not is_current_risk_question("Что такое кредитный риск?")


def test_current_risk_summary_is_dated_and_structured():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("insert into issuer(issuer_id,name,health_status) values(?,?,?)",
                ("1234567890", "Тест", "yellow"))
    con.commit()
    result = current_risk_summary(con, "1234567890")
    assert result["status"] == "found"
    assert result["health_status"] == "yellow"
    assert result["route"] == "current_risk"
    con.close()


def test_current_risk_uses_canonical_moex_rating_resolution():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    agency_ratings.init(con)
    con.execute("insert into issuer(issuer_id,name) values(?,?)", ("1234567890", "Тест"))
    con.execute(
        """INSERT INTO agency_rating(emitent_id,isin,inn,agency,book_id,raw_level,
           grade,scale,status,rated_on,published_on,first_seen,last_seen,source)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (1, "", "1234567890", "Эксперт РА", 1, "ruB", "ruB", "national",
         "live", "2026-09-30", "2026-09-30", "2026-09-30", "2026-10-12", "moex_cci"),
    )
    con.commit()

    result = current_risk_summary(con, "1234567890")
    assert result["rating_resolution"]["grade"] == "ruB"
    assert result["ratings"][0]["source"] == "moex"
    assert result["ratings"][0]["rating"] == "ruB"
    con.close()
