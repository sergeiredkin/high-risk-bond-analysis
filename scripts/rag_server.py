#!/usr/bin/env python3
"""Local-only HTTP interface for asking the HyBond RAG registry."""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hybond.rag.answer import answer_fact
from hybond.rag.bond_brief import bond_brief_summary
from hybond.rag.bond_snapshot import answer_bond_snapshot
from hybond.rag.company import resolve_company
from hybond.rag.current_risk import current_risk_summary
from hybond.rag.filing_catalog import (
    is_payment_filing_lookup_question,
    payment_filing_catalog,
)
from hybond.rag.financial_reports import (
    financial_report_summary,
    is_financial_report_list_question,
)
from hybond.rag.issuer_brief import issuer_brief_summary
from hybond.rag.payment_notices import payment_notice_details
from hybond.rag.report import log_feedback, log_query
from hybond.rag.retrieval import retrieve
from hybond.rag.router import classify_query
from hybond.rag.search import search_scope_summary, snippet
from hybond.rag.verify import verify_citation
from hybond.store import db

_ISIN_RE = re.compile(r"\bRU[0-9A-Z]{10}\b", re.IGNORECASE)


def _is_cbr_alias(value: str) -> bool:
    """Recognize central-bank source labels entered in the company field."""
    normalized = re.sub(r"[^a-zа-яё0-9]", "", value.casefold())
    return normalized in {"cb", "cbr", "цб", "цбрф", "центробанк", "банкроссии"}


