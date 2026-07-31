"""Tests for the broad ВДО-scope screen (Phase 1): bank-subordinated/perpetual
keyword heuristic, rating-band-or-unrated check, and the combined
compute_vdo_scope() predicate. Pure logic, no network, no DB.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.universe_pipeline import (  # noqa: E402
    compute_vdo_scope, is_bank_subordinated_perpetual, is_in_vdo_rating_band,
    VDO_MIN_G_SPREAD_BP, VDO_MIN_VOLUME,
)


# --- bank subordinated/perpetual keyword heuristic -------------------------

def test_flags_known_subordinated_and_perpetual_bonds():
    # Real SHORTNAME/SECNAME pairs verified live against the MOEX bond master
    # (see universe_pipeline.py's docstring for the full sample).
    cases = [
        ("ГПБ 01-АТ1", "ГПБ (АО) 01-АТ1"),
        ("ВТБСУБТ2-1", "Банк ВТБ ПАО СУБ-Т2-1"),
        ("Сбер2СУБ2R", "Сбербанк 002СУБ-02R"),
        ("СовкомТ2-1", "Совкомбанк Т2-01"),
        ("TCS perp1", "ТБанк TCS-perp1"),
        ("ДОМСУБТ2-1", "Банк ДОМ.РФ СУБ-T2-1"),
        ("ТКБ СУБ-ЗО", "ТКБ СУБ-ЗО"),
    ]
    for short_name, secname in cases:
        assert is_bank_subordinated_perpetual(short_name, secname), short_name


def test_does_not_flag_ordinary_corporate_names():
    # Regression guard: a bare "т2" substring match (rejected candidate
    # pattern) would have falsely caught tickers like this one.
    cases = [
        ("Роснфт2P1", "ПАО НК Роснефть 002P-01"),
        ("Систем1P28", "АФК Система БО-01P28"),
        ("МОНОП 1P04", "МОНОПОЛИЯ 001P-04"),
        ("Роделен2P4", "Роделен 002P-04"),
        (None, None),
    ]
    for short_name, secname in cases:
        assert not is_bank_subordinated_perpetual(short_name, secname), short_name


# --- rating band -------------------------------------------------------

def test_unrated_is_in_band():
    assert is_in_vdo_rating_band(None) is True


def test_ruBB_and_below_is_in_band():
    for r in ("ruBB", "ruBB-", "ruB+", "ruB", "ruB-", "ruCCC", "ruCC", "ruC"):
        assert is_in_vdo_rating_band(r) is True, r


def test_above_ruBB_is_out_of_band():
    for r in ("ruAAA", "ruAA", "ruA", "ruA-", "ruBBB+", "ruBBB", "ruBBB-", "ruBB+"):
        assert is_in_vdo_rating_band(r) is False, r


def test_unrecognised_rating_is_out_of_band():
    assert is_in_vdo_rating_band("not-a-rating") is False


# --- combined predicate -------------------------------------------------

def _passing_kwargs(**overrides):
    kw = dict(rating="ruBB-", g_spread_bp=VDO_MIN_G_SPREAD_BP + 1,
              volume=VDO_MIN_VOLUME, short_name="Обычный1P1", secname="Обычный Эмитент",
              signal_excluded=False)
    kw.update(overrides)
    return kw


def test_passes_when_all_criteria_met():
    assert compute_vdo_scope(**_passing_kwargs()) is True


def test_fails_when_signal_excluded():
    assert compute_vdo_scope(**_passing_kwargs(signal_excluded=True)) is False


def test_fails_when_bank_subordinated():
    assert compute_vdo_scope(
        **_passing_kwargs(short_name="ВТБСУБТ2-1", secname="Банк ВТБ ПАО СУБ-Т2-1")
    ) is False


def test_fails_when_rating_too_high():
    assert compute_vdo_scope(**_passing_kwargs(rating="ruBBB")) is False


def test_fails_below_spread_threshold():
    assert compute_vdo_scope(**_passing_kwargs(g_spread_bp=VDO_MIN_G_SPREAD_BP)) is False
    assert compute_vdo_scope(**_passing_kwargs(g_spread_bp=None)) is False


def test_fails_below_volume_floor():
    assert compute_vdo_scope(**_passing_kwargs(volume=VDO_MIN_VOLUME - 1)) is False
    assert compute_vdo_scope(**_passing_kwargs(volume=None)) is False


def test_passes_when_unrated_and_otherwise_qualifying():
    assert compute_vdo_scope(**_passing_kwargs(rating=None)) is True
