"""Tests for rebuild_dashboard.sh (project root) and the rebuild_at /
day2_snap_date display split it depends on -- classification-aware
dashboard rebuild task (2026-08-21).

Live-verified separately (not repeated here as an automated test -- would
require faking ROOT/watchlist.json/report.json/a fully seeded universe
snapshot for export_dashboard_data.py's main(), disproportionate for one new
field): a real `timeout 60 ./rebuild_dashboard.sh` run correctly skipped the
catch-up burst when last_classify.timestamp was fresh, ran export + build,
and the resulting data/dashboard_data.json carried a rebuild_at close to the
real wall-clock time, distinct from day2_snap_date (still the real DB
snapshot date, unmodified)."""
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond.store import db  # noqa: E402

REBUILD_SCRIPT = ROOT / "rebuild_dashboard.sh"


def test_script_exists_at_project_root():
    assert REBUILD_SCRIPT.is_file()


def test_script_is_executable():
    mode = REBUILD_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR


def test_script_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(REBUILD_SCRIPT)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_script_references_expected_paths_and_steps():
    text = REBUILD_SCRIPT.read_text()
    assert "data/logs/classify_running.lock" in text
    assert "data/logs/last_classify.timestamp" in text
    assert "export_dashboard_data.py" in text
    assert "build_dashboard.py" in text
    assert "auto-classify" in text


def test_crontab_entry_present():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip("no crontab available in this environment")
    lines = [l for l in result.stdout.splitlines() if "rebuild_dashboard.sh" in l]
    assert lines, "no crontab line references rebuild_dashboard.sh"
    assert any(l.strip().startswith("*/30 * * * *") for l in lines)


def test_export_script_sets_rebuild_at_from_wall_clock_not_db():
    # Regression guard: rebuild_at must come from datetime.now(), not from
    # report.get(...)/snap_date (the DB-derived fields) -- that distinction
    # is the entire point of this field (see the user's explicit design
    # decision this task was built against).
    text = (ROOT / "scripts" / "export_dashboard_data.py").read_text()
    assert '"rebuild_at": datetime.now().isoformat()' in text


def test_dashboard_never_overrides_day2_snap_date_with_rebuild_at():
    # Regression guard against re-introducing the rejected design (forcing
    # day2_snap_date to always read as "today", which would hide genuine
    # data staleness -- explicitly rejected in favor of two separate signals).
    text = (ROOT / "dashboard" / "template.html").read_text()
    assert "DATA.day2_snap_date = " not in text.replace(" ", "")
    assert "rebuildAt" in text  # the second, distinct signal is actually rendered


def _seed_filings(con, n: int, issuer_id="1"):
    db.init_filing(con)
    db.add_issuer_at_tier(con, issuer_id, "Test Issuer", watchlist_tier=1)
    for i in range(n):
        db.insert_filing(con, body_hash=f"h{i}", issuer_id=issuer_id, edisclosure_id="1",
                          published=f"2026-08-{10 + i:02d}", title=f"Раскрытие {i}",
                          url=f"http://x/{i}", fetched_on=f"2026-08-{10 + i:02d}")
    con.commit()


def test_lock_file_removed_on_real_sigterm(tmp_path, monkeypatch):
    # The actual bug found live: a bare try/finally does NOT protect against
    # SIGTERM (Python's default handler kills the process before finally
    # runs -- unlike SIGINT, which Python already converts to a catchable
    # KeyboardInterrupt). `timeout`, `systemctl stop`, and a plain `kill`
    # all send SIGTERM, so this is the realistic "interrupted mid-classify"
    # case rebuild_dashboard.sh's wait-for-lock logic depends on actually
    # clearing. Sends a real signal to this test process while
    # cmd_auto_classify is blocked in a mocked slow classify call.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(cli, "AUTO_CLASSIFY_LOCK_PATH", tmp_path / "classify_running.lock")
    monkeypatch.setattr(cli, "AUTO_CLASSIFY_TIMESTAMP_PATH", tmp_path / "last_classify.timestamp")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 5)
    con.close()

    import os
    import signal as signal_module

    def _slow_classify(*_args, **_kwargs):
        time.sleep(5)  # interrupted well before this returns
        return {"category": "routine", "severity": "noise", "confidence": 0.9, "summary": "test"}

    def _send_sigterm():
        time.sleep(0.3)
        os.kill(os.getpid(), signal_module.SIGTERM)

    args = __import__("argparse").Namespace(
        source="filing", limit=None, priority_only=False, dry_run=False, pause=None)

    with patch("hybond.classify_ollama.classify_filing", side_effect=_slow_classify):
        t = threading.Thread(target=_send_sigterm)
        t.start()
        rc = cli.cmd_auto_classify(args)
        t.join(timeout=5)

    assert rc == 143
    assert not cli.AUTO_CLASSIFY_LOCK_PATH.exists()
