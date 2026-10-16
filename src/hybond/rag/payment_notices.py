"""Conservative extraction from indexed official paid-income notices."""
from __future__ import annotations

import re
import sqlite3

from .verify import verify_citation

_SECTION = re.compile(r"(?<![\d.])2\.(\d{1,2})\.\s*")
_ISIN = re.compile(r"\bКод\s+ISIN\s+(RU[0-9A-Z]{10})\b", re.IGNORECASE)
_SERIES = re.compile(r"\bсерии\s+([0-9A-ZА-ЯЁ-]+)", re.IGNORECASE)
_DATE_RANGE = re.compile(r"с\s+\d{2}\.\d{2}\.\d{4}\s+по\s+\d{2}\.\d{2}\.\d{4}")
_DATE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")


def _sections(text: str) -> dict[str, str]:
    matches = list(_SECTION.finditer(text))
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end():end].strip()
    return sections


def payment_notice_details(con: sqlite3.Connection, doc_sha256: str,
                          page_no: int) -> dict | None:
    """Extract only explicitly labeled fields from an official indexed notice.

    Each exposed field is independently quote-checked against the indexed page.
    Numeric strings stay verbatim; this is not a payment reconciliation engine.
    """
    document = con.execute(
        """SELECT source_url,local_path,doc_type,source_kind,authority_level
           FROM rag_documents WHERE doc_sha256=?""",
        (doc_sha256,),
    ).fetchone()
    if (document is None or document["doc_type"] != "edisclosure_html"
            or document["source_kind"] != "edisclosure"
            or document["authority_level"] != "issuer_disclosure"):
        return None
    rows = con.execute(
        "SELECT chunk_id,text FROM rag_chunks WHERE doc_sha256=? AND page_no=?",
        (doc_sha256, page_no),
    ).fetchall()
    if not rows:
        return None
    page_text = " ".join(
        row["text"] for row in sorted(rows, key=lambda row: int(row["chunk_id"].rsplit(":", 1)[-1]))
    )
    text = " ".join(page_text.split())
    if "выплаченные доходы" not in text.casefold():
        return None
    sections = _sections(text)
    category = sections.get("2", "").casefold()
    if "купонный доход" not in category or "облигац" not in category:
        return None

    fields = []

    def add_field(key: str, label: str, value: str | None) -> None:
        quote = sections.get(key)
        if not quote or not value:
            return
        check = verify_citation(con, {
            "doc_sha256": doc_sha256, "page": page_no, "quote": quote,
        })
        if check["valid"]:
            fields.append({"key": key, "label": label, "value": value,
                           "quote": quote, "verified": True})

    identity = sections.get("1", "")
    isin_match = _ISIN.search(identity)
    series_match = _SERIES.search(identity)
    if isin_match:
        add_field("1", "ISIN выпуска", isin_match.group(1).upper())
    if series_match:
        # The quote is section 2.1, and series is only returned if that full
        # source section passes the page-level verification check.
        if not any(item["key"] == "1" for item in fields):
            add_field("1", "Серия выпуска", series_match.group(1))
        else:
            fields.append({"key": "1", "label": "Серия выпуска",
                           "value": series_match.group(1), "quote": sections["1"],
                           "verified": True})

    disclosure_date = re.search(r"Дата раскрытия:\s*(\d{2}\.\d{2}\.\d{4})", text)
    if disclosure_date:
        quote = disclosure_date.group(0)
        if verify_citation(con, {"doc_sha256": doc_sha256, "page": page_no,
                                 "quote": quote})["valid"]:
            fields.append({"key": "published", "label": "Дата раскрытия",
                           "value": disclosure_date.group(1), "quote": quote,
                           "verified": True})

    period = _DATE_RANGE.search(sections.get("3", ""))
    add_field("3", "Купонный период", period.group(0) if period else None)
    for key, label in (("4", "Всего выплачено"), ("5", "На одну облигацию"),
                       ("6", "Количество облигаций")):
        section = sections.get(key, "")
        _, separator, value = section.partition(":")
        add_field(key, label, value.strip() if separator else None)
    due_date = _DATE.search(sections.get("9", ""))
    add_field("9", "Дата исполнения выплаты", due_date.group(0) if due_date else None)
    fulfillment = re.search(r"(?<!\d)100(?:[,.]0+)?\s*%", sections.get("10", ""))
    add_field("10", "Доля исполнения обязательства", fulfillment.group(0) if fulfillment else None)
    if not fields:
        return None
    return {
        "source_title": document["local_path"].rsplit("/", 1)[-1],
        "source_url": document["source_url"]
        if (document["source_url"] or "").startswith(
            "https://e-disclosure.azipi.ru/messages/"
        ) else None,
        "doc_sha256": doc_sha256,
        "page": page_no,
        "fields": fields,
        "disclaimer": "Извлечено из раскрытия эмитента; цитаты проверены по индексу. Это не сверка поступления денег держателям.",
    }
