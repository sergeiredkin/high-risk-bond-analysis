"""Tests for src/hybond/pipeline_lock.py -- the shared cross-pipeline
DB-write coordination lock (scrape / auto-classify / daily `hybond run`),
generalized from auto-classify's own classify_running.lock after a real
live collision: hourly_filings.sh's scrape step still running past its
:15 slot collided with rebuild_dashboard.sh's :30 catch-up classify burst
(2026-08-21), causing a genuine "database is locked" failure."""
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import pipeline_lock  # noqa: E402


def test_acquires_immediately_when_no_lock_present(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"
    sleeps = []
    with pipeline_lock.held(lock_path=lock_path, sleep_fn=sleeps.append):
        assert lock_path.exists()
    assert sleeps == []  # no wait needed
    assert not lock_path.exists()  # released on exit


def test_lock_directory_created_if_missing(tmp_path):
    lock_path = tmp_path / "nested" / "dir" / "pipeline_running.lock"
    with pipeline_lock.held(lock_path=lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_waits_in_poll_increments_while_lock_is_fresh(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"
    now = [1000.0]
    lock_path.write_text(str(int(now[0])))  # fresh lock, age=0 at start
    sleep_calls = []

    def tracking_sleep(seconds):
        sleep_calls.append(seconds)
        now[0] += seconds
        lock_path.unlink()  # simulate the other process finishing during this poll

    with pipeline_lock.held(lock_path=lock_path, wait_timeout=60, poll_seconds=10,
                             sleep_fn=tracking_sleep, now_fn=lambda: now[0]):
        pass
    # slept once (10s), then the next existence check found it cleared --
    # did not wait the full 60s timeout
    assert sleep_calls == [10]


def test_proceeds_without_waiting_when_lock_is_stale(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"
    now = [1000.0]
    lock_path.write_text(str(int(now[0] - 400)))  # written 400s ago -- stale (>300s)
    sleeps = []
    with pipeline_lock.held(lock_path=lock_path, fresh_seconds=300,
                             sleep_fn=sleeps.append, now_fn=lambda: now[0]):
        pass
    assert sleeps == []


def test_proceeds_regardless_after_wait_timeout_exhausted(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"
    now = [1000.0]
    lock_path.write_text(str(int(now[0])))  # fresh, never clears, never goes stale

    def fake_sleep(seconds):
        now[0] += seconds

    with pipeline_lock.held(lock_path=lock_path, fresh_seconds=10_000, wait_timeout=60,
                             poll_seconds=10, sleep_fn=fake_sleep, now_fn=lambda: now[0]):
        # proceeded despite the lock still being present and fresh
        pass
    # exactly 6 polls of 10s = 60s, matching wait_timeout
    assert now[0] == 1060.0


def test_corrupt_lock_file_treated_as_not_locked(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"
    lock_path.write_text("not-a-timestamp")
    sleeps = []
    with pipeline_lock.held(lock_path=lock_path, sleep_fn=sleeps.append):
        pass
    assert sleeps == []


def test_exception_inside_block_still_releases_lock(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"
    with pytest.raises(ValueError):
        with pipeline_lock.held(lock_path=lock_path):
            raise ValueError("boom")
    assert not lock_path.exists()


def test_real_sigterm_raises_pipeline_interrupted_and_releases_lock(tmp_path):
    lock_path = tmp_path / "pipeline_running.lock"

    def _send_sigterm():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    caught = False
    t = threading.Thread(target=_send_sigterm)
    t.start()
    try:
        with pipeline_lock.held(lock_path=lock_path):
            time.sleep(5)  # interrupted well before this returns
    except pipeline_lock.PipelineInterrupted:
        caught = True
    finally:
        t.join(timeout=5)

    assert caught
    assert not lock_path.exists()


def test_default_lock_path_resolved_at_call_time_not_import_time(tmp_path, monkeypatch):
    # Regression: a bound default argument (lock_path: Path = PIPELINE_LOCK_PATH)
    # freezes at function-definition time, so monkeypatching the module
    # attribute afterward would silently do nothing -- the same bug class
    # db.connect()/DB_PATH already documents and works around.
    new_path = tmp_path / "pipeline_running.lock"
    monkeypatch.setattr(pipeline_lock, "PIPELINE_LOCK_PATH", new_path)
    with pipeline_lock.held():
        assert new_path.exists()
    assert not new_path.exists()
