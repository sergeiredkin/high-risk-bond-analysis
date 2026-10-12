"""Structured provenance for annual Nalog financial-report records."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .verify import verify_citation


def is_financial_report_list_question(question: str) -> bool:
    """Recognize requests for available report periods, not financial metrics."""
    value = question.casefold().replace("ё", "е")
    financial = "финансов" in value or "бухгалтер" in value
    report = any(term in value for term in ("документ", "отчет", "отчетност"))
    availability = any(term in value for term in ("последн", "доступн", "период"))
    return financial and report and availability


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def extract_nalog_reports(path: Path) -> list[dict]:
    """Extract report-level publication metadata from a Nalog JSON response."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    reports = payload.get("reports") if isinstance(payload, dict) else None
    if not isinstance(reports, list):
        return []

    extracted = []
    for index, report in enumerate(reports):
        if not isinstance(report, dict):
            continue
        period = str(report.get("period") or "").strip()
        if not re.fullmatch(r"\d{4}", period):
            continue
        source_url = report.get("url")
        if not isinstance(source_url, str) or not source_url.startswith(
            "https://bo.nalog.gov.ru/"
        ):
            source_url = None
        extracted.append({
            "report_id": str(report.get("detail_id") or f"row-{index}"),
            "period": period,
            "actual_bfo_date": report.get("actual_bfo_date"),
            "published_date": report.get("published_date"),
            "correction_number": _int_or_none(report.get("correction_number")),
            "correction_date": report.get("correction_date"),
            "form_code": str(report.get("knd") or ""),
            "source_url": source_url,
        })
    return extracted


def replace_nalog_reports(con: sqlite3.Connection, doc_sha256: str,
                          reports: list[dict]) -> None:
    """Replace report metadata for one re-indexed Nalog source document."""
    con.execute("DELETE FROM rag_financial_reports WHERE doc_sha256=?", (doc_sha256,))
    con.executemany(
        """INSERT INTO rag_financial_reports
           (doc_sha256,report_id,period,actual_bfo_date,published_date,
            correction_number,correction_date,form_code,source_url)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        [(doc_sha256, row["report_id"], row["period"], row["actual_bfo_date"],
          row["published_date"], row["correction_number"], row["correction_date"],
          row["form_code"], row["source_url"]) for row in reports],
    )


def financial_report_summary(con: sqlite3.Connection, issuer_inn: str,
                             *, limit: int = 10) -> list[dict]:
    """Return the newest known report revision per period with indexed provenance."""
    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rag_financial_reports'"
    ).fetchone()
    if not has_table:
        return []
    rows = con.execute(
        """WITH ranked AS (
             SELECT r.*, d.local_path, d.doc_sha256,
                    ROW_NUMBER() OVER (
                      PARTITION BY r.period, r.form_code
                      ORDER BY COALESCE(r.correction_number,-1) DESC,
                               COALESCE(r.correction_date,'' ) DESC,
                               d.local_path DESC
                    ) AS revision_rank
             FROM rag_financial_reports r
             JOIN rag_documents d ON d.doc_sha256=r.doc_sha256
             WHERE d.issuer_inn=? AND d.doc_type='nalog_financials'
           )
           SELECT doc_sha256,local_path,report_id,period,actual_bfo_date,
                  published_date,correction_number,correction_date,form_code,source_url
           FROM ranked WHERE revision_rank=1
           ORDER BY CAST(period AS INTEGER) DESC,form_code LIMIT ?""",
        (issuer_inn, limit),
    ).fetchall()

    result = []
    for row in rows:
        values = {
            "period": row["period"],
            "actual_bfo_date": row["actual_bfo_date"],
            "published_date": row["published_date"],
            "correction_number": row["correction_number"],
            "correction_date": row["correction_date"],
            "knd": row["form_code"],
            "url": row["source_url"],
        }
        verified_fields = []
        for field, value in values.items():
            if value is None or value == "":
                continue
            quote = f'"{field}": {json.dumps(value, ensure_ascii=False)}'
            checked = verify_citation(con, {
                "doc_sha256": row["doc_sha256"], "page": 1, "quote": quote,
            })
            if checked["valid"]:
                verified_fields.append(field)
        period_quote = f'"period": {json.dumps(row["period"], ensure_ascii=False)}'
        citation = {
            "doc_sha256": row["doc_sha256"], "page": 1,
            "quote": period_quote, "verified": "period" in verified_fields,
            "verified_fields": verified_fields,
            "source_path": row["local_path"],
            "source_title": Path(row["local_path"]).name,
        }
        result.append({
            "period": row["period"], "actual_bfo_date": row["actual_bfo_date"],
            "published_date": row["published_date"],
            "correction_number": row["correction_number"],
            "correction_date": row["correction_date"],
            "form_code": row["form_code"], "source_url": row["source_url"],
            "citation": citation,
        })
    return result
