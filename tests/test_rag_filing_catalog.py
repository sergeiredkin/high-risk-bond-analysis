from pathlib import Path

from hybond.rag.filing_catalog import (
    is_payment_filing_lookup_question,
    payment_filing_catalog,
)
from hybond.rag.ingest import ingest_file
from hybond.store import db


def _insert_filing(con, *, body_hash, title, url, published, edisclosure_id):
    db.insert_filing(
        con, body_hash=body_hash, issuer_id="1234567890",
        edisclosure_id=edisclosure_id, published=published, title=title,
        url=url, fetched_on="2026-10-12",
    )


def test_payment_filing_lookup_is_not_used_for_default_claims():
    assert is_payment_filing_lookup_question(
        "Найди официальные сообщения Сегежа Групп о выплатах по облигациям."
    )
    assert is_payment_filing_lookup_question(
        "Найди сообщения о купонных выплатах за разные даты."
    )
    assert not is_payment_filing_lookup_question(
        "Найди раскрытия о просроченных или невыплаченных обязательствах."
    )
    assert not is_payment_filing_lookup_question(
        "Какова дата следующей выплаты купона?"
    )


def test_payment_filing_catalog_hides_rows_when_body_is_already_indexed(tmp_path):
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    db.init_filing(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    _insert_filing(
        con, body_hash="official-1", title="Выплаченные доходы по эмиссионным ценным бумагам эмитента",
        url="https://e-disclosure.azipi.ru/messages/1001/", published="2026-10-02",
        edisclosure_id="1234567890",
    )
    source = tmp_path / "indexed-message.html"
    source.write_text(
        "Выплаченные доходы по эмиссионным ценным бумагам эмитента. Дата раскрытия: 02.10.2026.",
        encoding="utf-8",
    )
    ingest_file(con, source, doc_type="edisclosure_html", issuer_inn="1234567890",
                source_kind="edisclosure", authority_level="issuer_disclosure")
    con.commit()

    assert payment_filing_catalog(con, "1234567890") == []
    con.close()


def test_payment_filing_catalog_lists_only_official_metadata(tmp_path):
    con = db.connect(Path(":memory:"))
    db.init_db(con)
    db.init_filing(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    _insert_filing(
        con, body_hash="official-1", title="Выплаченные доходы по эмиссионным ценным бумагам эмитента",
        url="https://e-disclosure.azipi.ru/messages/1001/", published="2026-10-02",
        edisclosure_id="1234567890",
    )
    _insert_filing(
        con, body_hash="official-2", title="Выплаченные доходы по эмиссионным ценным бумагам эмитента",
        url="https://e-disclosure.azipi.ru/messages/1002/", published="2026-10-01",
        edisclosure_id="1234567890",
    )
    _insert_filing(
        con, body_hash="secondary", title="Сегежа: выплаты по купонам",
        url="https://smart-lab.ru/blog/123/", published="2026-10-03",
        edisclosure_id="smartlab",
    )
    con.commit()

    records = payment_filing_catalog(con, "1234567890")
    assert [row["published"] for row in records] == ["2026-10-02", "2026-10-01"]
    assert all(row["source_url"].startswith("https://e-disclosure.azipi.ru/") for row in records)
    assert all(row["evidence_level"] == "filing_catalog_metadata_only" for row in records)
    con.close()
