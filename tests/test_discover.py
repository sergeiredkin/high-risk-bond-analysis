"""Tests for discover.py -- the on-demand MOEX-vs-watchlist bond gap-check.
Network calls (sources.fetch_bonds_by_inn / fetch_marketdata) are monkeypatched
with canned data, never hit MOEX live.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import discover  # noqa: E402
from hybond.ingest import sources  # noqa: E402


def _fake_bonds_by_inn(inn):
    if inn == "1111111111":
        return [
            {"SECID": "TRACKED1", "ISIN": "RU000A1", "SHORTNAME": "Tracked-1",
             "MATDATE": "2027-01-01"},
            {"SECID": "NEWBOND1", "ISIN": "RU000A2", "SHORTNAME": "New-1",
             "MATDATE": "2028-06-15"},
        ]
    if inn == "2222222222":
        return []
    raise AssertionError(f"unexpected ИНН {inn}")


def _fake_marketdata(secids, snapshot=False):
    return [{"SECID": s, "DURATION": 365} for s in secids]


def test_discover_bonds_for_issuer_computes_maturity_and_duration(monkeypatch):
    monkeypatch.setattr(sources, "fetch_bonds_by_inn", _fake_bonds_by_inn)
    monkeypatch.setattr(sources, "fetch_marketdata", _fake_marketdata)

    out = discover.discover_bonds_for_issuer("1111111111")
    assert {d["secid"] for d in out} == {"TRACKED1", "NEWBOND1"}
    newbond = next(d for d in out if d["secid"] == "NEWBOND1")
    assert newbond["mod_dur"] == 1.0  # 365 days / 365.0
    assert newbond["years_to_maturity"] is not None


def test_discover_bonds_for_issuer_returns_empty_when_moex_has_nothing(monkeypatch):
    monkeypatch.setattr(sources, "fetch_bonds_by_inn", _fake_bonds_by_inn)
    monkeypatch.setattr(sources, "fetch_marketdata", _fake_marketdata)

    assert discover.discover_bonds_for_issuer("2222222222") == []


def test_run_gap_check_flags_untracked_secid(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "fetch_bonds_by_inn", _fake_bonds_by_inn)
    monkeypatch.setattr(sources, "fetch_marketdata", _fake_marketdata)

    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({
        "_meta": {"note": "should be skipped"},
        "1111111111": {"name": "Issuer One", "secids": ["TRACKED1"]},
        "2222222222": {"name": "Issuer Two", "secids": []},
    }, ensure_ascii=False), encoding="utf-8")

    report = discover.run_gap_check(wl_path)
    assert report["issuers_checked"] == 2
    assert report["total_untracked"] == 1

    one = next(r for r in report["issuers"] if r["issuer_id"] == "1111111111")
    assert one["tracked_count"] == 1
    assert one["discovered_count"] == 2
    assert [u["secid"] for u in one["untracked"]] == ["NEWBOND1"]

    two = next(r for r in report["issuers"] if r["issuer_id"] == "2222222222")
    assert two["tracked_count"] == 0
    assert two["discovered_count"] == 0
    assert two["untracked"] == []


def test_run_gap_check_no_gaps_when_fully_tracked(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "fetch_bonds_by_inn", _fake_bonds_by_inn)
    monkeypatch.setattr(sources, "fetch_marketdata", _fake_marketdata)

    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({
        "1111111111": {"name": "Issuer One", "secids": ["TRACKED1", "NEWBOND1"]},
    }, ensure_ascii=False), encoding="utf-8")

    report = discover.run_gap_check(wl_path)
    assert report["total_untracked"] == 0
