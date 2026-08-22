"""hybond financials CLI: removed the required positional watchlist arg
(genuinely unused for issuer scoping since the Watchlist Tier System
migration -- financials_pipeline.run_financials() has always scoped off
`issuer WHERE watchlist_tier IN (1, 2)`, watchlist.json was read only as an
optional name/sector enrichment fallback). Added --inn for a single-issuer
targeted run."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import financials_pipeline  # noqa: E402
from hybond.ingest import nalog  # noqa: E402
from hybond.store import db  # noqa: E402

SNAP_KWARGS = {"issuers": {}, "errors": {}, "snap_date": "2026-08-22"}


def _fake_record(inn):
    return nalog.FinancialsRecord(
        inn=inn, org_name=f"Org {inn}", period="2025", actual_date="2026-04-01",
        has_consolidated=False, has_audit=True,
        balance={1600: 100000.0, 1400: 50000.0, 1500: 20000.0, 1300: 30000.0},
        financial_result={2110: 90000.0, 2200: 10000.0, 2330: 2000.0},
        funds_movement={4100: 5000.0},
        balance_prev={1600: 90000.0},
    )


def test_no_positional_watchlist_arg_required():
    """`hybond financials` with zero args must parse cleanly -- the old
    interface required a positional watchlist path."""
    with patch("hybond.financials_pipeline.run_financials",
               return_value={"issuers": {}, "errors": {}}) as mock_run:
        rc = cli.main(["financials"])
    assert rc == 0
    mock_run.assert_called_once()
    # scope arg (first positional) is always None now -- no watchlist.json dependency
    assert mock_run.call_args.args[0] is None
    assert mock_run.call_args.kwargs["inn"] is None


def test_bare_positional_watchlist_arg_now_rejected():
    """Confirms the positional was actually removed, not just made optional --
    passing one now hits argparse's own unrecognized-arguments error."""
    with pytest.raises(SystemExit):
        cli.main(["financials", "watchlist.json"])


def test_inn_flag_passed_through_to_pipeline():
    with patch("hybond.financials_pipeline.run_financials",
               return_value={"issuers": {"7708186108": {}}, "errors": {}}) as mock_run:
        rc = cli.main(["financials", "--inn", "7708186108"])
    assert rc == 0
    assert mock_run.call_args.kwargs["inn"] == "7708186108"


def test_key_flag_still_works_alongside_inn():
    with patch("hybond.financials_pipeline.run_financials",
               return_value={"issuers": {}, "errors": {}}) as mock_run:
        cli.main(["financials", "--inn", "123", "--key", "abc"])
    assert mock_run.call_args.kwargs["api_key"] == "abc"
    assert mock_run.call_args.kwargs["inn"] == "123"


def _setup_con(tmp_path, monkeypatch):
    dbpath = tmp_path / "t.sqlite"
    con = db.connect(dbpath)
    db.init_db(con)
    db.init_pillars(con)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    return con


def test_run_financials_inn_scopes_to_single_issuer(tmp_path, monkeypatch):
    """Pipeline-level check: --inn must process only that issuer, not the
    full tier=1+2 scope, even when other tier=1/2 issuers exist."""
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "7708186108", 'АО "Полипласт"', watchlist_tier=2)
    con.commit()
    con.close()

    monkeypatch.setattr(nalog, "fetch_financials", lambda inn, backend: _fake_record(inn))
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    summary = financials_pipeline.run_financials(
        None, api_key="fake-key", inn="7708186108")

    assert "error" not in summary
    assert list(summary["issuers"].keys()) == ["7708186108"]
    assert "1" not in summary["issuers"]


def test_run_financials_inn_unknown_issuer_returns_error(tmp_path, monkeypatch):
    _setup_con(tmp_path, monkeypatch)
    summary = financials_pipeline.run_financials(
        None, api_key="fake-key", inn="0000000000")
    assert "error" in summary
    assert "0000000000" in summary["error"]


def test_run_financials_no_inn_still_processes_full_tier_scope(tmp_path, monkeypatch):
    """No-args behavior must be unchanged: all tier=1+2 issuers."""
    con = _setup_con(tmp_path, monkeypatch)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2 Co", watchlist_tier=2)
    con.execute("INSERT INTO issuer(issuer_id,name,watchlist_tier) VALUES('3','Excluded Co',3)")
    con.commit()
    con.close()

    monkeypatch.setattr(nalog, "fetch_financials", lambda inn, backend: _fake_record(inn))
    monkeypatch.setattr(nalog, "ParserApiBackend", lambda api_key: object())

    summary = financials_pipeline.run_financials(None, api_key="fake-key")
    assert set(summary["issuers"].keys()) == {"1", "2"}
