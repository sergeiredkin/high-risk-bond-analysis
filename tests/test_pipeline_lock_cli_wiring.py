"""Confirms cmd_scrape and cmd_run acquire the shared pipeline_lock around
their real work, and translate a PipelineInterrupted into a clean exit
code 143 instead of a crash -- the other two pipelines (besides
auto-classify) that needed the shared-lock fix for the live-confirmed
scrape/classify collision (2026-08-21)."""
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import pipeline_lock  # noqa: E402


def test_cmd_scrape_acquires_lock_around_run_scrape():
    calls = []

    class _RecordingHeld:
        def __enter__(self):
            calls.append("lock_acquired")
            return None

        def __exit__(self, *exc):
            calls.append("lock_released")
            return False

    args = Namespace(watchlist="watchlist.json", selenium=False, no_bodies=False,
                      debug=False, smartlab=False)
    fake_summary = {"issuers": [], "errors": [], "new_total": 0}
    with patch("hybond.pipeline_lock.held", return_value=_RecordingHeld()), \
         patch("hybond.scrape_pipeline.run_scrape", return_value=fake_summary) as mock_run:
        def _record_run(*a, **kw):
            calls.append("run_scrape")
            return fake_summary
        mock_run.side_effect = _record_run
        rc = cli.cmd_scrape(args)
    assert rc == 0
    assert calls == ["lock_acquired", "run_scrape", "lock_released"]


def test_cmd_scrape_returns_143_on_interrupted():
    args = Namespace(watchlist="watchlist.json", selenium=False, no_bodies=False,
                      debug=False, smartlab=False)

    class _RaisingHeld:
        def __enter__(self):
            raise pipeline_lock.PipelineInterrupted("signal 15")

        def __exit__(self, *exc):
            return False

    with patch("hybond.pipeline_lock.held", return_value=_RaisingHeld()):
        rc = cli.cmd_scrape(args)
    assert rc == 143


def test_cmd_run_acquires_lock_around_pipeline_run():
    calls = []

    class _RecordingHeld:
        def __enter__(self):
            calls.append("lock_acquired")
            return None

        def __exit__(self, *exc):
            calls.append("lock_released")
            return False

    args = Namespace(watchlist="watchlist.json", out=None)
    fake_report = {"spread_screen": []}
    with patch("hybond.pipeline_lock.held", return_value=_RecordingHeld()), \
         patch("hybond.pipeline.run") as mock_run:
        def _record_run(*a, **kw):
            calls.append("pipeline_run")
            return fake_report
        mock_run.side_effect = _record_run
        rc = cli.cmd_run(args)
    assert rc == 0
    assert calls == ["lock_acquired", "pipeline_run", "lock_released"]


def test_cmd_run_returns_143_on_interrupted():
    args = Namespace(watchlist="watchlist.json", out=None)

    class _RaisingHeld:
        def __enter__(self):
            raise pipeline_lock.PipelineInterrupted("signal 15")

        def __exit__(self, *exc):
            return False

    with patch("hybond.pipeline_lock.held", return_value=_RaisingHeld()):
        rc = cli.cmd_run(args)
    assert rc == 143
