"""hybond run / hybond scrape CLI: removed the required positional watchlist
argument, same fix already applied to hybond financials. Both pipeline.run()
and scrape_pipeline.run_scrape() have scoped issuers off `issuer WHERE
watchlist_tier IN (1, 2)` since the Watchlist Tier System migration --
watchlist.json is read only as an optional enrichment fallback (or, for
scrape, not read at all: run_scrape()'s own docstring says watchlist_path is
"accepted for signature compatibility... but no longer read here").

cmd_run/cmd_scrape had an extra live bug on top of the required positional:
they silently hardcoded Path("watchlist.json") instead of reading
args.watchlist at all, so even passing a custom --watchlist value would have
been ignored. Fixed alongside the positional removal."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402


# ---- hybond run -------------------------------------------------------

def test_run_no_positional_arg_required():
    with patch("hybond.pipeline.run", return_value={"spread_screen": []}) as mock_run, \
         patch("hybond.pipeline_lock.held") as mock_lock:
        mock_lock.return_value.__enter__ = lambda self: None
        mock_lock.return_value.__exit__ = lambda self, *a: False
        rc = cli.main(["run"])
    assert rc == 0
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] is None  # no --watchlist given -> None, not "watchlist.json"


def test_run_bare_positional_now_rejected():
    with pytest.raises(SystemExit):
        cli.main(["run", "watchlist.json"])


def test_run_watchlist_flag_passed_through():
    with patch("hybond.pipeline.run", return_value={"spread_screen": []}) as mock_run, \
         patch("hybond.pipeline_lock.held") as mock_lock:
        mock_lock.return_value.__enter__ = lambda self: None
        mock_lock.return_value.__exit__ = lambda self, *a: False
        cli.main(["run", "--watchlist", "custom.json"])
    called_path = mock_run.call_args.args[0]
    assert called_path is not None
    assert str(called_path) == "custom.json"


# ---- hybond scrape ------------------------------------------------------

def test_scrape_no_positional_arg_required():
    with patch("hybond.scrape_pipeline.run_scrape",
               return_value={"issuers": {}, "new_total": 0, "errors": {}}) as mock_run, \
         patch("hybond.pipeline_lock.held") as mock_lock:
        mock_lock.return_value.__enter__ = lambda self: None
        mock_lock.return_value.__exit__ = lambda self, *a: False
        rc = cli.main(["scrape"])
    assert rc == 0
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] is None


def test_scrape_bare_positional_now_rejected():
    with pytest.raises(SystemExit):
        cli.main(["scrape", "watchlist.json"])


def test_scrape_watchlist_flag_passed_through():
    with patch("hybond.scrape_pipeline.run_scrape",
               return_value={"issuers": {}, "new_total": 0, "errors": {}}) as mock_run, \
         patch("hybond.pipeline_lock.held") as mock_lock:
        mock_lock.return_value.__enter__ = lambda self: None
        mock_lock.return_value.__exit__ = lambda self, *a: False
        cli.main(["scrape", "--watchlist", "custom.json", "--smartlab"])
    called_path = mock_run.call_args.args[0]
    assert called_path is not None
    assert str(called_path) == "custom.json"
    assert mock_run.call_args.kwargs["use_smartlab"] is True
