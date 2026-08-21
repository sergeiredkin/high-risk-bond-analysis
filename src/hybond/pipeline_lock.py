"""Shared cross-pipeline DB-write coordination lock.

Generalizes what used to be auto-classify's own classify_running.lock:
scrape (hybond scrape), classify (hybond auto-classify), and the daily
Slice 1 pipeline (hybond run) can all be triggered by cron within
overlapping windows -- confirmed live (2026-08-21): hourly_filings.sh's
scrape step still running past its :15 slot collided with rebuild_
dashboard.sh's :30 catch-up classify burst, causing a real
"database is locked" failure on an unrelated write (scan-company-store).

This is a WAIT-AND-RETRY courtesy signal between cooperating pipelines, not
a hard mutex -- SQLite's own WAL mode + busy_timeout (see store/db.py's
connect()) is what actually makes concurrent writes safe; this lock just
reduces how often two heavy, long-running pipelines start writing at the
same moment. A pipeline that finds the lock still held after waiting
proceeds anyway (never skips its work) -- scrape and classify are both
too important to silently drop a scheduled run over.

A SIGKILL (or a process blocked in a C-level blocking network call, which
delays even a caught SIGTERM until the call returns -- confirmed live,
2026-08-21: a `hybond scrape` process stuck on a slow/hung network fetch
kept running well past a `timeout`-sent SIGTERM) can still leave the lock
file behind with no user-space code able to run and clean it up. This is
exactly what `fresh_seconds` is for: a lock older than that is treated as
abandoned and ignored without waiting, so an orphaned lock self-heals
within `fresh_seconds`, it does not deadlock the next caller forever.
"""
from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from pathlib import Path

PIPELINE_LOCK_PATH = Path("data/logs/pipeline_running.lock")
LOCK_FRESH_SECONDS = 300
WAIT_TIMEOUT_SECONDS = 60
WAIT_POLL_SECONDS = 10


class PipelineInterrupted(Exception):
    """Raised from the SIGTERM/SIGINT handler installed by held().

    Python's DEFAULT SIGTERM handling terminates the process immediately
    without running try/finally blocks -- unlike SIGINT, which Python's
    default handler already converts into a catchable KeyboardInterrupt.
    `timeout`, `systemctl stop`, and a plain `kill` (no -9) all send
    SIGTERM, so a bare try/finally around the lock is not enough (this
    exact bug was found and fixed for the classify-only lock this
    replaces -- see git history)."""


def _wait_for_lock(lock_path: Path, fresh_seconds: int, wait_timeout: int,
                    poll_seconds: int, sleep_fn, now_fn) -> None:
    waited = 0
    while waited < wait_timeout:
        if not lock_path.exists():
            return
        try:
            held_at = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            return  # unreadable/corrupt lock file -- treat as not locked
        if now_fn() - held_at >= fresh_seconds:
            return  # stale lock from a crashed run -- proceed without waiting
        sleep_fn(poll_seconds)
        waited += poll_seconds
    # Timeout exhausted and the lock is still fresh -- proceed regardless,
    # per the wait-and-retry (never skip) contract.


@contextmanager
def held(lock_path: Path | None = None, fresh_seconds: int = LOCK_FRESH_SECONDS,
          wait_timeout: int = WAIT_TIMEOUT_SECONDS, poll_seconds: int = WAIT_POLL_SECONDS,
          sleep_fn=time.sleep, now_fn=time.time):
    """Wait up to `wait_timeout` (in `poll_seconds` increments) for
    `lock_path` to clear or go stale, then acquire it for the duration of
    the `with` block, releasing it on normal exit, exception, or a caught
    SIGTERM/SIGINT. Yields nothing; raises PipelineInterrupted (not the
    original signal) if interrupted, so callers can catch it and exit
    cleanly instead of crashing with a traceback."""
    # Resolve at call time (like db.connect() does for DB_PATH) so
    # PIPELINE_LOCK_PATH can be monkeypatched in tests after import without
    # the default being frozen at function-definition time.
    lock_path = lock_path if lock_path is not None else PIPELINE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _wait_for_lock(lock_path, fresh_seconds, wait_timeout, poll_seconds, sleep_fn, now_fn)
    lock_path.write_text(str(int(now_fn())))

    def _raise_interrupted(signum, _frame):
        raise PipelineInterrupted(f"signal {signum}")

    old_sigterm = signal.signal(signal.SIGTERM, _raise_interrupted)
    old_sigint = signal.signal(signal.SIGINT, _raise_interrupted)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
