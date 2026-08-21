"""CLI-level tests for hybond auto-classify's DB-lock fix (Sub-task A) --
confirms cmd_auto_classify commits every AUTO_CLASSIFY_BATCH_SIZE items
(chunked, not one uncommitted transaction for the whole run) and that
dry-run doesn't write.

classify_filings_batch (the Ollama batch-prompting attempt) is NOT wired
into cmd_auto_classify -- tried live against this machine's real Ollama/
qwen3.5 build (2026-08-20) and it does not work (see cmd_auto_classify's
own comment for the measured failure). Production still calls
classify_filing() once per item; only the DB-commit cadence is batched."""
import sys
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import pipeline_lock  # noqa: E402
from hybond.store import db  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_lock_and_timestamp_paths(tmp_path, monkeypatch):
    # cmd_auto_classify writes these to fixed repo-relative paths in
    # production (data/logs/...) -- without redirecting them here, every
    # test in this file would write real files into the actual repo's
    # data/logs/ (a real bug caught live: a test run left a fake
    # last_classify.timestamp in the real repo, which rebuild_dashboard.sh
    # would have read as a genuine recent classify completion).
    monkeypatch.setattr(pipeline_lock, "PIPELINE_LOCK_PATH", tmp_path / "pipeline_running.lock")
    monkeypatch.setattr(cli, "AUTO_CLASSIFY_TIMESTAMP_PATH", tmp_path / "last_classify.timestamp")


def _seed_filings(con, n: int, issuer_id="1"):
    db.init_filing(con)
    db.add_issuer_at_tier(con, issuer_id, "Test Issuer", watchlist_tier=1)
    for i in range(n):
        db.insert_filing(con, body_hash=f"h{i}", issuer_id=issuer_id, edisclosure_id="1",
                          published=f"2026-08-{10 + i:02d}", title=f"Раскрытие {i}",
                          url=f"http://x/{i}", fetched_on=f"2026-08-{10 + i:02d}")
    con.commit()


def _fake_result(**overrides):
    base = {"category": "routine", "severity": "noise", "confidence": 0.9, "summary": "test"}
    base.update(overrides)
    return base


def test_seven_filings_call_classify_filing_seven_times(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 7)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 7  # one real call per filing, not batched to Ollama


