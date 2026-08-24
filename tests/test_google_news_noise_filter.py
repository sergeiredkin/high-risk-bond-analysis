"""Tests for the Google News market_note keyword noise pre-filter (Google
News Noise Pre-Filter task, 2026-08-24): db.is_google_news_noise()/
skip_noise_google_news()/unclassified_google_news(), the automatic
pre-filter step inside _auto_classify_pass(), and `hybond queue
--skip-noise-fast`."""
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond import pipeline_lock  # noqa: E402
from hybond.classify_ollama import MODEL  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "1234567890"


@pytest.fixture(autouse=True)
def _isolate_lock_and_timestamp_paths(tmp_path, monkeypatch):
    # Same isolation as test_auto_classify_batching_cli.py -- without this,
    # cmd_auto_classify writes real files into data/logs/ in the repo.
    monkeypatch.setattr(pipeline_lock, "PIPELINE_LOCK_PATH", tmp_path / "pipeline_running.lock")
    monkeypatch.setattr(cli, "AUTO_CLASSIFY_TIMESTAMP_PATH", tmp_path / "last_classify.timestamp")


def _note(con, source, headline, summary=None, note_id_hint="n1", url=None):
    db.insert_market_note(
        con, source=source, issuer_id=ISSUER, occurred="2026-08-24",
        headline=headline, summary=summary, url=url or f"http://x/{note_id_hint}",
        severity=None, category=None, confidence=None, fetched_on="2026-08-24",
        body_hash=f"h-{note_id_hint}")


def _fake_result(**overrides):
    base = {"category": "operations", "severity": "medium", "confidence": 0.9, "summary": "test"}
    base.update(overrides)
    return base


# ---- db.is_google_news_noise (pure keyword logic) --------------------------

def test_noise_true_when_no_credit_keyword_present():
    assert db.is_google_news_noise("Компания открыла новый офис в Москве", None) is True


def test_noise_false_when_credit_keyword_in_headline():
    assert db.is_google_news_noise("Компания допустила технический дефолт по купону", None) is False


def test_noise_false_when_credit_keyword_only_in_summary():
    assert db.is_google_news_noise("Обычная новость", "АКРА понизило рейтинг компании") is False


def test_noise_keyword_match_is_case_insensitive():
    assert db.is_google_news_noise("КОМПАНИЯ ДОПУСТИЛА ДЕФОЛТ", None) is False


def test_noise_true_for_empty_headline_and_summary():
    assert db.is_google_news_noise(None, None) is True


def test_noise_checks_multiple_credit_keywords():
    for kw in ["банкротство", "оферта", "суд", "просрочка", "иск", "арест",
               "взыскание", "реструктуризация", "мораторий", "ликвидация",
               "задолженность", "неплатёж", "кредитор", "облигации", "купон",
               "погашение", "рефинансирование"]:
        assert db.is_google_news_noise(f"Новость про {kw}", None) is False, kw


# ---- db.unclassified_google_news / skip_noise_google_news ------------------

