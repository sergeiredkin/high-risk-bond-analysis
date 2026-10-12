from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from hybond.rag.ingest import ingest_file
from hybond.store import db
from scripts.rag_server import Handler


def _ask(base_url: str, payload: dict) -> dict:
    request = Request(
        f"{base_url}/api/ask",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _serve(monkeypatch, db_path: Path):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def test_api_routes_issuer_and_bond_profile_requests_to_sql(tmp_path, monkeypatch):
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    db.init_universe(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.execute("INSERT INTO bond(secid,isin,issuer_id,short_name,mat_date,coupon_pct) VALUES(?,?,?,?,?,?)",
                ("RU000A000001", "RU000A000001", "1234567890", "Example 1", "2028-01-01", 12.5))
    con.execute(
        """INSERT INTO universe_snapshot(secid,snap_date,emitent_inn,short_name,ytm,g_spread_bp)
           VALUES(?,?,?,?,?,?)""",
        ("RU000A000001", "2026-10-12", "1234567890", "Example 1", 0.2, 500.0),
    )
    con.commit()
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        issuer = _ask(base_url, {
            "question": "Расскажи об эмитенте и его облигациях.", "company": "Example Co",
        })
        bond = _ask(base_url, {
            "question": "Сделай профиль облигации RU000A000001.", "company": None,
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert issuer["route"] == "issuer_brief"
    assert issuer["answer"]["market"]["bond_count"] == 1
    assert bond["route"] == "bond_brief"
    assert bond["answer"]["bond"]["isin"] == "RU000A000001"
    assert bond["answer"]["market"]["ytm_pct"] == 20.0


def test_unreconciled_numeric_fact_falls_back_to_cited_document_search(tmp_path, monkeypatch):
    source = tmp_path / "payment-notice.md"
    source.write_text(
        "# Уведомление о выплате\n\n"
        "Эмитент не исполнил денежное обязательство. Размер неисполненного "
        "обязательства составляет 4 200 000 рублей.",
        encoding="utf-8",
    )
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    ingest_file(
        con, source, doc_type="edisclosure_html", issuer_inn="1234567890",
        source_url="https://example.test/payment-notice",
        source_kind="edisclosure", authority_level="issuer_disclosure",
    )
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {
            "question": "Какова сумма неисполненного обязательства Example Co?",
            "company": "Example Co",
            "source_scope": "official",
            "doc_type": "edisclosure_html",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["route"] == "retrieval"
    assert result["fact_lookup"] == {"status": "not_found", "fact_type": "unpaid_amount_rub"}
    assert result["hits"]
    hit = result["hits"][0]
    assert "4 200 000 рублей" in hit["text"]
    assert hit["citation"]["verified"] is True
    assert hit["citation"]["source_title"] == "payment-notice.md"
    assert hit["citation"]["source_url"] == "https://example.test/payment-notice"


def test_financial_citation_snippet_skips_company_header_and_shows_late_values(tmp_path, monkeypatch):
    source = tmp_path / "financials.json"
    source.write_text(
        '{"organization":{"name":"Example Co","notes":"' + "x" * 8_000
        + '"},"financial_data":{"period":"2025 год","выручка":42000000}}',
        encoding="utf-8",
    )
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    ingest_file(con, source, doc_type="nalog_financials", issuer_inn="1234567890",
                source_kind="nalog", authority_level="financial_source")
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {
            "question": "Найди выручку за 2025 год Example Co",
            "company": "Example Co",
            "doc_type": "nalog_financials",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["hits"]
    assert "42000000" in result["hits"][0]["text"]
    assert "2025 год" in result["hits"][0]["text"]
    assert result["hits"][0]["citation"]["verified"] is True


def test_financial_report_periods_are_returned_with_nalog_source_provenance(tmp_path, monkeypatch):
    source = tmp_path / "nalog.json"
    source.write_text(json.dumps({
        "success": 1,
        "reports": [
            {"period": "2025", "detail_id": 10, "actual_bfo_date": "2026-03-31",
             "published_date": "2026-04-18", "correction_number": 0,
             "correction_date": "2026-03-31", "knd": "0710099",
             "url": "https://bo.nalog.gov.ru/download/bfo/pdf/10?period=2025"},
            {"period": "2024", "detail_id": 9, "actual_bfo_date": "2025-03-31",
             "published_date": "2025-04-18", "correction_number": 1,
             "correction_date": "2025-04-18", "knd": "0710099",
             "url": "https://bo.nalog.gov.ru/download/bfo/pdf/9?period=2024"},
        ],
    }), encoding="utf-8")
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    ingest_file(con, source, doc_type="nalog_financials", issuer_inn="1234567890",
                source_kind="nalog", authority_level="financial_source")
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {
            "question": "Найди последние доступные финансовые документы Example Co и укажи период каждого.",
            "company": "Example Co",
            "doc_type": "nalog_financials",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    reports = result["financial_reports"]
    assert [report["period"] for report in reports] == ["2025", "2024"]
    assert all(report["citation"]["verified"] for report in reports)
    assert reports[0]["source_url"].endswith("period=2025")


def test_cbr_alias_and_company_free_query_retrieve_official_documents(tmp_path, monkeypatch):
    source = tmp_path / "cbr-forecast.md"
    source.write_text(
        "Банк России публикует прогноз ключевой ставки в среднем за год: "
        "14 процентов на 2026 год.",
        encoding="utf-8",
    )
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    ingest_file(
        con, source, doc_type="cbr_document", source_url="https://cbr.example/forecast",
        source_kind="cbr", authority_level="official",
    )
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        alias_result = _ask(base_url, {
            "question": "CB key rate forecast",
            "company": "CB",
            "doc_type": "all",
        })
        company_free_result = _ask(base_url, {
            "question": "Какая ключевая ставка указана в прогнозе Банка России?",
            "source_scope": "official",
            "doc_type": "cbr_document",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    for result in (alias_result, company_free_result):
        assert result["route"] == "retrieval"
        assert result["hits"]
        citation = result["hits"][0]["citation"]
        assert citation["doc_type"] == "cbr_document"
        assert citation["source_title"] == "cbr-forecast.md"
        assert citation["source_url"] == "https://cbr.example/forecast"
        assert citation["verified"] is True
        assert "14 процентов" in result["hits"][0]["text"]


def test_bond_snapshot_returns_known_metrics_and_marks_missing_coupon(tmp_path, monkeypatch):
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    db.init_universe(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.execute(
        "INSERT INTO bond(secid,isin,issuer_id,short_name,coupon_pct,mat_date) VALUES(?,?,?,?,?,?)",
        ("TEST01", "RU000A108991", "1234567890", "Test Bond", None, "2027-04-08"),
    )
    con.execute(
        """INSERT INTO universe_snapshot
           (secid,snap_date,short_name,emitent_inn,coupon_pct,coupon_period,ytm,mat_date,yield_source)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("TEST01", "2026-10-12", "Test Bond", "1234567890", None, 91, 0.3008,
         "2027-04-08", "moex"),
    )
    con.commit()
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {
            "question": "Какова ставка купона для выбранной облигации?",
            "isin": "RU000A108991",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    snapshot = result["bond_snapshot"]
    assert snapshot["status"] == "partial"
    assert snapshot["as_of"] == "2026-10-12"
    assert snapshot["values"] == {"coupon_period_days": 91}
    assert snapshot["missing_fields"] == ["ставка купона"]
    assert result["hits"] == []
    assert result["search_scope"]["indexed_documents_in_scope"] == 0


def test_payment_message_query_returns_structured_verified_notice_fields(tmp_path, monkeypatch):
    source = tmp_path / "payment.html"
    source.write_text(
        "<p>Выплаченные доходы по эмиссионным ценным бумагам эмитента</p>"
        "<p>Дата раскрытия: 02.10.2026</p>"
        "<p>2.1. Облигации серии 003P-10R, Код ISIN RU000A10E747.</p>"
        "<p>2.2. Категория выплат: проценты (купонный доход) по облигациям.</p>"
        "<p>2.3. Купонный период: с 02.09.2026 по 02.10.2026.</p>"
        "<p>2.4. Общий размер выплаченных доходов: 59 674 898 рублей.</p>"
        "<p>2.9. Дата исполнения обязанности: 02.10.2026.</p>"
        "<p>2.10. Обязательство исполнено: 100%.</p>",
        encoding="utf-8",
    )
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    db.init_filing(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    url = "https://e-disclosure.azipi.ru/messages/1001/"
    ingest_file(con, source, doc_type="edisclosure_html", issuer_inn="1234567890",
                source_url=url, source_kind="edisclosure", authority_level="issuer_disclosure")
    db.insert_filing(
        con, body_hash="official-payment", issuer_id="1234567890",
        edisclosure_id="1234567890", published="2026-10-02",
        title="Выплаченные доходы по эмиссионным ценным бумагам эмитента",
        url=url, fetched_on="2026-10-12",
    )
    con.commit()
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {
            "question": "Найди официальные сообщения Example Co о выплатах по облигациям.",
            "company": "Example Co",
            "source_scope": "official",
            "doc_type": "edisclosure_html",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["hits"]
    assert len(result["payment_notices"]) == 1
    notice = result["payment_notices"][0]
    assert notice["source_url"] == url
    fields = {field["label"]: field for field in notice["fields"]}
    assert fields["ISIN выпуска"]["value"] == "RU000A10E747"
    assert fields["Купонный период"]["value"] == "с 02.09.2026 по 02.10.2026"
    assert fields["Доля исполнения обязательства"]["verified"] is True
    assert "filing_catalog" not in result


def test_payment_message_query_returns_metadata_only_official_filing_catalog(tmp_path, monkeypatch):
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    db.init_filing(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    db.insert_filing(
        con, body_hash="official-payment", issuer_id="1234567890",
        edisclosure_id="1234567890", published="2026-10-02",
        title="Выплаченные доходы по эмиссионным ценным бумагам эмитента",
        url="https://e-disclosure.azipi.ru/messages/1001/", fetched_on="2026-10-12",
    )
    con.commit()
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {
            "question": "Найди официальные сообщения Example Co о выплатах по облигациям.",
            "company": "Example Co",
            "source_scope": "official",
            "doc_type": "edisclosure_html",
        })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["hits"] == []
    assert result["filing_catalog"][0]["published"] == "2026-10-02"
    assert result["filing_catalog"][0]["evidence_level"] == "filing_catalog_metadata_only"
    assert result["search_scope"]["indexed_documents_in_scope"] == 0


def test_unknown_company_explains_cbr_search_does_not_need_issuer(tmp_path, monkeypatch):
    db_path = tmp_path / "rag.sqlite"
    con = db.connect(db_path)
    db.init_db(con)
    con.close()

    server, thread, base_url = _serve(monkeypatch, db_path)
    try:
        result = _ask(base_url, {"question": "Найди решение ЦБ", "company": "Unknown Issuer"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["route"] == "company_resolution"
    assert result["resolution_status"] == "not_found"
    assert "оставьте поле компании пустым" in result["message"]
