"""Tests for classify_utils.is_likely_noise()/queue_bucket() -- the
substring-based noise pre-filter for hybond queue / market-notes-queue.

Note: sampled against the real hybond.sqlite queue while building this --
0 of 1864 real unclassified filing titles and 0 of 19 real market_note
headlines currently match NOISE_PATTERNS (real AZIPI titles use
"Выплаченные доходы"/"Начисленные доходы" phrasing, not "выплата купона").
These tests exercise the matching logic itself against the literal patterns
the task specified, not a claim that the patterns fire on real data today.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.classify_utils import is_likely_noise, queue_bucket  # noqa: E402


def test_noise_pattern_matches():
    assert is_likely_noise("Плановая выплата купона по облигациям выполнена по графику")
    assert is_likely_noise("Coupon payment made on schedule")
    assert is_likely_noise("Годовое общее собрание акционеров назначено на 30 июня")


def test_priority_overrides_noise():
    # Mentions a routine coupon phrase AND a priority word (default) --
    # priority must win, never silently buried as noise.
    title = "Плановая выплата купона приостановлена в связи с техническим дефолтом"
    assert not is_likely_noise(title)
    assert queue_bucket(title) == "priority"


def test_priority_pattern_matches():
    assert queue_bucket("Присвоение рейтинга эмитенту понижено") == "priority"
    assert queue_bucket("Объявлена реструктуризация выпуска") == "priority"
    assert queue_bucket("Technical default declared") == "priority"


def test_real_noise_phrasing_matches():
    # Added after auditing the real queue: literal AZIPI title, not the
    # generic "выплата купона" phrasing the original pattern list used.
    assert is_likely_noise("Выплаченные доходы по эмиссионным ценным бумагам эмитента")
    assert is_likely_noise("Начисленные доходы по эмиссионным ценным бумагам эмитента")


def test_real_priority_phrasing_matches():
    # Added after auditing the real queue: the actual AZIPI title for a
    # covenant/default-adjacent event and a put-right trigger, neither of
    # which contains "дефолт" or "оферта" literally.
    assert queue_bucket("Неисполнение обязательств эмитента перед владельцами облигаций") == "priority"
    assert queue_bucket("Возникновение у владельцев облигаций права требовать досрочного погашения") == "priority"


def test_normal_when_neither_matches():
    title = "Решения совета директоров (наблюдательного совета)"
    assert queue_bucket(title) == "normal"
    assert not is_likely_noise(title)


def test_case_insensitive():
    assert is_likely_noise("ROUTINE COUPON payment processed")
    assert queue_bucket("ДЕФОЛТ объявлен эмитентом") == "priority"


def test_empty_or_none_title_is_normal():
    assert queue_bucket("") == "normal"
    assert queue_bucket(None) == "normal"
    assert not is_likely_noise("")
