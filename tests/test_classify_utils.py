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


# ---- MOEX Listing / CBR Press source-specific patterns (2026-08-17) ------
# Titles below are copied VERBATIM from live fetches of both feeds
# (https://moex.com/export/news.aspx?cat=104, https://www.cbr.ru/rss/RssPress)
# pulled while building this task -- not invented examples. Real MOEX/CBR
# titles use inflected (genitive/prepositional/accusative) case forms, NOT
# the nominative/short-form phrasing a first guess reaches for -- e.g. real
# "О приостановке торгов" vs a naive "остановка торгов" guess, which is NOT
# a substring match. See classify_utils.py's own note on this.

def test_source_none_or_unrecognized_keeps_azipi_patterns_unchanged():
    # Existing AZIPI behaviour must be byte-identical whether source is
    # omitted, None, or some other value -- only 'moex_listing'/'cbr_press'
    # switch pattern lists.
    title = "Выплаченные доходы по эмиссионным ценным бумагам эмитента"
    assert queue_bucket(title) == queue_bucket(title, source=None) == "noise"
    assert queue_bucket(title, source="azipi") == "noise"
    assert queue_bucket(title, source="smartlab_news") == "noise"


def test_moex_priority_real_titles():
    assert queue_bucket(
        "О приостановке торгов ценными бумагами", source="moex_listing") == "priority"
    assert queue_bucket(
        "Об исключении ценных бумаг из Списка ценных бумаг, допущенных к торгам, "
        "и о прекращении торгов ценными бумагами", source="moex_listing") == "priority"
    assert queue_bucket(
        "О признании выпусков облигаций несостоявшимися, об аннулировании регистрации "
        "выпусков, об исключении их из Списка ценных бумаг, допущенных к торгам",
        source="moex_listing") == "priority"
    assert queue_bucket(
        "О выявленном несоответствии ценных бумаг", source="moex_listing") == "priority"


def test_moex_noise_real_titles():
    assert queue_bucket('О начале торгов ценными бумагами "17" августа  2026 года',
                         source="moex_listing") == "noise"
    assert queue_bucket(
        "О регистрации выпуска биржевых облигаций, о включении его в Список ценных "
        "бумаг, допущенных к торгам, и о присвоении указанным биржевым облигациям "
        "регистрационного номера", source="moex_listing") == "noise"
    assert queue_bucket("Итоги выпуска биржевых облигаций", source="moex_listing") == "noise"
    assert queue_bucket(
        "Об оставлении ценных бумаг в Списке ценных бумаг, допущенных к торгам",
        source="moex_listing") == "noise"


def test_moex_naive_nominative_form_would_not_match_confirming_case_sensitivity_matters():
    # The literal short-form phrasing a first guess would use ("остановка
    # торгов", "исключен из списка") does NOT appear in real MOEX titles --
    # documenting why the patterns use the inflected forms instead.
    assert "остановка торгов" not in "о приостановке торгов ценными бумагами"
    assert "исключен из списка" not in (
        "об исключении ценных бумаг из списка ценных бумаг, допущенных к торгам, "
        "и о прекращении торгов ценными бумагами")


def test_cbr_priority_real_title():
    assert queue_bucket(
        "Банк России принял решение снизить ключевую ставку на 25 б.п., "
        "до 14,00% годовых (24.07.2026)", source="cbr_press") == "priority"


def test_cbr_noise_real_title():
    assert queue_bucket(
        "Результаты мониторинга максимальных процентных ставок кредитных "
        "организаций (12.08.2026)", source="cbr_press") == "noise"


def test_cbr_normal_when_neither_matches_real_title():
    # Confirmed against the live feed: an insider-trading enforcement item
    # matches neither list (falls through to 'normal', not silently buried
    # as noise or auto-flagged priority without a matching pattern).
    assert queue_bucket(
        "Установлены факты неправомерного использования инсайдерской информации "
        "при совершении сделок с обыкновенными акциями ПАО «Софтлайн» на торгах "
        "ПАО Московская Биржа (22.07.2026)", source="cbr_press") == "normal"
