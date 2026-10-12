import json
from pathlib import Path

from hybond.rag.financial_reports import (
    financial_report_summary,
    is_financial_report_list_question,
)
from hybond.rag.ingest import ingest_file
from hybond.store import db


def _write_report(path: Path, period: str, *, detail_id: int, correction: int,
                  published: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "success": 1,
        "reports": [{
            "period": period,
            "detail_id": detail_id,
            "actual_bfo_date": f"{int(period) + 1}-03-31",
            "published_date": published,
            "correction_number": correction,
            "correction_date": published,
            "knd": "0710099",
            "url": f"https://bo.nalog.gov.ru/download/bfo/pdf/{detail_id}?period={period}",
        }],
    }, ensure_ascii=False), encoding="utf-8")


def test_financial_report_metadata_is_deduplicated_by_period_and_verified(tmp_path):
    old = tmp_path / "2026-04-01" / "old.json"
    new = tmp_path / "2026-06-01" / "new.json"
    _write_report(old, "2025", detail_id=10, correction=0, published="2026-04-01")
    _write_report(new, "2025", detail_id=11, correction=1, published="2026-06-01")

    con = db.connect(Path(":memory:"))
    db.init_db(con)
    con.execute("INSERT INTO issuer(issuer_id,name) VALUES(?,?)", ("1234567890", "Example Co"))
    con.commit()
    ingest_file(con, old, doc_type="nalog_financials", issuer_inn="1234567890",
                source_kind="nalog", authority_level="financial_source")
    ingest_file(con, new, doc_type="nalog_financials", issuer_inn="1234567890",
                source_kind="nalog", authority_level="financial_source")

    reports = financial_report_summary(con, "1234567890")
    assert len(reports) == 1
    report = reports[0]
    assert report["period"] == "2025"
    assert report["published_date"] == "2026-06-01"
    assert report["correction_number"] == 1
    assert report["source_url"].startswith("https://bo.nalog.gov.ru/")
    assert report["citation"]["verified"] is True
    assert {"period", "published_date", "correction_number", "url"} <= set(
        report["citation"]["verified_fields"]
    )
    con.close()


def test_financial_report_list_intent_is_narrow():
    assert is_financial_report_list_question(
        "Найди последние доступные финансовые документы и укажи период каждого"
    )
    assert is_financial_report_list_question(
        "Документы с финансовой отчетностью за последний доступный период"
    )
    assert not is_financial_report_list_question("Какая выручка за 2025 год?")
