"""Deterministic query routing for safe RAG/SQL answer paths."""
from __future__ import annotations

from .bond_brief import is_bond_brief_question
from .current_risk import is_current_risk_question
from .issuer_brief import is_issuer_brief_question

_NUMERIC = (
    "купон", "доходност", "ytm", "ставк", "погаш", "оферт", "амортиз",
    "объем", "объём", "долг", "выручк", "ebitda", "финансов", "рейтинг",
    "g-spread", "спред", "isin", "инн", "дата",
)
_MIXED = ("почему", "объясни", "причин", "из-за чего", "что изменил", "динамик")
_OUT_OF_SCOPE = ("погода", "рецепт", "переведи", "напиши код", "программирован")


def classify_query(question: str) -> str:
    """Return the safe answer path without using an LLM."""
    value = question.casefold()
    if any(term in value for term in _OUT_OF_SCOPE):
        return "out_of_scope"
    if is_current_risk_question(question):
        return "current_risk"
    if is_bond_brief_question(question):
        return "bond_brief"
    if is_issuer_brief_question(question):
        return "issuer_brief"
    has_numeric = any(term in value for term in _NUMERIC)
    has_mixed = any(term in value for term in _MIXED)
    if has_numeric and has_mixed:
        return "mixed"
    if has_numeric:
        return "numeric_fact"
    return "narrative"
