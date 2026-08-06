"""Unit tests for company_scan_pipeline.py -- no real network calls (the
Anthropic API call is mocked); DB-backed pieces use a real tmp-file SQLite
DB, same pattern as test_ofz_pipeline.py/test_portfolio.py."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import company_scan_pipeline as csp  # noqa: E402
from hybond.store import db  # noqa: E402


_UNIVERSE_COLS = (
    "secid,snap_date,short_name,emitent_inn,emitent_title,mat_date,"
    "offer_date,coupon_pct,list_level,qualified,last_pct,yield_moex,"
    "yield_to_offer,accrued_int,volume,num_trades,duration_moex,ytm,"
    "tenor_yrs,rf_rate,g_spread_bp,yield_source,rating,"
    "implied_fair_spread_bp,mispricing_bp,in_watchlist,signal_excluded,"
    "mod_dur,aey_base,aey_stress,aey_bull,aey_base_risk_adj,vdo_scope"
).split(",")


def _uni_row(**overrides):
    row = {c: None for c in _UNIVERSE_COLS}
    row["vdo_scope"] = 1
    row["snap_date"] = "2026-08-06"
    row.update(overrides)
    return row


def _fake_anthropic_response(text: str, usage: dict | None = None) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": usage or {"input_tokens": 100, "output_tokens": 50,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    }


# --- resolve_inn -------------------------------------------------------

def test_resolve_inn_accepts_bare_digit_string():
    assert csp.resolve_inn("7703550709") == "7703550709"


def test_resolve_inn_rejects_free_text():
    assert csp.resolve_inn("Л-Старт") is None


def test_resolve_inn_rejects_too_short_or_too_long():
    assert csp.resolve_inn("123") is None
    assert csp.resolve_inn("1234567890123") is None


# --- parse_scan_response -------------------------------------------------

def test_parse_scan_response_splits_markdown_and_json():
    text = '# Report\n\nSome analysis.\n\n```json\n{"issuer_name": "Foo"}\n```'
    card, markdown = csp.parse_scan_response(text)
    assert card == {"issuer_name": "Foo"}
    assert markdown == "# Report\n\nSome analysis."


def test_parse_scan_response_no_json_block_falls_back_to_markdown_only():
    text = "# Report\n\nJust prose, no fenced block."
    card, markdown = csp.parse_scan_response(text)
    assert card is None
    assert markdown == text


def test_parse_scan_response_malformed_json_falls_back_gracefully():
    text = '# Report\n\n```json\n{not valid json\n```'
    card, markdown = csp.parse_scan_response(text)
    assert card is None
    assert markdown == "# Report"


# --- _usage_tokens ---------------------------------------------------------

def test_usage_tokens_extracts_all_fields():
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5,
              "cache_creation_input_tokens": 3, "server_tool_use": {"web_search_requests": 4}}
    tok = csp._usage_tokens(usage)
    assert tok == {"input_tokens": 10, "cache_creation_tokens": 3, "cache_read_tokens": 5,
                    "output_tokens": 20, "web_searches": 4}


def test_usage_tokens_missing_fields_default_sanely():
    tok = csp._usage_tokens({})
    assert tok["input_tokens"] == 0
    assert tok["web_searches"] is None


# --- scan_company: no key -> error dict, no network call -------------------

def test_scan_company_no_key_returns_error_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    with patch("hybond.company_scan_pipeline.requests.post") as mock_post:
        result = csp.scan_company(con, "7703550709")
    assert "error" in result
    mock_post.assert_not_called()
    con.close()


# --- scan_company: mocked API call, full round trip through the DB --------

def test_scan_company_persists_result_and_returns_summary(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rating(con)
    db.init_filing(con)
    fake_text = ('# Bond Screening Report — Test Co\n\nBody text.\n\n'
                 '```json\n{"issuer_name": "Test Co", "issuer_quality": "Adequate", '
                 '"bond_suitability": "Watchlist", "confidence": "Medium", '
                 '"data_coverage_pct": 72}\n```')
    with patch("hybond.company_scan_pipeline.requests.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json.return_value = _fake_anthropic_response(fake_text)
        result = csp.scan_company(con, "1234567890", api_key="test-key")

    assert result["inn"] == "1234567890"
    assert result["card"]["issuer_quality"] == "Adequate"
    assert result["no_json_block"] is False

    rows = con.execute("SELECT * FROM company_scan_result").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["inn"] == "1234567890"
    assert row["issuer_quality"] == "Adequate"
    assert row["bond_suitability"] == "Watchlist"
    assert row["data_coverage_pct"] == 72
    assert "Body text." in row["raw_markdown"]
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    con.close()


def test_scan_company_free_text_query_not_tracked_as_inn(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    with patch("hybond.company_scan_pipeline.requests.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json.return_value = _fake_anthropic_response("# Report\n\nno json block")
        result = csp.scan_company(con, "Some Company LLC", api_key="test-key")
    assert result["inn"] is None
    row = con.execute("SELECT inn FROM company_scan_result").fetchone()
    assert row["inn"] is None
    con.close()


# --- list_scan_candidates: server-side port of the amber panel -------------

def test_list_scan_candidates_excludes_known_issuers(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.upsert_issuer(con, "1111111111", "Known Co", "leasing")
    db.insert_universe_snapshot(con, **_uni_row(
        secid="A1", emitent_inn="1111111111", emitent_title="Known Co",
        volume=1000, tenor_yrs=2.0, g_spread_bp=800, rating=None))
    db.insert_universe_snapshot(con, **_uni_row(
        secid="B1", emitent_inn="2222222222", emitent_title="Unknown Co",
        volume=1000, tenor_yrs=2.0, g_spread_bp=900, rating=None))
    con.commit()
    candidates = csp.list_scan_candidates(con)
    inns = {c["inn"] for c in candidates}
    assert "1111111111" not in inns  # already in `issuer` table
    assert "2222222222" in inns
    con.close()


def test_list_scan_candidates_excludes_rated_and_illiquid_and_short_tenor(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.insert_universe_snapshot(con, **_uni_row(
        secid="R1", emitent_inn="3333333333", emitent_title="Rated Co",
        volume=1000, tenor_yrs=2.0, g_spread_bp=900, rating="ruBB"))
    db.insert_universe_snapshot(con, **_uni_row(
        secid="I1", emitent_inn="4444444444", emitent_title="Illiquid Co",
        volume=10, tenor_yrs=2.0, g_spread_bp=900, rating=None))
    db.insert_universe_snapshot(con, **_uni_row(
        secid="S1", emitent_inn="5555555555", emitent_title="Short Tenor Co",
        volume=1000, tenor_yrs=0.1, g_spread_bp=900, rating=None))
    con.commit()
    candidates = csp.list_scan_candidates(con)
    assert candidates == []
    con.close()


def test_list_scan_candidates_keeps_best_bond_per_issuer(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.insert_universe_snapshot(con, **_uni_row(
        secid="X1", emitent_inn="6666666666", emitent_title="Two Bond Co",
        volume=1000, tenor_yrs=2.0, g_spread_bp=700, rating=None))
    db.insert_universe_snapshot(con, **_uni_row(
        secid="X2", emitent_inn="6666666666", emitent_title="Two Bond Co",
        volume=1000, tenor_yrs=3.0, g_spread_bp=1200, rating=None))
    con.commit()
    candidates = csp.list_scan_candidates(con)
    assert len(candidates) == 1
    assert candidates[0]["secid"] == "X2"  # higher G-spread bond wins
    con.close()


def test_list_scan_candidates_sorted_by_gspread_desc(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.insert_universe_snapshot(con, **_uni_row(
        secid="L1", emitent_inn="7777777777", emitent_title="Low Spread",
        volume=1000, tenor_yrs=2.0, g_spread_bp=500, rating=None))
    db.insert_universe_snapshot(con, **_uni_row(
        secid="H1", emitent_inn="8888888888", emitent_title="High Spread",
        volume=1000, tenor_yrs=2.0, g_spread_bp=1500, rating=None))
    con.commit()
    candidates = csp.list_scan_candidates(con)
    assert [c["inn"] for c in candidates] == ["8888888888", "7777777777"]
    con.close()


# --- needs_rescan: Task-B drift detection, DB-backed -----------------------

def test_needs_rescan_no_drift_returns_empty(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rating(con)
    db.upsert_issuer(con, "9999999999", "Test Co")
    db.upsert_rating(con, "9999999999", "АКРА", "ruBB", None, "confirmed", "2026-01-01")
    con.commit()
    last_scan = {"rating_snapshot": "ruBB", "rated_on_snapshot": "2026-01-01",
                 "filing_count_snapshot": 0}
    assert csp.needs_rescan(con, "9999999999", last_scan) == []
    con.close()


def test_needs_rescan_flags_rating_change(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rating(con)
    db.upsert_issuer(con, "9999999999", "Test Co")
    db.upsert_rating(con, "9999999999", "АКРА", "ruB", None, "downgraded", "2026-08-01")
    con.commit()
    last_scan = {"rating_snapshot": "ruBB", "rated_on_snapshot": "2026-01-01",
                 "filing_count_snapshot": 0}
    reasons = csp.needs_rescan(con, "9999999999", last_scan)
    assert len(reasons) == 1
    assert "ruB" in reasons[0]
    con.close()


def test_needs_rescan_flags_new_filings(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_filing(con)
    db.upsert_issuer(con, "9999999999", "Test Co")
    db.insert_filing(con, body_hash="h1", issuer_id="9999999999", edisclosure_id="azipi",
                     published="2026-08-01", title="New filing", url="http://x",
                     fetched_on="2026-08-01", classified=0)
    con.commit()
    last_scan = {"rating_snapshot": None, "rated_on_snapshot": None,
                 "filing_count_snapshot": 0}
    reasons = csp.needs_rescan(con, "9999999999", last_scan)
    assert len(reasons) == 1
    assert "AZIPI" in reasons[0]
    con.close()


# --- run_scan_candidates: batch mode, --limit, skip-already-scanned -------

def test_run_scan_candidates_no_key_returns_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    result = csp.run_scan_candidates(con)
    assert "error" in result
    con.close()


def test_run_scan_candidates_skips_already_scanned_no_drift(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_company_scan(con)
    db.init_rating(con)
    db.init_filing(con)
    db.insert_universe_snapshot(con, **_uni_row(
        secid="A1", emitent_inn="1010101010", emitent_title="Already Scanned Co",
        volume=1000, tenor_yrs=2.0, g_spread_bp=900, rating=None))
    con.commit()
    db.insert_company_scan_result(
        con, inn="1010101010", query="1010101010", scanned_at="2026-08-01T00:00:00",
        model="claude-sonnet-5", issuer_name="Already Scanned Co", track=None,
        issuer_quality="Adequate", bond_suitability="Watchlist", confidence="Medium",
        data_coverage_pct=70, raw_markdown="...", json_addendum=None,
        input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0,
        web_searches=0, elapsed_ms=1000,
        rating_snapshot=None, rated_on_snapshot=None,
        filing_count_snapshot=0, filing_latest_snapshot=None,
    )
    con.commit()

    with patch("hybond.company_scan_pipeline.requests.post") as mock_post:
        summary = csp.run_scan_candidates(con, api_key="test-key")
    mock_post.assert_not_called()  # no drift -> nothing due
    assert summary["candidates_found"] == 1
    assert summary["already_scanned"] == 1
    assert summary["due_this_run"] == 0
    assert summary["scanned_this_run"] == 0
    con.close()


def test_run_scan_candidates_respects_limit(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)
    db.init_rating(con)
    db.init_filing(con)
    for i in range(3):
        inn = f"20202020{i:02d}"
        db.insert_universe_snapshot(con, **_uni_row(
            secid=f"S{i}", emitent_inn=inn, emitent_title=f"Co {i}",
            volume=1000, tenor_yrs=2.0, g_spread_bp=900 + i, rating=None))
    con.commit()

    fake_text = '# Report\n\n```json\n{"issuer_name": "X"}\n```'
    with patch("hybond.company_scan_pipeline.requests.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json.return_value = _fake_anthropic_response(fake_text)
        summary = csp.run_scan_candidates(con, api_key="test-key", limit=2)

    assert summary["due_this_run"] == 3
    assert summary["scanned_this_run"] == 2
    assert summary["skipped_by_limit"] == 1
    assert mock_post.call_count == 2
    con.close()
