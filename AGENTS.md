# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- `CLAUDE_CODE_MEMO.md` is the long-form orientation doc (layout, cron jobs, pipeline); read it first.
- Production runs from `/home/sergei/Documents/high risk bond analysis` (cron rebuilds the dashboard every 30 min). Never work or run pipeline commands there from a task worktree.
- `data/` is gitignored except `data/exports/`; `data/dashboard_data.json` must be copied in from the primary checkout before `python3 scripts/build_dashboard.py`. The built `dashboard/index.html` is committed alongside its sources.
- Dashboard redesign ("Купон Лаб", 2026-09, three stages): `dashboard/template.html` + `dashboard/src/` rebuild the Claude Design reference `bond-lab-design-reference.html` (untracked, primary checkout root; its header comment is the spec). CSS tokens/components in `src/design-system.css` are verbatim; vanilla JS, no framework, one self-contained output file. Stages 1-2 (Обзор, Карта и скринер) are in; Портфель and Данные still render from `src/pages/pending.js`, and the old dashboard lives at `dashboard/legacy/template.html` until stage 3 ports them.
- D3 (cdnjs) is the dashboard's only external script, for the market map; everything else including the data is inlined. Without it `TerminalMap.mount()` says so and the screener still works — keep that fallback.
- `src/bonds.js` is the one bond model the map, screener and drawer read. Two sharp edges it exists to absorb: `pd_rating` is exported as a fraction while `DATA.pd_by_rating` is the same number in percent, and `DATA.rf_curve` keeps one bond per 0.1-year bucket, so it zigzags wherever that bond carried a fallback `rf_rate_pct`.
- Tests: `python3 -m pytest -q` (whole suite runs in under a minute).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
