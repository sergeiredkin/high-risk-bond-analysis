"""Tests for scripts/startup_catch.sh (Suspend/resume cron-gap fix,
2026-08-23). Static checks only, same pattern as test_rebuild_dashboard.py
-- actually exercising it would require a real sqlite DB, a fake
network-readiness curl target, and root to install the systemd-sleep hook,
disproportionate for one script. The systemd-sleep hook itself
(/etc/systemd/system-sleep/hybond-resume.sh) needs root to install and is
NOT created by this test suite or by any hybond code -- see the task's own
handoff note for the exact commands the user runs manually."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCRIPT = ROOT / "scripts" / "startup_catch.sh"


def test_script_exists_and_executable():
    assert SCRIPT.is_file()
    import stat
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR


def test_script_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_script_uses_snap_date_staleness_check_not_just_log_filename():
    text = SCRIPT.read_text()
    assert "SELECT MAX(snap_date) FROM universe_snapshot" in text
    assert "sqlite3 data/db/hybond.sqlite" in text
    # the old log-filename-only heuristic must be gone, not just supplemented
    assert "hybond_[0-9]*.log" not in text
    assert "LAST_RUN" not in text


def test_script_still_runs_daily_run_on_stale_and_hourly_filings_when_fresh():
    text = SCRIPT.read_text()
    assert "bash scripts/daily_run.sh" in text
    assert "bash scripts/hourly_filings.sh" in text


def test_script_still_waits_for_network_before_checking_staleness():
    # Pre-existing network-readiness poll (2026-08-20 fix) must survive this
    # change -- staleness detection is pointless if the pipeline it triggers
    # can't reach MOEX yet.
    text = SCRIPT.read_text()
    assert "iss.moex.com" in text
    net_ready_pos = text.index("NETWORK_READY")
    snap_check_pos = text.index("LAST_SNAP")
    assert net_ready_pos < snap_check_pos, \
        "network-readiness wait must run BEFORE the snap_date staleness check"


def test_no_reboot_only_assumption_left_in_comments():
    # The header comment must acknowledge this now also runs from the
    # systemd-sleep resume hook, not describe itself as reboot-only --
    # a stale comment here would mislead the next person editing this file.
    text = SCRIPT.read_text()
    assert "resume" in text.lower()
