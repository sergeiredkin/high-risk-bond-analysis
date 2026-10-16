"""Serve the unified dashboard and its same-origin local RAG API."""
from __future__ import annotations

import argparse
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_INDEX = ROOT / "dashboard" / "index.html"
sys.path.insert(0, str(ROOT))

from scripts.rag_server import Handler as RAGHandler


class Handler(RAGHandler):
    """Serve the dashboard shell while retaining the local RAG routes."""

    def do_GET(self) -> None:
        """Serve the built dashboard at the application root."""
        if self.path in {"/", "/index.html"}:
            try:
                page = DASHBOARD_INDEX.read_bytes()
            except OSError:
                self._send(503, {"error": "dashboard build is unavailable"})
                return
            self._send(200, page, "text/html")
            return
        super().do_GET()


def main() -> None:
    """Run the unified app; bind only to the explicitly selected interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="local interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    print(f"HyBond app: http://{args.bind}:{args.port}/", flush=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