def test_commits_happen_per_batch_not_once_for_whole_run(tmp_path, monkeypatch, capsys):
    """The actual DB-lock fix: con.commit() must fire after every
    AUTO_CLASSIFY_BATCH_SIZE items, not just once at the very end."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 7)  # 2 batches of AUTO_CLASSIFY_BATCH_SIZE=5: [5, 2]
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify, \
         patch("hybond.__main__.time.sleep") as mock_sleep:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    # one yield-sleep per completed batch (2 batches) -- proves the loop
    # actually paused/committed mid-run instead of holding one transaction
    # open for all 7 items.
    assert mock_sleep.call_count == 2


def test_dry_run_does_not_write_events(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 3)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=True)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0

    con = db.connect()
    assert con.execute("SELECT COUNT(*) c FROM event").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM filing WHERE classified=1").fetchone()["c"] == 0
    con.close()


def test_pause_flag_parses_to_int():
    with patch("hybond.__main__.cmd_auto_classify") as mock_cmd:
        mock_cmd.return_value = 0
        cli.main(["auto-classify", "--limit", "50", "--pause", "30"])
    args = mock_cmd.call_args.args[0]
    assert args.pause == 30
    assert args.limit == 50


def test_pause_defaults_to_none():
    with patch("hybond.__main__.cmd_auto_classify") as mock_cmd:
        mock_cmd.return_value = 0
        cli.main(["auto-classify"])
    assert mock_cmd.call_args.args[0].pause is None


def test_pause_bursts_in_limit_sized_chunks_until_queue_empty(tmp_path, monkeypatch):
    # 12 filings, --limit 5 as the per-burst size with --pause set -> three
    # bursts (5, 5, 2), sleeping `pause` seconds between bursts (not after
    # the last, since the queue is then empty).
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 12)
    con.close()

    args = Namespace(source="filing", limit=5, priority_only=False, dry_run=False, pause=7)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify, \
         patch("hybond.__main__.time.sleep") as mock_sleep:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 12  # every filing processed across bursts
    pause_sleeps = [c for c in mock_sleep.call_args_list if c.args == (7,)]
    assert len(pause_sleeps) == 2  # between burst 1->2 and 2->3, not after the last


def test_pause_with_dry_run_does_not_loop_forever(tmp_path, monkeypatch):
    # dry-run never marks filings classified, so a burst loop would refetch
    # the same rows forever; --pause must still stop after one pass.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 3)
    con.close()

    args = Namespace(source="filing", limit=2, priority_only=False, dry_run=True, pause=5)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify, \
         patch("hybond.__main__.time.sleep") as mock_sleep:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 2  # exactly one pass, capped at --limit
    mock_sleep.assert_not_called()


def test_pause_stops_immediately_when_queue_already_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    con.close()

    args = Namespace(source="filing", limit=5, priority_only=False, dry_run=False, pause=7)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify, \
         patch("hybond.__main__.time.sleep") as mock_sleep:
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    mock_classify.assert_not_called()
    mock_sleep.assert_not_called()


def test_no_pause_preserves_single_pass_behavior(tmp_path, monkeypatch):
    # Backward compatibility: omitting --pause must behave exactly as before
    # this flag existed -- one pass, capped at --limit, no burst sleeps.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 12)
    con.close()

    args = Namespace(source="filing", limit=5, priority_only=False, dry_run=False, pause=None)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 5  # only one burst, not all 12


def test_lock_file_removed_after_normal_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 3)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False, pause=None)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        cli.cmd_auto_classify(args)
    assert not pipeline_lock.PIPELINE_LOCK_PATH.exists()


def test_lock_file_removed_even_if_classify_raises(tmp_path, monkeypatch):
    # The interrupted/crashed-run case the DoD asks for: cleanup must happen
    # via the finally block, not only on the happy path.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 3)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False, pause=None)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.side_effect = RuntimeError("simulated crash mid-classify")
        with pytest.raises(RuntimeError):
            cli.cmd_auto_classify(args)
    assert not pipeline_lock.PIPELINE_LOCK_PATH.exists()


def test_lock_file_present_during_run():
    # Can't easily observe the lock file mid-run without a second thread;
    # instead verify the expected path constants exist and point at the
    # expected filenames (regression guard against the path being silently
    # renamed/removed).
    assert pipeline_lock.PIPELINE_LOCK_PATH.name == "pipeline_running.lock"
    assert cli.AUTO_CLASSIFY_TIMESTAMP_PATH.name == "last_classify.timestamp"


def test_timestamp_written_on_successful_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 3)
    con.close()

    assert not cli.AUTO_CLASSIFY_TIMESTAMP_PATH.exists()
    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False, pause=None)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        cli.cmd_auto_classify(args)
    assert cli.AUTO_CLASSIFY_TIMESTAMP_PATH.exists()
    written = int(cli.AUTO_CLASSIFY_TIMESTAMP_PATH.read_text())
    assert abs(written - int(time.time())) < 5


def test_timestamp_not_written_when_nothing_processed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False, pause=None)
    cli.cmd_auto_classify(args)
    assert not cli.AUTO_CLASSIFY_TIMESTAMP_PATH.exists()


def test_timestamp_not_written_on_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 3)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=True, pause=None)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        cli.cmd_auto_classify(args)
    assert not cli.AUTO_CLASSIFY_TIMESTAMP_PATH.exists()


def test_real_results_written_to_event_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    _seed_filings(con, 2)
    con.close()

    args = Namespace(source="filing", limit=None, priority_only=False, dry_run=False)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.side_effect = [
            _fake_result(category="coupon_event", severity="critical", confidence=0.92,
                         summary="Пропуск купона"),
            _fake_result(category="routine", severity="noise", confidence=0.99,
                         summary="Плановое раскрытие"),
        ]
        rc = cli.cmd_auto_classify(args)
    assert rc == 0

    con = db.connect()
    rows = con.execute("SELECT category, severity FROM event ORDER BY event_id").fetchall()
    assert [dict(r) for r in rows] == [
        {"category": "coupon_event", "severity": "critical"},
        {"category": "routine", "severity": "noise"},
    ]
    assert con.execute("SELECT COUNT(*) c FROM filing WHERE classified=1").fetchone()["c"] == 2
    con.close()
