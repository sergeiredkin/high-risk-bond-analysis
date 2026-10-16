from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

from hybond.store import db
from scripts import hybond_app_server


def test_unified_app_serves_dashboard_and_rag_api_on_one_origin(tmp_path, monkeypatch):
    dashboard = tmp_path / "index.html"
    dashboard.write_text("<title>HyBond test shell</title>", encoding="utf-8")
    monkeypatch.setattr(hybond_app_server, "DASHBOARD_INDEX", dashboard)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.sqlite")

    con = db.connect()
    db.init_db(con)
    con.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), hybond_app_server.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/", timeout=5) as response:
            page = response.read().decode("utf-8")
            assert response.headers.get_content_type() == "text/html"
        with urlopen(f"{base_url}/api/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))

        request = Request(
            f"{base_url}/api/ask",
            data=json.dumps({"question": "Погода в Москве?"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=5) as response:
            answer = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "HyBond test shell" in page
    assert health == {"ok": True}
    assert answer["route"] == "out_of_scope"


def test_unified_app_preserves_standalone_rag_page_route():
    server = ThreadingHTTPServer(("127.0.0.1", 0), hybond_app_server.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/rag.html", timeout=5) as response:
            page = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "HyBond · Документальный поиск" in page