class Handler(BaseHTTPRequestHandler):
    """Serve the static RAG page and a local JSON query endpoint."""

    def _send(self, status: int, payload: object, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if content_type == "application/json" else payload.encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in {"/", "/rag.html"}:
            self._send(200, (ROOT / "dashboard/rag.html").read_text(encoding="utf-8"), "text/html")
            return
        if self.path == "/api/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/api/ask", "/api/feedback"}:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/feedback":
                con = db.connect()
                db.init_db(con)
                log_feedback(con, query_id=int(payload["query_id"]),
                             label=str(payload["label"]), note=payload.get("note"))
                con.close()
                self._send(200, {"ok": True})
                return
            con = db.connect()
            db.init_db(con)
            question = str(payload.get("question", "")).strip()
            company = str(payload.get("company") or "").strip()
            if company and _is_cbr_alias(company):
                if payload.get("doc_type") in (None, "", "all", "cbr_document"):
                    payload["doc_type"] = "cbr_document"
                    payload["source_scope"] = "official"
                    payload["issuer_inn"] = None
                    company = ""
                else:
                    con.close()
                    self._send(200, {"route": "company_resolution", "resolution_status": "source_filter_conflict",
                                     "message": "Для документов ЦБ выберите тип документа «ЦБ РФ» или сбросьте фильтр типа документа.",
                                     "company_matches": [], "hits": [], "answer": None})
                    return
            if company:
                candidates = resolve_company(con, company)
                if len(candidates) != 1:
                    con.close()
                    status = "not_found" if not candidates else "ambiguous"
                    message = (
                        f"Эмитент «{company}» не найден. Для общих вопросов о ЦБ оставьте поле компании пустым "
                        "или введите «ЦБ»/«CBR» для поиска документов Банка России."
                        if not candidates else "Найдено несколько эмитентов — уточните название или ИНН."
                    )
                    self._send(200, {"route": "company_resolution", "resolution_status": status,
                                     "message": message, "company_matches": candidates,
                                     "hits": [], "answer": None})
                    return
                payload["issuer_inn"] = candidates[0]["issuer_id"]
            if not question or len(question) > 2000:
                raise ValueError("question must contain 1–2000 characters")
            query_route = classify_query(question)
            if query_route == "out_of_scope":
                con.close()
                self._send(200, {"route": "out_of_scope", "answer": None, "hits": [],
                                 "message": "Вопрос вне области документов и данных HyBond."})
                return
            if query_route == "current_risk":
                issuer_id = payload.get("issuer_inn")
                if not issuer_id:
                    con.close()
                    self._send(200, {"route": "scope_required", "answer": None,
                                     "hits": [], "message": "Укажите название компании или ИНН."})
                    return
                answer = current_risk_summary(con, issuer_id)
                query_id = log_query(con, question=question, route=query_route, hits=[],
                                     abstained=answer.get("status") != "found")
                con.close()
                self._send(200, {"route": "current_risk", "answer": answer, "query_id": query_id, "hits": []})
                return
            if query_route == "issuer_brief":
                issuer_id = payload.get("issuer_inn")
                if not issuer_id:
                    con.close()
                    self._send(200, {"route": "scope_required", "answer": None,
                                     "hits": [], "message": "Укажите эмитента в поле компании или ИНН."})
                    return
                answer = issuer_brief_summary(con, issuer_id)
                query_id = log_query(con, question=question, route=query_route, hits=[],
                                     abstained=answer.get("status") != "found")
                con.close()
                self._send(200, {"route": "issuer_brief", "answer": answer,
                                 "query_id": query_id, "hits": []})
                return
            if query_route == "bond_brief":
                instrument = payload.get("isin") or next(iter(_ISIN_RE.findall(question)), None)
                if not instrument:
                    con.close()
                    self._send(200, {"route": "scope_required", "answer": None,
                                     "hits": [], "message": "Укажите точный ISIN или SECID облигации."})
                    return
                answer = bond_brief_summary(con, instrument)
                query_id = log_query(con, question=question, route=query_route, hits=[],
                                     abstained=answer.get("status") != "found")
                con.close()
                self._send(200, {"route": "bond_brief", "answer": answer,
                                 "query_id": query_id, "hits": []})
                return
            bond_snapshot = answer_bond_snapshot(con, question, isin=payload.get("isin"))
            financial_reports = []
            if is_financial_report_list_question(question) and payload.get("issuer_inn"):
                financial_reports = financial_report_summary(con, payload["issuer_inn"])
            filing_catalog = []
            if is_payment_filing_lookup_question(question) and payload.get("issuer_inn"):
                filing_catalog = payment_filing_catalog(con, payload["issuer_inn"])
            fact = answer_fact(con, question, isin=payload.get("isin"),
                               issuer_inn=payload.get("issuer_inn"))
            if fact is not None and fact.get("status") == "found":
                query_id = log_query(con, question=question, route=query_route, hits=[],
                                     abstained=fact["status"] != "found")
                self._send(200, {"route": "fact", "answer": fact, "query_route": query_route,
                                  "query_id": query_id, "hits": []})
                con.close()
                return
            source_scope = payload.get("source_scope", "all")
            doc_type = payload.get("doc_type")
            hits = retrieve(con, question, limit=3, issuer_inn=payload.get("issuer_inn"),
                          isin=payload.get("isin"), source_scope=source_scope,
                          doc_type=doc_type)
            scope_summary = search_scope_summary(
                con, issuer_inn=payload.get("issuer_inn"), isin=payload.get("isin"),
                source_scope=source_scope, doc_type=doc_type,
            )
            query_id = log_query(con, question=question, route=query_route, hits=hits,
                                 abstained=(not bool(hits) and bond_snapshot is None
                                            and not financial_reports))
            payment_notices = []
            if is_payment_filing_lookup_question(question):
                for hit in hits:
                    details = payment_notice_details(con, hit.doc_sha256, hit.page_no)
                    if details:
                        payment_notices.append(details)
            result = []
            snippet_exclusions = []
            if payload.get("issuer_inn"):
                issuer = con.execute(
                    "SELECT name FROM issuer WHERE issuer_id=?", (payload["issuer_inn"],)
                ).fetchone()
                if issuer:
                    snippet_exclusions.append(issuer[0])
            if payload.get("isin"):
                snippet_exclusions.append(payload["isin"])
            if payload.get("doc_type") == "cbr_document":
                snippet_exclusions.append("Банк России")
            for hit in hits:
                quote = snippet(hit.text, question, exclude_terms=tuple(snippet_exclusions))
                source = con.execute(
                    "SELECT local_path,source_url,doc_type FROM rag_documents WHERE doc_sha256=?",
                    (hit.doc_sha256,),
                ).fetchone()
                local_path = source[0] if source else None
                source_url = source[1] if source else None
                citation = {"doc_sha256": hit.doc_sha256, "page": hit.page_no,
                            "source_kind": hit.source_kind,
                            "authority_level": hit.authority_level, "quote": quote,
                            "source_title": Path(local_path).name if local_path else "",
                            "source_path": local_path or "",
                            "doc_type": source[2] if source else "unknown",
                            "source_url": source_url if source_url and source_url.startswith(("https://", "http://")) else None}
                citation["verified"] = verify_citation(con, citation)["valid"]
                result.append({"text": quote, "section": hit.section,
                               "score": hit.score, "citation": citation})
            con.close()
            response = {"route": "retrieval", "answer": None,
                        "query_route": query_route, "query_id": query_id, "hits": result,
                        "search_scope": scope_summary}
            if fact is not None:
                response["fact_lookup"] = fact
            if bond_snapshot is not None:
                response["bond_snapshot"] = bond_snapshot
            if financial_reports:
                response["financial_reports"] = financial_reports
            if filing_catalog:
                response["filing_catalog"] = filing_catalog
            if payment_notices:
                response["payment_notices"] = payment_notices
            self._send(200, response)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def log_message(self, _format: str, *_args) -> None:
        """Keep routine requests out of stdout; the service is local."""


def main() -> None:
    """Start the local-only RAG service."""
    host = "127.0.0.1"
    port = 8779
    print(f"HyBond RAG: http://{host}:{port}/rag.html", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
