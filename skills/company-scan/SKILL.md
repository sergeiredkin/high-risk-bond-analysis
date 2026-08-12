---
name: company-scan
description: Screen a Russian high-yield (ВДО) bond issuer that is NOT YET in hybond's database, using free public web sources, and persist the result into company_scan_result. Use this whenever the user asks to "scan"/"проверить" a company by name or ИНН that hybond doesn't already track, whenever working through hybond's auto-flagged "Кандидаты на проверку" candidate list (dashboard amber panel, or `hybond scan-candidates-list`), or whenever the deprecated `hybond scan-company`/`scan-candidates` CLI commands would previously have been reached for. Runs the full bond_screening_prompt_v3.5.1.md methodology with Claude Code's own web search — no ANTHROPIC_API_KEY, no external HTTP call — then stores the result via `hybond scan-company-store` so the dashboard can render it from the DB like any other saved scan.
---

# company-scan

Screen one Russian HY bond issuer from scratch using free public web sources, exactly
as `dashboard/template.html`'s "Проверить компанию" panel and the now-deprecated
`hybond scan-company` used to via a direct Anthropic API call. The difference here is
mechanical, not analytical: Claude Code runs the methodology itself, using its own web
search, and writes the result straight into `company_scan_result` via a small CLI
helper — no API key changes hands, no HTTP call leaves this session.

This is a Layer-0 research skill, not an interpretive one (contrast [[hy-event-classify]]):
it goes out and gathers facts about an issuer hybond has no data for, rather than reading
data hybond already has.

## When this applies vs. when it doesn't

- **Applies:** issuer name or ИНН not in hybond's `issuer` table — the "new candidate"
  case this skill and the deprecated CLI path both exist for.
- **Does not apply:** an issuer already on the watchlist. For those, hybond has real
  `rating`/`financials_snapshot`/`pillars_snapshot`/`event` data — use the per-bond
  detail panel's "Generate full analysis" (Part D, `skills/bond-narrative-card/PROMPT.md`),
  which reads that data directly instead of researching from the open web. Don't re-derive
  facts hybond already has structured just because this skill exists.

## Inputs

One of:
- A company name or ИНН typed by the user directly.
- One entry from the candidate todo list: run
  `PYTHONPATH=src python -m hybond scan-candidates-list` (no key needed) to get the
  same auto-flagged "new candidate" + "rescan-due" list the dashboard's amber panel
  shows, as JSON (`inn`, `name`, `reason`). Pick as many as the session should cover —
  there is no `--limit` here on purpose: you are directly in the loop deciding how many
  to run, the same way you'd pace any other multi-step task tonight, so no code-level
  safety cap is needed the way the deprecated batch CLI required one for unattended runs.

## Method

1. **Read `skills/bond-narrative-card/bond_screening_prompt_v3.5.1.md` in full.** This
   is the unedited, free-web-sources version of the methodology (NOT
   `skills/bond-narrative-card/PROMPT.md`, which is the hybond-database-adapted variant
   used by the per-bond detail panel for issuers hybond already tracks — wrong file for
   this skill). Follow every step (1–16) exactly as written: track selection, financial
   health with calculation transparency, refinancing profile, risk checks, bond terms,
   liquidity, event risk, default pathway, "Why This Bond?", investment fit, data
   coverage, decision logic with hard/soft stops and the cross-check rule.

2. **Research using your own web search**, not hybond's DB (there is none for this
   issuer) and not general knowledge — every fact must trace back to a source you
   actually looked at this session. Cite what you find (source + document date) the way
   the prompt's Calculation Transparency and source-attribution rules require. If
   something can't be verified from free sources, mark it **Not disclosed** and lower
   confidence accordingly — never fabricate a plausible number, exactly as Rule 1 of the
   prompt requires.

3. **Produce the full Step 16 markdown report** in the exact structure the prompt
   specifies.

4. **Append exactly one fenced ` ```json ` code block** after the markdown report, with
   this structure (identical contract to `NC_RENDER_ADDENDUM` in
   `dashboard/template.html` and `src/hybond/company_scan_pipeline.py` — every value
   must restate facts already established in the markdown above it, never introduce a
   new one only in the JSON):

   ```json
   {
     "issuer_name": string, "date": "YYYY-MM-DD", "track": "Large" | "Small",
     "issuer_quality": "Strong" | "Adequate" | "Weak" | "Distressed", "why_rating": string,
     "bond_suitability": "Buy" | "Speculative" | "Watchlist" | "Avoid",
     "confidence": "High" | "Medium" | "Low", "data_coverage_pct": number,
     "traffic_lights": [{"name": string, "color": "green"|"yellow"|"red", "note": string}],
     "strengths": [{"title": string, "desc": string}],
     "risks": [{"title": string, "desc": string}],
     "bottom_line": string,
     "default_pathway": {"primary_risk": string, "pathway": [string], "mitigants": [string], "probability": string},
     "monitoring": [string],
     "data_conflicts": [string],
     "missing_data": [string]
   }
   ```

5. **Persist the result.** Write the markdown report to a file and (if step 4 produced
   valid JSON) the JSON block to a second file, then call:

   ```
   PYTHONPATH=src python -m hybond scan-company-store "<ИНН-or-name-you-were-given>" \
     --markdown /path/to/report.md --card /path/to/card.json
   ```

   Omit `--card` if no JSON block was produced (the CLI stores the markdown-only report,
   same as the deprecated API path's own fallback). This calls
   `company_scan_pipeline.store_scan_result()`, which writes the same
   `company_scan_result` columns the deprecated `scan_company()`/API path did — same
   schema, same table, same rating/filing drift snapshot for future rescan comparisons,
   same rendering on the dashboard's "Сохранённые проверки" section. No token/cost
   counters are recorded this way (there is no API usage to report) — that's expected,
   not a bug; those columns stay `NULL` for skill-produced rows.

6. **Report the verdict inline** (issuer quality, bond suitability, confidence, coverage
   %) so the user sees the outcome without a separate DB query.

## Batch mode

For several candidates in one session: pull the todo list once via
`hybond scan-candidates-list`, then repeat steps 1–6 per issuer, checking in with the
user periodically (e.g. every 5–10) rather than silently burning through the whole list —
same judgment call you'd make on any long-running multi-step task. Each `scan-company-store`
call is independent, so a partial batch (interrupted, or deliberately stopped early)
leaves every already-stored result intact; nothing needs to be redone.

## Guardrails

- **Never invent data.** If free sources don't state a fact, write "Not disclosed," per
  the prompt's own Rule 1 — this skill inherits every guardrail in
  `bond_screening_prompt_v3.5.1.md` unchanged, not just the output shape.
- **Don't skip the JSON block** unless the report genuinely can't be reduced to it —
  the dashboard card view has nothing to render without it, though the markdown alone
  still stores and is still readable via the "Сохранённые проверки" history list.
- **Don't re-scan an issuer that's merely due for a routine check.** If `--card`/query
  resolves to an ИНН already in `company_scan_result` with no drift reason from
  `scan-candidates-list`, there's nothing new to find — skip it.
- **This skill does not touch the watchlist.** Scanning and storing a result is not a
  promotion decision — that remains a separate, explicit step (adding the issuer to
  `watchlist.json`), same as it always has been for the deprecated path.
