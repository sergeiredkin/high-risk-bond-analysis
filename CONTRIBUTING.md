# Contributing

## Definition of Done

A feature is complete when all of the following are satisfied.

### Correctness
1. The specified behavior works correctly across all described scenarios
2. Edge cases are handled: empty DB state, missing external data source, concurrent DB access, zero/null numeric values, `watchlist.json` absent

### Code quality
3. New Python functions have docstrings (parameters, return value, exceptions)
4. New JavaScript dashboard functions have JSDoc comments
5. Python code passes `ruff` linting with no errors; JS passes `eslint` if configured

### Testing
6. A unit test exists for every new function, covering happy path and at least one failure/edge case
7. No existing tests are deleted or weakened to make the suite pass
8. Full test suite passes with 0 failures

### DB integrity
9. Schema changes use conditional `ALTER TABLE` (check `PRAGMA table_info` first); migrations are non-destructive and idempotent
10. `watchlist.json` is not introduced as a primary data source; `issuer` DB table is the source of truth

### Live verification
11. For DB/pipeline/export tasks: at least one verification step runs against the real DB, not only mocked
12. If `daily_run.sh`, `export_dashboard_data.py`, or any pipeline step is modified: confirm dashboard snapshot date updates correctly after a manual run

### Frontend (dashboard tasks only)
13. Feature works correctly at desktop (1280px+) and mobile (375px) viewport widths
14. No JS console errors on page load (CDP-verified)

### Reference
This DoD applies to all Claude Code tasks in this project. Each task prompt should include:
> "All work must satisfy the Definition of Done in `CONTRIBUTING.md`."
