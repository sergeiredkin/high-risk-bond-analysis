"""Network-resilience audit for external HTTP calls (2026-08-21).

Live incident: PID 81344 (`hybond scrape`) ran 2+ hours blocked inside a
network call, immune to SIGTERM -- a caught signal only runs its handler
once the current C-level call returns, and Selenium's browser-control
channel (used by scrape.edisclosure.SeleniumBackend) is exactly that kind
of call. HttpBackend already had a `requests` timeout; SeleniumBackend had
none at all.

Two kinds of coverage here:
  1. An AST audit (not a grep) that every requests.get/post call site in
     src/hybond passes a `timeout` keyword -- catches a future call site
     that forgets it, not just today's set.
  2. Behavioral tests: a Timeout/ConnectionError from a network call must be
     caught, logged, and NOT propagate out of run_scrape/pipeline.run/
     run_financials -- the pipeline continues to the next item instead of
     crashing. Plus the 300s total-runtime guard and SeleniumBackend's new
     page_load_timeout.
"""
import ast
import logging
import sys
import time
from datetime import date
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import financials_pipeline, pipeline, scrape_pipeline  # noqa: E402
from hybond.ingest import nalog, sources  # noqa: E402
from hybond.scrape import edisclosure as ed  # noqa: E402
from hybond.store import db  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "hybond"
SNAP = date(2026, 8, 20)


# ---- 1. AST audit: every requests.get/post call passes a timeout kwarg -----

def _requests_call_sites():
    """Yields (file, lineno) for every `requests.get(...)`/`requests.post(...)`/
    `<x>._requests.get(...)`/`<x>._requests.post(...)` call in src/hybond that
    has no `timeout=` keyword argument."""
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # matches `requests.get(...)` / `requests.post(...)` and
            # `self._requests.get(...)` / `self._requests.post(...)`
            if not (isinstance(func, ast.Attribute) and func.attr in ("get", "post")):
                continue
            target = func.value
            is_requests_call = (
                (isinstance(target, ast.Name) and target.id == "requests")
                or (isinstance(target, ast.Attribute) and target.attr == "_requests")
            )
            if not is_requests_call:
                continue
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            if not has_timeout:
                yield (path, node.lineno)


def test_all_requests_calls_pass_timeout():
    missing = list(_requests_call_sites())
    assert missing == [], (
        "requests.get/post call(s) without a timeout= kwarg: "
        + ", ".join(f"{p}:{ln}" for p, ln in missing))


# ---- 2. edisclosure.py: HttpBackend/SeleniumBackend ------------------------

class _RaisingRequests:
    class exceptions:
        Timeout = requests.exceptions.Timeout
        ConnectionError = requests.exceptions.ConnectionError

    def get(self, *a, **kw):
        raise requests.exceptions.Timeout("simulated timeout")


def test_http_backend_logs_and_reraises_timeout(caplog):
    b = ed.HttpBackend()
    b._requests = _RaisingRequests()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(requests.exceptions.Timeout):
            b.get("http://example.invalid/x")
    assert any("example.invalid" in r.message for r in caplog.records)


class _FakeDriver:
    def __init__(self):
        self.page_load_timeout_calls = []
        self.page_source = "<html></html>"

    def set_page_load_timeout(self, seconds):
        self.page_load_timeout_calls.append(seconds)

    def get(self, url):
        pass


def test_selenium_backend_sets_page_load_timeout():
    driver = _FakeDriver()
    backend = ed.SeleniumBackend(wait=0, driver=driver, page_load_timeout=30)
    backend.get("http://example.invalid/x")
    assert driver.page_load_timeout_calls == [30]


def test_selenium_backend_logs_timeout_exception(caplog):
    from selenium.common.exceptions import TimeoutException

    class _TimingOutDriver(_FakeDriver):
        def get(self, url):
            raise TimeoutException("page load timed out")

    backend = ed.SeleniumBackend(wait=0, driver=_TimingOutDriver(), page_load_timeout=30)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TimeoutException):
            backend.get("http://example.invalid/x")
    assert any("example.invalid" in r.message for r in caplog.records)


# ---- 3. scrape_pipeline.py: catch-log-continue + 300s runtime guard -------

