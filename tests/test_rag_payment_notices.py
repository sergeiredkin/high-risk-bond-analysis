from pathlib import Path

from hybond.rag.ingest import ingest_file
from hybond.rag.payment_notices import payment_notice_details
from hybond.store import db


def _notice_html(category: str = "проценты (купонный доход) по облигациям") -> str:
    return f"""<html><body>
<p>Выплаченные доходы по эмиссионным ценным бумагам эмитента</p>
<p>Дата раскрытия: 02.10.2026</p>
<p>2.1. Биржевые облигации серии 003P-10R, Код ISIN RU000A10E747.</p>
<p>2.2. Категория выплат: {category}.</p>
<p>2.3. Купонный период: с 02.09.2026 по 02.10.2026.</p>
<p>2.4. Общий размер выплаченных доходов: 59 674 898 (Пятьдесят девять миллионов) рублей и 40 копеек.</p>
<p>2.5. Размер выплаты на одну бумагу: 20 рублей и 55 копеек.</p>
<p>2.6. Общее количество облигаций: 2 903 888 штук.</p>
<p>2.9. Дата исполнения обязанности по выплате: 02.10.2026.</p>
<p>2.10. Обязательство исполнено в объёме: 100%, доходы выплачены полностью.</p>
</body></html>"""


def test_payment_notice_exposes_only_page_verified_fields(tmp_path):
    source = tmp_path / "payment.html"
    source.write_text(_notice_html(), encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    document = ingest_file(
        con, source, doc_type="edisclosure_html", issuer_inn="1234567890",
        source_url="https://e-disclosure.azipi.ru/messages/1001/",
        source_kind="edisclosure", authority_level="issuer_disclosure",
    )

    details = payment_notice_details(con, document["doc_sha256"], 1)
    assert details is not None
    fields = {field["label"]: field for field in details["fields"]}
    assert fields["ISIN выпуска"]["value"] == "RU000A10E747"
    assert fields["Серия выпуска"]["value"] == "003P-10R"
    assert fields["Купонный период"]["value"] == "с 02.09.2026 по 02.10.2026"
    assert fields["Всего выплачено"]["value"].startswith("59 674 898")
    assert fields["Дата исполнения выплаты"]["value"] == "02.10.2026"
    assert fields["Доля исполнения обязательства"]["value"] == "100%"
    assert all(field["verified"] for field in details["fields"])
    con.close()


def test_payment_notice_parser_rejects_dividend_or_nonissuer_source(tmp_path):
    source = tmp_path / "dividend.html"
    source.write_text(_notice_html("дивиденды по акциям"), encoding="utf-8")
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    document = ingest_file(
        con, source, doc_type="edisclosure_html", issuer_inn="unknown",
        source_kind="edisclosure", authority_level="issuer_disclosure",
    )
    assert payment_notice_details(con, document["doc_sha256"], 1) is None
    con.close()
