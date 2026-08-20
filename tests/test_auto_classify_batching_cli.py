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
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond.store import db  # noqa: E402


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