def _setup_scrape_con(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_filing(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    return con


def test_run_scrape_continues_past_timeout_on_org_search(tmp_path, monkeypatch, caplog):
    con = _setup_scrape_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Broken Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Fine Co", watchlist_tier=2)
    con.commit()
    con.close()

    monkeypatch.setattr(ed, "fetch_homepage", lambda backend: "")

    def _search(inn, backend):
        if inn == "1":
            raise requests.exceptions.Timeout("simulated timeout")
        return None

    monkeypatch.setattr(ed, "search_org_by_inn", _search)

    with caplog.at_level(logging.WARNING):
        summary = scrape_pipeline.run_scrape(None, fetch_bodies=False, snap_date=SNAP)

    # did not crash; broken issuer recorded as an error, next issuer still ran
    assert "1" in summary["errors"]
    assert "2" in summary["issuers"]
    assert any("issuer 1" in r.message for r in caplog.records)


def test_run_scrape_stops_after_max_runtime(tmp_path, monkeypatch):
    con = _setup_scrape_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Co A", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Co B", watchlist_tier=1)
    con.commit()
    con.close()

    monkeypatch.setattr(ed, "fetch_homepage", lambda backend: "")
    monkeypatch.setattr(ed, "search_org_by_inn", lambda inn, backend: None)
    monkeypatch.setattr(scrape_pipeline, "MAX_RUNTIME_S", 300)

    # first monotonic() call is start_time; every call after the loop begins
    # reports as already past the guard, so issuer "1" never gets processed.
    calls = {"n": 0}
    real_monotonic = time.monotonic

    def _fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 301.0

    monkeypatch.setattr(scrape_pipeline.time, "monotonic", _fake_monotonic)
    summary = scrape_pipeline.run_scrape(None, fetch_bodies=False, snap_date=SNAP)
    monkeypatch.setattr(scrape_pipeline.time, "monotonic", real_monotonic)

    assert summary["timed_out"] is True
    assert summary["issuers"] == {}


# ---- 4. pipeline.py: per-secid network errors don't crash the run ---------

def _bond_master_row(secid, isin):
    return {
        "SECID": secid, "ISIN": isin, "SHORTNAME": secid, "FACEVALUE": 1000.0,
        "FACEUNIT": "SUR", "COUPONPERCENT": 15.0, "LISTLEVEL": 3,
        "MATDATE": "2028-01-01", "ISSUESIZE": 1_000_000, "ISSUESIZEPLACED": 1_000_000,
    }


def _marketdata_row(secid):
    return {"SECID": secid, "LAST": 98.5, "YIELD": 18.0, "DURATION": 400,
            "VALTODAY": 50000, "NUMTRADES": 10, "SYSTIME": "2026-08-20 12:00:00"}


def test_pipeline_run_continues_past_bondization_timeout(tmp_path, monkeypatch, caplog):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "BROKEN", source="bond")
    db.add_issuer_bond(con, "1", "FINE", source="bond")
    con.commit()
    con.close()

    monkeypatch.setattr(sources, "fetch_bond_master",
                         lambda: [_bond_master_row(s, f"RU_{s}") for s in ("BROKEN", "FINE")])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [_marketdata_row(s) for s in secids or []])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_security_qualified", lambda secid: False)

    def _bondization(secid):
        if secid == "BROKEN":
            raise requests.exceptions.ConnectionError("simulated connection error")
        return {"coupons": [{"coupondate": "2030-01-01", "value": None}], "amortizations": []}

    monkeypatch.setattr(sources, "fetch_bondization", _bondization)

    with caplog.at_level(logging.WARNING):
        report = pipeline.run(None, snap_date=SNAP)

    secids_in_screen = {r["secid"] for r in report["spread_screen"]}
    assert "BROKEN" not in secids_in_screen  # skipped, did not crash the run
    assert "FINE" in secids_in_screen        # next item still processed
    assert any("BROKEN" in r.message for r in caplog.records)


def test_pipeline_run_continues_past_qualified_timeout(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID1", source="bond")
    con.commit()
    con.close()

    monkeypatch.setattr(sources, "fetch_bond_master", lambda: [_bond_master_row("SECID1", "RU1")])
    monkeypatch.setattr(sources, "fetch_marketdata",
                         lambda secids=None: [_marketdata_row(s) for s in secids or []])
    monkeypatch.setattr(sources, "fetch_cbr_zcyc", lambda: [(1.0, 0.15), (5.0, 0.15)])
    monkeypatch.setattr(sources, "fetch_bondization", lambda secid: {
        "coupons": [{"coupondate": "2030-01-01", "value": None}], "amortizations": []})

    def _qualified(secid):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(sources, "fetch_security_qualified", _qualified)

    # a qualified-flag failure must not skip the whole bond -- descriptor and
    # price data still get written (unlike a bondization failure, which does
    # skip the rest of that secid).
    report = pipeline.run(None, snap_date=SNAP)
    secids_in_screen = {r["secid"] for r in report["spread_screen"]}
    assert "SECID1" in secids_in_screen


# ---- 5. financials_pipeline.py: per-issuer network errors don't crash -----

def test_run_financials_continues_past_timeout(tmp_path, monkeypatch, caplog):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_pillars(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.add_issuer_at_tier(con, "1", "Broken Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Fine Co", watchlist_tier=1)
    con.commit()
    con.close()

    def _fetch(inn, backend):
        if inn == "1":
            raise requests.exceptions.Timeout("simulated timeout")
        return nalog.FinancialsRecord(
            inn=inn, org_name="Fine Co", period="2025", actual_date="2026-04-01",
            has_consolidated=False, has_audit=True,
            balance={1600: 100000.0, 1400: 50000.0, 1500: 20000.0, 1300: 30000.0},
            financial_result={2110: 90000.0, 2200: 10000.0, 2330: 2000.0},
            funds_movement={4100: 5000.0}, balance_prev={1600: 90000.0})

    monkeypatch.setattr(nalog, "fetch_financials", _fetch)
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    with caplog.at_level(logging.WARNING):
        summary = financials_pipeline.run_financials(None, api_key="fake-key", snap_date=SNAP)

    assert "1" in summary["errors"]
    assert "2" in summary["issuers"]
    assert any("issuer 1" in r.message or "for 1" in r.message for r in caplog.records)
