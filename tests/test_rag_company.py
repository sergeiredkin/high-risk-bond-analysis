from pathlib import Path

from hybond.rag.company import resolve_company
from hybond.store import db


def test_company_name_resolution_exact_and_partial():
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("insert into issuer(issuer_id,name,watchlist_tier) values(?,?,?)",
                ("1234567890", "Тест Лизинг", 1))
    con.commit()
    assert resolve_company(con, "Тест Лизинг")[0]["issuer_id"] == "1234567890"
    assert resolve_company(con, "лизинг")[0]["name"] == "Тест Лизинг"
    con.execute("insert into issuer(issuer_id,name,watchlist_tier) values(?,?,?)",
                ("9731004688", "ГК Самолёт", 2))
    con.commit()
    assert resolve_company(con, "самолет")[0]["issuer_id"] == "9731004688"
    assert resolve_company(con, "неизвестная") == []
    con.close()
