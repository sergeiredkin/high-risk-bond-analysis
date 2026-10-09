#!/usr/bin/env python3
"""Local-only HTTP interface for asking the HyBond RAG registry."""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hybond.rag.answer import answer_fact
from hybond.rag.company import resolve_company
from hybond.rag.current_risk import current_risk_summary
from hybond.rag.report import log_feedback, log_query
from hybond.rag.router import classify_query
from hybond.rag.search import search, snippet
from hybond.rag.verify import verify_citation
from hybond.store import db


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
            company = str(payload.get("company", "")).strip()
            if company:
                candidates = resolve_company(con, company)
                if len(candidates) != 1:
                    con.close()
                    self._send(200, {"route": "company_resolution",
                                     "company_matches": candidates,
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
            fact = answer_fact(con, question, isin=payload.get("isin"),
                               issuer_inn=payload.get("issuer_inn"))
            if fact is not None:
                query_id = log_query(con, question=question, route=query_route, hits=[],
                                     abstained=fact["status"] != "found")
                self._send(200, {"route": "fact", "answer": fact, "query_route": query_route,
                                  "query_id": query_id, "hits": []})
                con.close()
                return
            hits = search(con, question, limit=3, issuer_inn=payload.get("issuer_inn"),
                          isin=payload.get("isin"),
                          source_scope=payload.get("source_scope", "all"),
                          doc_type=payload.get("doc_type"))
            query_id = log_query(con, question=question, route=query_route, hits=hits,
                                 abstained=not bool(hits))
            result = []
            for hit in hits:
                quote = snippet(hit.text, question)
                citation = {"doc_sha256": hit.doc_sha256, "page": hit.page_no,
                            "source_kind": hit.source_kind,
                            "authority_level": hit.authority_level, "quote": quote}
                citation["verified"] = verify_citation(con, citation)["valid"]
                result.append({"text": quote, "section": hit.section,
                               "score": hit.score, "citation": citation})
            con.close()
            self._send(200, {"route": "retrieval", "answer": None,
                              "query_route": query_route, "query_id": query_id, "hits": result})
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
