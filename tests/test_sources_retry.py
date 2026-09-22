"""Tests for sources._get()'s retry on transient upstream failures (502/503/504,
connection errors, timeouts). Live trigger: one HTTP 502 on a single bondization
call killed the entire 2026-09-22 10:00 daily run. The retry lives in _get so
every bulk and per-bond fetcher inherits it; sleeps are patched out here."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hybond.ingest import sources
from hybond.ingest.sources import IngestError
class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = "raw"
def test_get_retries_transient_502_then_succeeds(monkeypatch):
    calls = []
    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        return _Resp(502 if len(calls) == 1 else 200)
    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    r = sources._get("https://iss.moex.com/x")
    assert r.status_code == 200
    assert len(calls) == 2
def test_get_gives_up_after_retries_on_persistent_502(monkeypatch):
    calls = []
    def fake_get(*a, **k):
        calls.append(1)
        return _Resp(502)
    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with pytest.raises(IngestError):
        sources._get("https://iss.moex.com/x")
    assert len(calls) == 1 + len(sources._RETRY_DELAYS_S)
def test_get_does_not_retry_client_errors(monkeypatch):
    """404 is a real answer, not a transient blip -- one call, fail fast."""
    calls = []
    def fake_get(*a, **k):
        calls.append(1)
        return _Resp(404)
    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with pytest.raises(IngestError):
        sources._get("https://iss.moex.com/x")
    assert len(calls) == 1
def test_get_retries_connection_error_then_succeeds(monkeypatch):
    import requests as _rq
    calls = []
    def fake_get(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise _rq.exceptions.ConnectionError("boom")
        return _Resp(200)
    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    assert sources._get("https://iss.moex.com/x").status_code == 200
    assert len(calls) == 2
