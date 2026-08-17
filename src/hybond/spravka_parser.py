"""Справка tab (2026-08-17): parses spravka_vdo.md -- the hand-written ВДО
investor guide -- into a JSON-able structure the dashboard renders. This is
a lightweight parser for the SPECIFIC subset of markdown this one document
uses (## block headers, ### question headers, paragraphs, ``` fenced code,
**bold**, `inline code`, pipe tables, and the document's own "**В hybond
→**"/"⚠ **Ограничение...**" conventions) -- not a general markdown engine.

Nav-button extraction (Part 3): the document's "В hybond →" paragraphs
reference tabs/sections in free text (e.g. "Вкладка **Рынок**", "Портфель
→ Сценарии"). NAV_PATTERNS maps the phrases the task specified to real
gotoTab()-compatible destinations -- verified against the actual gotoTab()
implementation in dashboard/template.html, which takes bare tab names
("market", not "tab-market") and bare element IDs for scrollTo (no "#"
prefix, since it calls document.getElementById directly). Patterns are
tried longest/most-specific first so a compound match (e.g. "Рынок →
Динамика G-спреда") claims its span before the bare "Вкладка Рынок"
pattern can also match the same substring.
"""
from __future__ import annotations

import html
import re

BLOCK_RE = re.compile(r"^## Блок (\d+) — (.+?) \(Q(\d+)[–-]Q?(\d+)\)$")
QUESTION_RE = re.compile(r"^### Q(\d+)\. (.+)$")
TABLE_ROW_RE = re.compile(r"^\|.*\|$")
TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")

# Ordered most-specific-first so compound phrases (which contain a bare tab
# name as a substring) claim their span before the bare pattern also fires.
NAV_PATTERNS: list[tuple[str, str, dict]] = [
    (r"ЦБ РФ → Симулятор ставки", "cbr", {"scrollTo": "cbrSimCard"}),
    (r"ЦБ РФ → Лента событий", "cbr", {"scrollTo": "sec-events"}),
    (r"Портфель → Обзор → Рекомендации", "portfolio", {"pftab": "overview", "scrollTo": "pfRecommendations"}),
    (r"Портфель → Сценарии", "portfolio", {"pftab": "scenarios"}),
    (r"Портфель → Стена", "portfolio", {"pftab": "wall"}),
    (r"Портфель → Календарь", "portfolio", {"pftab": "calendar"}),
    (r"Рынок → Динамика G-спреда", "market", {"scrollTo": "sec-spread-delta"}),
    (r"Рынок → История G-спреда", "market", {"scrollTo": "sec-spread-history"}),
    (r"Рынок → Карта рынка", "market", {"mapView": True}),
    (r"Вотчлист → Проверить кандидата", "market", {"scrollTo": "sec-company-scan"}),
    (r"Вкладка Рынок", "market", {}),
    (r"Вкладка Вотчлист", "watchlist", {}),
    (r"Вкладка ЦБ РФ", "cbr", {}),
    (r"Вкладка Портфель", "portfolio", {}),
    (r"Вкладка Главная", "home", {}),
]


def _inline_html(text: str) -> str:
    """Escape, then re-apply the document's two inline styles: **bold**
    and `inline code`. Order matters -- escape first (safe: none of these
    marker characters are HTML-special), THEN convert markers to tags."""
    escaped = html.escape(text.strip())
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


def _extract_nav_buttons(raw_text: str) -> list[dict]:
    """Find every NAV_PATTERNS phrase in the plain (marker-stripped) text
    and return one button dict per non-overlapping match, in document
    order. See module docstring for the claimed-span algorithm."""
    plain = raw_text.replace("**", "")
    claimed: list[tuple[int, int]] = []
    found: list[tuple[int, dict]] = []
    for pattern, tab, opts in NAV_PATTERNS:
        for m in re.finditer(re.escape(pattern), plain):
            span = m.span()
            if any(not (span[1] <= c[0] or span[0] >= c[1]) for c in claimed):
                continue
            claimed.append(span)
            found.append((span[0], {"label": pattern, "tab": tab, "opts": opts}))
    found.sort(key=lambda x: x[0])
    return [b for _, b in found]


def _parse_paragraph(raw: str) -> dict:
    stripped = raw.strip()
    # "**В hybond →** ⚠ ..." -- a handful of questions embed the warning
    # directly in the hybond paragraph with no real nav instruction (see
    # module docstring); treat the whole paragraph as a warning in that
    # case rather than a hybond block with zero buttons.
    body_after_hybond = None
    if stripped.startswith("**В hybond →**"):
        body_after_hybond = stripped[len("**В hybond →**"):].strip()
    if stripped.startswith("⚠") or (body_after_hybond is not None and body_after_hybond.startswith("⚠")):
        return {"type": "warning", "html": _inline_html(stripped)}
    if body_after_hybond is not None:
        return {"type": "hybond", "html": _inline_html(stripped),
                "buttons": _extract_nav_buttons(body_after_hybond)}
    return {"type": "p", "html": _inline_html(stripped)}


def _parse_table(lines: list[str]) -> dict:
    rows = [ln for ln in lines if not TABLE_SEP_RE.match(ln.strip())]
    parsed = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    return {"type": "table", "rows": parsed}


def _parse_question_body(lines: list[str]) -> list[dict]:
    segments: list[dict] = []
    buf: list[str] = []
    in_code = False
    code_lines: list[str] = []
    table_lines: list[str] = []

    def flush_paragraph():
        if buf:
            text = "\n".join(buf).strip()
            if text:
                segments.append(_parse_paragraph(text))
            buf.clear()

    def flush_table():
        if table_lines:
            segments.append(_parse_table(table_lines))
            table_lines.clear()

    for line in lines:
        stripped = line.strip()
        if stripped == "```":
            if in_code:
                segments.append({"type": "formula", "text": "\n".join(code_lines)})
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                flush_table()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if stripped == "---":
            continue
        if TABLE_ROW_RE.match(stripped):
            flush_paragraph()
            table_lines.append(stripped)
            continue
        if table_lines and not stripped:
            flush_table()
            continue
        if not stripped:
            flush_paragraph()
            continue
        buf.append(stripped)
    flush_paragraph()
    flush_table()
    return segments


def parse(text: str) -> dict:
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    cur_block: dict | None = None
    cur_question: dict | None = None
    body_lines: list[str] = []

    def flush_question():
        nonlocal cur_question
        if cur_question is not None:
            cur_question["segments"] = _parse_question_body(body_lines)
            cur_block["questions"].append(cur_question)
            cur_question = None
        body_lines.clear()

    for line in lines:
        bm = BLOCK_RE.match(line.strip())
        qm = QUESTION_RE.match(line.strip())
        if bm:
            flush_question()
            cur_block = {"n": int(bm.group(1)), "title": bm.group(2).strip(),
                         "range": f"Q{bm.group(3)}–{bm.group(4)}", "questions": []}
            blocks.append(cur_block)
            continue
        if qm:
            flush_question()
            cur_question = {"id": int(qm.group(1)), "anchor": f"q{qm.group(1)}",
                            "title": qm.group(2).strip()}
            continue
        if cur_question is not None:
            body_lines.append(line)
    flush_question()
    return {"blocks": blocks}
