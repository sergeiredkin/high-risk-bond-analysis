"""Tests for hybond.spravka_parser -- Справка tab (2026-08-17). Parses the
real spravka_vdo.md at repo root (authoritative source, not a fixture) plus
small inline-string unit tests for the parsing rules themselves."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import spravka_parser  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPRAVKA_MD = ROOT / "spravka_vdo.md"


def _real_data():
    return spravka_parser.parse(SPRAVKA_MD.read_text(encoding="utf-8"))


def test_real_document_has_6_blocks_and_58_questions_in_order():
    data = _real_data()
    assert len(data["blocks"]) == 6
    ids = [q["id"] for b in data["blocks"] for q in b["questions"]]
    assert ids == list(range(1, 59))


def test_real_document_block_ranges():
    data = _real_data()
    ranges = [b["range"] for b in data["blocks"]]
    assert ranges == ["Q1–10", "Q11–20", "Q21–30", "Q31–40", "Q41–50", "Q51–58"]


def test_q1_has_hybond_segment_with_market_and_cbr_buttons():
    data = _real_data()
    q1 = data["blocks"][0]["questions"][0]
    hybond_segs = [s for s in q1["segments"] if s["type"] == "hybond"]
    assert len(hybond_segs) == 1
    tabs = [b["tab"] for b in hybond_segs[0]["buttons"]]
    assert tabs == ["market", "cbr"]


def test_q42_formula_and_portfolio_scenarios_button():
    data = _real_data()
    q42 = next(q for b in data["blocks"] for q in b["questions"] if q["id"] == 42)
    formulas = [s for s in q42["segments"] if s["type"] == "formula"]
    assert any("AEY" in f["text"] for f in formulas)
    hybond_seg = next(s for s in q42["segments"] if s["type"] == "hybond")
    assert {"label": "Портфель → Сценарии", "tab": "portfolio", "opts": {"pftab": "scenarios"}} \
        in hybond_seg["buttons"]


def test_q44_table_parsed():
    data = _real_data()
    q44 = next(q for b in data["blocks"] for q in b["questions"] if q["id"] == 44)
    tables = [s for s in q44["segments"] if s["type"] == "table"]
    assert len(tables) == 1
    assert tables[0]["rows"][0] == ["Профиль", "Макс. доля 1 эмитента", "Макс. доля 1 сектора"]
    assert len(tables[0]["rows"]) == 4  # header + 3 data rows, separator row dropped


def test_q17_covenant_disclaimer_classified_as_warning_not_hybond():
    # "**В hybond →** ⚠ ..." paragraphs are pure disclaimers with no real
    # nav instruction -- must be classified as warning, not hybond.
    data = _real_data()
    q17 = next(q for b in data["blocks"] for q in b["questions"] if q["id"] == 17)
    types = [s["type"] for s in q17["segments"]]
    assert "warning" in types
    assert "hybond" not in types


def test_q53_has_both_warning_and_hybond_segments():
    data = _real_data()
    q53 = next(q for b in data["blocks"] for q in b["questions"] if q["id"] == 53)
    types = [s["type"] for s in q53["segments"]]
    assert "warning" in types
    assert "hybond" in types


def test_all_questions_have_at_least_one_segment():
    data = _real_data()
    for b in data["blocks"]:
        for q in b["questions"]:
            assert q["segments"], f"Q{q['id']} has no segments"


# ---- Inline unit tests for the parsing rules themselves ------------------

SAMPLE = """
## Блок 1 — Тест (Q1–2)

---

### Q1. Первый вопрос?

Первый абзац с **жирным** текстом и `inline_code`.

```
Формула = A + B
```

**В hybond →** Вкладка **Рынок** → тест.

⚠ **Ограничение hybond:** тестовое ограничение.

---

### Q2. Второй вопрос с таблицей?

| A | B |
|---|---|
| 1 | 2 |

**В hybond →** Вкладка **ЦБ РФ → Симулятор ставки** делает что-то.

---
"""


def test_inline_bold_and_code_escaped_and_converted():
    data = spravka_parser.parse(SAMPLE)
    q1 = data["blocks"][0]["questions"][0]
    p = next(s for s in q1["segments"] if s["type"] == "p")
    assert "<b>жирным</b>" in p["html"]
    assert "<code>inline_code</code>" in p["html"]


def test_formula_segment_captures_code_block_verbatim():
    data = spravka_parser.parse(SAMPLE)
    q1 = data["blocks"][0]["questions"][0]
    formula = next(s for s in q1["segments"] if s["type"] == "formula")
    assert formula["text"] == "Формула = A + B"


def test_bare_market_pattern_matched():
    data = spravka_parser.parse(SAMPLE)
    q1 = data["blocks"][0]["questions"][0]
    hybond = next(s for s in q1["segments"] if s["type"] == "hybond")
    assert hybond["buttons"] == [{"label": "Вкладка Рынок", "tab": "market", "opts": {}}]


def test_compound_pattern_claims_span_before_bare_pattern():
    data = spravka_parser.parse(SAMPLE)
    q2 = data["blocks"][0]["questions"][1]
    hybond = next(s for s in q2["segments"] if s["type"] == "hybond")
    # Only the compound match should fire -- not also a bare "Вкладка ЦБ РФ".
    assert hybond["buttons"] == [
        {"label": "ЦБ РФ → Симулятор ставки", "tab": "cbr", "opts": {"scrollTo": "cbrSimCard"}}
    ]


def test_table_segment_parsed():
    data = spravka_parser.parse(SAMPLE)
    q2 = data["blocks"][0]["questions"][1]
    table = next(s for s in q2["segments"] if s["type"] == "table")
    assert table["rows"] == [["A", "B"], ["1", "2"]]


def test_html_escaping_prevents_injection():
    text = """
## Блок 1 — Тест (Q1–1)

### Q1. <script>alert(1)</script>?

Текст с <b>уже html</b> и & амперсандом.

---
"""
    data = spravka_parser.parse(text)
    q1 = data["blocks"][0]["questions"][0]
    p = q1["segments"][0]
    assert "<b>уже html</b>" not in p["html"]
    assert "&lt;b&gt;" in p["html"]
    assert "&amp;" in p["html"]
