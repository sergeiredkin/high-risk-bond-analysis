"""Tests for classify_ollama.py -- classify_filing()/needs_review(). Ollama
itself is mocked (unittest.mock.patch on requests.post, same convention
tests/test_company_scan_pipeline.py already uses for the Anthropic API)."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.classify_ollama import (  # noqa: E402
    VALID_CATEGORIES, VALID_SEVERITIES, classify_filing, needs_review,
)


def _mock_response(response_text):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"response": response_text}
    return R()


def test_valid_response_parsed():
    body = ('{"category": "coupon_event", "severity": "critical", '
            '"confidence": 0.92, "summary": "Эмитент пропустил выплату купона"}')
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        result = classify_filing("Купон не выплачен", "http://x", "Тест ОАО")
    assert result == {"category": "coupon_event", "severity": "critical",
                       "confidence": 0.92, "summary": "Эмитент пропустил выплату купона"}


def test_markdown_fenced_response_stripped():
    body = ('```json\n{"category": "routine", "severity": "noise", '
            '"confidence": 0.99, "summary": "Плановое раскрытие без содержания"}\n```')
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        result = classify_filing("АГМ уведомление", "http://x", "Тест ОАО")
    assert result["category"] == "routine"
    assert result["severity"] == "noise"


def test_invalid_category_returns_none():
    # Not in the real taxonomy (this is the OLD task-prompt category we
    # deliberately did not use) -- must fail validation, not silently pass.
    body = ('{"category": "legal_action", "severity": "high", '
            '"confidence": 0.9, "summary": "Судебный иск против эмитента"}')
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        result = classify_filing("Иск", "http://x", "Тест ОАО")
    assert result is None


def test_invalid_severity_missing_medium_still_valid():
    # 'medium' IS a valid severity in the real taxonomy (unlike the original
    # task prompt, which omitted it) -- must be accepted.
    body = ('{"category": "capital_action", "severity": "medium", '
            '"confidence": 0.75, "summary": "Зарегистрирован новый выпуск облигаций"}')
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        result = classify_filing("Новый выпуск", "http://x", "Тест ОАО")
    assert result["severity"] == "medium"


def test_confidence_out_of_range_returns_none():
    body = ('{"category": "routine", "severity": "noise", '
            '"confidence": 1.5, "summary": "Плановое раскрытие информации"}')
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        result = classify_filing("test", "http://x", "Тест ОАО")
    assert result is None


def test_malformed_json_returns_none():
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response("not json at all {{{")
        result = classify_filing("test", "http://x", "Тест ОАО")
    assert result is None


def test_summary_too_short_returns_none():
    body = '{"category": "routine", "severity": "noise", "confidence": 0.9, "summary": "ok"}'
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        result = classify_filing("test", "http://x", "Тест ОАО")
    assert result is None


def test_timeout_returns_none():
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.Timeout()
        result = classify_filing("test", "http://x", "Тест ОАО")
    assert result is None


def test_connection_error_returns_none():
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError()
        result = classify_filing("test", "http://x", "Тест ОАО")
    assert result is None


def test_stream_false_and_temperature_bounded():
    # "What not to do": stream=True must never be sent, temperature must
    # stay <= 0.2.
    body = '{"category": "routine", "severity": "noise", "confidence": 0.9, "summary": "Плановое раскрытие"}'
    with patch("hybond.classify_ollama.requests.post") as mock_post:
        mock_post.return_value = _mock_response(body)
        classify_filing("test", "http://x", "Тест ОАО")
    _, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert payload["stream"] is False
    assert payload["options"]["temperature"] <= 0.2
    assert kwargs["timeout"] == 120


def test_taxonomy_matches_real_hy_event_classify_taxonomy():
    # Locks in the taxonomy-alignment decision: exactly the 10 categories /
    # 5 severities from skills/hy-event-classify/references/taxonomy.md, not
    # the smaller/incompatible set the original task prompt specified.
    assert VALID_CATEGORIES == {
        "reporting_delay", "coupon_event", "offer_put_call", "covenant",
        "rating_action", "governance", "insolvency", "operations",
        "capital_action", "routine",
    }
    assert VALID_SEVERITIES == {"noise", "low", "medium", "high", "critical"}


# ---- needs_review() --------------------------------------------------------

def test_needs_review_critical_always_true_even_high_confidence():
    assert needs_review("critical", 0.99) is True


def test_needs_review_high_always_true_even_high_confidence():
    assert needs_review("high", 0.95) is True


def test_needs_review_low_confidence_non_noise_true():
    assert needs_review("low", 0.70) is True
    assert needs_review("medium", 0.79) is True


def test_needs_review_noise_low_confidence_still_false():
    # noise is exempt from the confidence-based review flag (only the
    # confidence THRESHOLD gate in classify_filing()/cmd_auto_classify
    # decides whether a noise result is written at all).
    assert needs_review("noise", 0.66) is False


def test_needs_review_high_confidence_non_critical_false():
    assert needs_review("low", 0.90) is False
    assert needs_review("medium", 0.85) is False
