from pathlib import Path

from hybond.rag.current_risk import current_risk_summary, is_current_risk_question
from hybond.store import db


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
