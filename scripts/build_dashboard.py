"""Embed data/dashboard_data.json into dashboard/template.html, producing a
single self-contained dashboard/index.html — no server, no CDN, no fetch/CORS
issues; just open it in a browser.

Run scripts/export_dashboard_data.py first to refresh the data.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "dashboard_data.json"

BUILDS = [
    (ROOT / "dashboard" / "template.html", ROOT / "dashboard" / "index.html"),
    (ROOT / "dashboard" / "market_map_template.html", ROOT / "dashboard" / "market_map.html"),
]


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    payload = json.dumps(data, ensure_ascii=False)
    # </script> can't appear literally inside the embedded JSON blob
    payload = payload.replace("</script", "<\\/script")
    for template_path, out_path in BUILDS:
        template = template_path.read_text(encoding="utf-8")
        html = template.replace("/*__DASHBOARD_DATA__*/", payload)
        out_path.write_text(html, encoding="utf-8")
        print(f"Wrote {out_path} ({len(html)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