def _con(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer")
    return con


def test_unclassified_google_news_only_returns_google_news_source(tmp_path):
    con = _con(tmp_path)
    _note(con, "google_news", "Компания открыла новый офис", note_id_hint="gn1")
    _note(con, "smartlab_news", "Компания открыла новый офис", note_id_hint="sl1")
    con.commit()

    rows = db.unclassified_google_news(con)
    con.close()
    assert len(rows) == 1
    assert rows[0]["source"] == "google_news"


def test_unclassified_google_news_excludes_already_classified(tmp_path):
    con = _con(tmp_path)
    _note(con, "google_news", "Компания открыла новый офис", note_id_hint="gn1")
    con.commit()
    note_id = con.execute("SELECT note_id FROM market_note").fetchone()["note_id"]
    db.set_market_note_classification(con, note_id, severity="noise", category="routine",
                                      confidence=1.0, classified_by="keyword_filter")
    con.commit()

    rows = db.unclassified_google_news(con)
    con.close()
    assert rows == []


def test_skip_noise_google_news_sets_expected_fields(tmp_path):
    con = _con(tmp_path)
    _note(con, "google_news", "Компания открыла новый офис", note_id_hint="gn1")
    con.commit()
    rows = db.unclassified_google_news(con)

    n = db.skip_noise_google_news(con, rows)
    assert n == 1
    row = con.execute("SELECT * FROM market_note WHERE note_id=?", (rows[0]["note_id"],)).fetchone()
    con.close()
    assert row["severity"] == "noise"
    assert row["category"] == "routine"  # NOT 'noise' -- 'noise' is a severity, not a taxonomy category
    assert row["classified_by"] == "keyword_filter"
    assert row["confidence"] == 1.0


# ---- automatic pre-filter inside _auto_classify_pass / cmd_auto_classify ---

def test_google_news_noise_row_classified_without_gpu_call(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, ISSUER, "Test Issuer", watchlist_tier=1)
    _note(con, "google_news", "Компания открыла новый офис в Москве", note_id_hint="gn1")
    con.commit()
    con.close()

    args = Namespace(source=None, limit=None, priority_only=False, dry_run=False)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 0  # never reached the GPU

    con = db.connect()
    row = con.execute("SELECT severity, category, classified_by FROM market_note").fetchone()
    con.close()
    assert row["severity"] == "noise"
    assert row["category"] == "routine"
    assert row["classified_by"] == "keyword_filter"


def test_google_news_keyword_match_row_still_sent_to_gpu(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, ISSUER, "Test Issuer", watchlist_tier=1)
    _note(con, "google_news", "Компания допустила технический дефолт по купону", note_id_hint="gn1")
    con.commit()
    con.close()

    args = Namespace(source=None, limit=None, priority_only=False, dry_run=False)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result(category="insolvency", severity="critical")
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 1  # sent to GPU, not fast-pathed

    con = db.connect()
    row = con.execute("SELECT severity, category, classified_by FROM market_note").fetchone()
    con.close()
    assert row["severity"] == "critical"
    assert row["category"] == "insolvency"
    assert row["classified_by"] == f"ollama:{MODEL}"


def test_non_google_news_market_note_bypasses_filter_goes_to_gpu(tmp_path, monkeypatch):
    """A smartlab_news/moex_listing/etc. row with no credit keyword must
    still be sent to the GPU -- the pre-filter only ever touches
    source='google_news' rows."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, ISSUER, "Test Issuer", watchlist_tier=1)
    _note(con, "smartlab_news", "Компания открыла новый офис в Москве", note_id_hint="sl1")
    con.commit()
    con.close()

    args = Namespace(source=None, limit=None, priority_only=False, dry_run=False)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0
    assert mock_classify.call_count == 1  # not fast-pathed, went to GPU as before


def test_dry_run_does_not_write_prefilter_classification(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.init_market_note(con)
    db.add_issuer_at_tier(con, ISSUER, "Test Issuer", watchlist_tier=1)
    _note(con, "google_news", "Компания открыла новый офис в Москве", note_id_hint="gn1")
    con.commit()
    con.close()

    args = Namespace(source=None, limit=None, priority_only=False, dry_run=True)
    with patch("hybond.classify_ollama.classify_filing") as mock_classify:
        # dry-run skips the prefilter entirely (see _auto_classify_pass),
        # so this row still reaches the GPU path below -- just never gets
        # written, since dry_run only gates the DB write, not the fetch.
        mock_classify.return_value = _fake_result()
        rc = cli.cmd_auto_classify(args)
    assert rc == 0

    con = db.connect()
    row = con.execute("SELECT severity FROM market_note").fetchone()
    con.close()
    assert row["severity"] is None  # untouched


# ---- hybond queue --skip-noise-fast -----------------------------------------

def test_skip_noise_fast_cli_marks_noise_leaves_gpu_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer")
    _note(con, "google_news", "Компания открыла новый офис", note_id_hint="gn1")
    _note(con, "google_news", "Компания открыла второй офис", note_id_hint="gn2")
    _note(con, "google_news", "Компания допустила технический дефолт", note_id_hint="gn3")
    con.commit()
    con.close()

    monkeypatch.setattr("builtins.input", lambda _: "y")
    rc = cli.cmd_queue(Namespace(skip_noise=False, skip_noise_fast=True))
    assert rc == 0

    con = db.connect()
    classified = con.execute(
        "SELECT COUNT(*) c FROM market_note WHERE classified_by='keyword_filter'").fetchone()["c"]
    unclassified = con.execute(
        "SELECT COUNT(*) c FROM market_note WHERE severity IS NULL").fetchone()["c"]
    con.close()
    assert classified == 2
    assert unclassified == 1  # the дефолт row stays queued for GPU

    out = capsys.readouterr().out
    assert "2 Google News note(s) match no credit keyword" in out
    assert "1 will remain queued for GPU classification" in out
    assert "Marked 2 Google News note(s)" in out


def test_skip_noise_fast_cli_declines_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer")
    _note(con, "google_news", "Компания открыла новый офис", note_id_hint="gn1")
    con.commit()
    con.close()

    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = cli.cmd_queue(Namespace(skip_noise=False, skip_noise_fast=True))
    assert rc == 0

    con = db.connect()
    row = con.execute("SELECT severity FROM market_note").fetchone()
    con.close()
    assert row["severity"] is None  # declined, nothing written


def test_skip_noise_fast_cli_no_matching_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_market_note(con)
    con.close()

    rc = cli.cmd_queue(Namespace(skip_noise=False, skip_noise_fast=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Нет Google News записей" in out
