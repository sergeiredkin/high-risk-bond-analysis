"""Python API path for hy-event-classify — DOCUMENTED STUB (automation OFF).

This is the "later" path from PLAN.md: once the SKILL.md classifier is proven by
manual invocation, the identical prompt (SKILL.md body + taxonomy.md) can run via
the Anthropic API against each unclassified filing on cron, with no human in the
loop for routine tagging.

It is intentionally not wired to make live calls yet. Turning it on is a
deliberate step, not a default, because:
  - it starts spending tokens automatically, and
  - the classifier should be trusted (spot-checked against manual runs) first.

Model-escalation policy (return-maximizing rule 6): route routine filings to a
small/cheap model; escalate to a stronger model only when the issuer's factlist
shows it is already flagged — a filing on an already-stressed name earns the
better model.
"""
from __future__ import annotations

from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "hy-event-classify"


def _load_prompt() -> str:
    """The system prompt is the skill body + taxonomy, so manual and automated
    paths classify identically."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    taxonomy = (SKILL_DIR / "references" / "taxonomy.md").read_text(encoding="utf-8")
    return f"{body}\n\n---\n# TAXONOMY (authoritative)\n\n{taxonomy}"


def choose_model(issuer_flagged: bool) -> str:
    """Cheap model for routine tagging; strong model for already-stressed names."""
    return "claude-opus-4-8" if issuer_flagged else "claude-haiku-4-5-20251001"


def classify_filing(filing_text: str, factlist: dict, issuer_flagged: bool) -> dict:
    """STUB. When automation is enabled, this builds the messages array
    (system=_load_prompt(); user=factlist + filing_text), calls the API with
    choose_model(issuer_flagged), parses the JSON block from the response, and
    returns it for insertion into the `event` table.

    Left unimplemented on purpose. Enable by adding the anthropic client call
    here; the prompt and model policy are already defined above so enabling is a
    few lines, not a redesign.
    """
    raise NotImplementedError(
        "Classification automation is off by design. Invoke the hy-event-classify "
        "SKILL.md manually until the classifier is spot-checked, then wire the "
        "Anthropic client here. Prompt and model policy are ready above."
    )
