"""ГИР БО financials ingestion — structured РСБУ statements by ИНН. No LLM, no
OCR. This is the Slice 4 breakthrough: instead of parsing scanned e-disclosure
PDFs, we pull machine-readable line items (keyed by standard Минфин 66н codes)
from the Federal Tax Service's open financial-statements resource.

Two backends, same normalised output:

  ParserApiBackend — parser-api.com wrapper. Documented, stable JSON, keyed by
                     ИНН. 200 free requests/month (covers a ~25-issuer annual
                     watchlist many times over). Needs an API key.
  NalogDirectBackend — bo.nalog.gov.ru directly. Free, the source, but the JSON
                     endpoints are undocumented/reverse-engineered and the site
                     has stability quirks. Documented stub — wire when needed.

Both return the same `FinancialsRecord` so downstream (compute/pillars.py) is
backend-agnostic. Snapshot-first: raw JSON cached before normalisation.

Coverage note (surfaced, not hidden): the record carries `has_consolidated`
(the source's `has_ks` flag). Single-entity РСБУ can understate debt for holding
companies whose business sits in subsidiaries — the cross-guarantee problem.
When has_consolidated is True, a later slice can pull IFRS; for now we flag it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

RAW_DIR = Path("data/raw/nalog")
PARSER_API_BASE = "https://parser-api.com/parser/nalog_bo_api"


@dataclass
class FinancialsRecord:
    inn: str
    org_name: str | None
    period: str                    # reporting year, e.g. "2025"
    actual_date: str | None        # date of the actual filing
    has_consolidated: bool         # source has_ks: consolidated stmts exist
    has_audit: bool
    # line items keyed by standard code -> current-period value (thousands RUB).
    # We keep the raw code map so pillars.py can pick exactly what it needs and
    # nothing is lost in normalisation.
    balance: dict[int, float] = field(default_factory=dict)
    financial_result: dict[int, float] = field(default_factory=dict)
    funds_movement: dict[int, float] = field(default_factory=dict)
    # previous-period balance, for year-over-year deltas in the pillars
    balance_prev: dict[int, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "inn": self.inn, "org_name": self.org_name, "period": self.period,
            "actual_date": self.actual_date,
            "has_consolidated": self.has_consolidated, "has_audit": self.has_audit,
            "balance": self.balance, "financial_result": self.financial_result,
            "funds_movement": self.funds_movement, "balance_prev": self.balance_prev,
        }


class Backend(Protocol):
    def search_id(self, inn: str) -> tuple[str, dict] | None: ...
    def details(self, org_id: str) -> dict: ...


class ParserApiBackend:
    def __init__(self, api_key: str, timeout: int = 40):
        import requests
        self._requests = requests
        self.key = api_key
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "key": self.key}
        r = self._requests.get(f"{PARSER_API_BASE}/{path}", params=params,
                               timeout=self.timeout)
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"parser-api: {j.get('error')} ({j.get('error_code')})")
        return j

    def search_id(self, inn: str) -> tuple[str, dict] | None:
        j = self._get("search", {"inn": inn})
        items = j.get("items", [])
        if not items:
            return None
        return str(items[0]["id"]), items[0]

    def details(self, org_id: str) -> dict:
        return self._get("details", {"id": org_id})


class NalogDirectBackend:
    """Direct bo.nalog.gov.ru access — DOCUMENTED STUB. The site exposes JSON via
    search + bfo detail endpoints (reverse-engineered from its own web app), but
    they are undocumented and unstable. Implement when you want to drop the
    wrapper dependency; the normaliser below already matches the shape.
    """
    def search_id(self, inn: str):
        raise NotImplementedError(
            "Direct bo.nalog.gov.ru backend not wired. Use ParserApiBackend "
            "(200 free req/month) or implement the search/bfo calls here.")

    def details(self, org_id: str):
        raise NotImplementedError


def _snapshot(name: str, text: str, day: date | None = None) -> Path:
    day = day or date.today()
    p = RAW_DIR / day.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    fp = p / name
    fp.write_text(text, encoding="utf-8")
    return fp


def _rows_to_map(rows: list[dict], value_key: str = "current") -> dict[int, float]:
    """Collapse a statement's row array to {code: value}, skipping nulls. Detail
    sub-rows are ignored here — pillars need top-level codes; details can be
    revisited if a pillar wants a breakdown."""
    out: dict[int, float] = {}
    for r in rows:
        code = r.get("code")
        val = r.get(value_key)
        if code is not None and val is not None:
            try:
                out[int(code)] = float(val)
            except (ValueError, TypeError):
                continue
    return out


def normalise(details: dict, inn: str, snapshot: bool = True) -> FinancialsRecord | None:
    """Turn a details() payload into a FinancialsRecord using the most recent
    report. Returns None if no reports are present."""
    if snapshot:
        _snapshot(f"nalog_{inn}.json", json.dumps(details, ensure_ascii=False))
    org = details.get("organization", {})
    reports = details.get("reports", [])
    if not reports:
        return None
    # most recent by period
    rep = max(reports, key=lambda r: str(r.get("period", "")))
    return FinancialsRecord(
        inn=inn,
        org_name=org.get("short_name") or org.get("full_name"),
        period=str(rep.get("period", "")),
        actual_date=rep.get("actual_bfo_date"),
        has_consolidated=bool(rep.get("organization_info", {}).get("has_ks")
                              or rep.get("has_ks", False)),
        has_audit=bool(rep.get("required_audit")),
        balance=_rows_to_map(rep.get("balance", []), "current"),
        balance_prev=_rows_to_map(rep.get("balance", []), "previous"),
        financial_result=_rows_to_map(rep.get("financial_result", []), "current"),
        funds_movement=_rows_to_map(rep.get("funds_movement", []), "current"),
    )


def fetch_financials(inn: str, backend: Backend, snapshot: bool = True) -> FinancialsRecord | None:
    hit = backend.search_id(inn)
    if not hit:
        return None
    org_id, _ = hit
    details = backend.details(org_id)
    return normalise(details, inn, snapshot=snapshot)
