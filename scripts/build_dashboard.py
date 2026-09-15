"""Embed data/dashboard_data.json into dashboard/template.html, producing a
single self-contained dashboard/index.html — no server, no CDN, no fetch/CORS
issues; just open it in a browser.

Run scripts/export_dashboard_data.py first to refresh the data.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "dashboard_data.json"
NARRATIVE_PROMPT = ROOT / "skills" / "bond-narrative-card" / "PROMPT.md"
COMPANY_SCAN_PROMPT = ROOT / "skills" / "bond-narrative-card" / "bond_screening_prompt_v3.5.1.md"

BUILDS = [
    (ROOT / "dashboard" / "template.html", ROOT / "dashboard" / "index.html"),
    # Pre-redesign dashboard, kept buildable while the Купон Лаб redesign is
    # rolled out page by page (stages 2-3 port its screener/map/portfolio
    # logic). Output is gitignored; delete this entry with the legacy file.
    (ROOT / "dashboard" / "legacy" / "template.html", ROOT / "dashboard" / "legacy" / "index.html"),
]

# /*__INCLUDE:src/app.css__*/ -- inlines a source file (path relative to the
# template's own directory) so the template can be split into CSS/JS files
# while the output stays one self-contained HTML file.
INCLUDE_RE = re.compile(r"/\*__INCLUDE:([\w./-]+)__\*/")


def inline_includes(template: str, base: Path) -> str:
    def repl(m: re.Match) -> str:
        text = (base / m.group(1)).read_text(encoding="utf-8")
        if "</script" in text or "</style" in text:
            raise ValueError(f"{m.group(1)} contains a closing </script or </style tag")
        return text
    return INCLUDE_RE.sub(repl, template)


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    payload = json.dumps(data, ensure_ascii=False)
    # </script> can't appear literally inside the embedded JSON blob
    payload = payload.replace("</script", "<\\/script")

    # skills/bond-narrative-card/PROMPT.md is the reasoning engine behind the
    # Part D "Generate full analysis" trigger -- embedded as a JS string
    # constant so the dashboard stays a single self-contained file (see
    # module docstring); the trigger's own JS only assembles ISSUER_DATA and
    # renders the model's response, it never reimplements this prompt.
    prompt_payload = json.dumps(NARRATIVE_PROMPT.read_text(encoding="utf-8"), ensure_ascii=False)
    prompt_payload = prompt_payload.replace("</script", "<\\/script")

    # bond_screening_prompt_v3.5.1.md is the ORIGINAL, unadapted, web-research
    # prompt -- the reasoning engine behind the "Проверить компанию" scan for
    # issuers not yet in hybond's DB (Task 2). Deliberately a separate file
    # from PROMPT.md above, never merged: PROMPT.md's data-gathering section
    # points at hybond tables that don't exist yet for an unonboarded issuer.
    company_scan_payload = json.dumps(COMPANY_SCAN_PROMPT.read_text(encoding="utf-8"), ensure_ascii=False)
    company_scan_payload = company_scan_payload.replace("</script", "<\\/script")

    for template_path, out_path in BUILDS:
        template = inline_includes(template_path.read_text(encoding="utf-8"), template_path.parent)
        html = template.replace("/*__DASHBOARD_DATA__*/", payload)
        html = html.replace("/*__NARRATIVE_PROMPT__*/", prompt_payload)
        html = html.replace("/*__COMPANY_SCAN_PROMPT__*/", company_scan_payload)
        out_path.write_text(html, encoding="utf-8")
        print(f"Wrote {out_path} ({len(html)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
