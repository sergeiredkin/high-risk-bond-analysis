"""The one place a bond's credit rating is decided.

Every consumer that used to call db.current_rating() (universe screen,
watchlist spread screen, portfolio monitor, factlist, company scan, dashboard
export) calls resolve() / resolved_grade() instead.

Sources, in order:
  1. MOEX CCI agency ratings (store/agency_ratings.py, fed daily by
     `hybond refresh-ratings`), keyed by emitent_id / ISIN / INN.
  2. The legacy `rating` table (manual entries, one-off agency imports),
     keyed by INN -- used only where MOEX has nothing for the issuer, or for
     one agency where our row is strictly newer than MOEX's (MOEX can lag an
     agency action by a day or two, e.g. a default).

Rules (captain's decision, 2026-09-17):
  - issue-level rating before issuer-level (it reflects guarantees and
    subordination, and is the only rating an SPV has);
  - across agencies, the most conservative (lowest) live national-scale
    grade; `split` flags disagreement, every agency is shown;
  - SD/RD/D map to 'D': surfaced, never fed into PD (the PD table has no D);
  - withdrawn and suspended ratings drop out; a bond left with none is
    unrated, with the withdrawal date kept for display;
  - structured-finance (.sf) and structural-note grades are shown, never
    modelled; standalone assessments (ОСК) are ignored.
No name matching anywhere.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .compute import default_probs
from .store import agency_ratings

# A MOEX rating not re-seen for this long is shown as "last known" (stale).
# The daily refresh re-sees every universe issuer, so 3 days means at least
# two missed refreshes, not one slow morning.
RATINGS_STALE_DAYS = 3

LIVE = ("live", "default")
DISPLAY_ONLY_SCALES = ("sf", "structural")


def _safe(fn, *args) -> list[dict]:
    try:
        return fn(*args)
    except sqlite3.OperationalError:  # store not initialised in this DB
        return []


def _legacy_rows(con, inn: str | None) -> list[dict]:
    if not inn:
        return []
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM rating WHERE issuer_id=? AND is_current=1", (inn,))]
    except sqlite3.OperationalError:
        return []


def _rank(grade: str | None) -> int:
    r = default_probs.rating_rank(grade) if grade else None
    return -1 if r is None else r


def _moex_entry(r: dict, level: str) -> dict:
    return {"agency": r["agency"], "grade": r["grade"], "raw": r["raw_level"],
            "rated_on": r["rated_on"], "scale": r["scale"], "status": r["status"],
            "level": level, "source": "moex", "last_seen": r["last_seen"]}


def _legacy_entry(r: dict) -> dict:
    grade = r["rating"]
    return {"agency": r["agency"], "grade": grade, "raw": grade, "rated_on": r["rated_on"],
            "scale": "national", "status": "default" if grade == "D" else "live",
            "level": "issuer", "source": "legacy", "last_seen": None}


def resolve(con, inn: str | None = None, secid: str | None = None,
            emitent_id: int | None = None, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    issue = _safe(agency_ratings.issue_rows, con, secid) if secid else []
    if inn:
        issuer = _safe(agency_ratings.issuer_rows_by_inn, con, inn)
    elif emitent_id is not None:
        issuer = _safe(agency_ratings.issuer_rows_by_emitent, con, emitent_id)
    else:
        issuer = []

    issue_entries = [_moex_entry(r, "issue") for r in issue]
    issuer_entries = [_moex_entry(r, "issuer") for r in issuer]
    national_issue = [e for e in issue_entries if e["scale"] == "national"]
    national_issuer = [e for e in issuer_entries if e["scale"] == "national"]

    if any(e["status"] in LIVE for e in national_issue):
        level, national = "issue", national_issue
    else:
        level, national = "issuer", national_issuer
        legacy = _legacy_rows(con, inn)
        if not issue_entries and not issuer_entries:
            real = [r for r in legacy if (r["agency"] or "").strip().lower() != "manual"]
            national = [_legacy_entry(r) for r in (real or legacy)]
        else:
            by_agency = {e["agency"]: i for i, e in enumerate(national)}
            for r in legacy:
                i = by_agency.get(r["agency"])
                if i is not None and (r["rated_on"] or "") > (national[i]["rated_on"] or ""):
                    national[i] = _legacy_entry(r)

    live = [e for e in national if e["status"] in LIVE and e["grade"]]
    grade = agency = rated_on = None
    if live:
        worst = max(live, key=lambda e: (_rank(e["grade"]), e["rated_on"] or ""))
        grade, agency, rated_on = worst["grade"], worst["agency"], worst["rated_on"]
    withdrawn = [e for e in national if e["status"] == "withdrawn"]
    display_only = [e for e in issue_entries + issuer_entries
                    if e["scale"] in DISPLAY_ONLY_SCALES and e["status"] in LIVE and e["grade"]]
    sf_grade = (max(display_only, key=lambda e: _rank(e["grade"].split(".")[0]))["grade"]
                if display_only else None)

    sources = {e["source"] for e in live}
    stale_cutoff = (as_of - timedelta(days=RATINGS_STALE_DAYS)).isoformat()
    moex_seen = [e["last_seen"] for e in live if e["source"] == "moex"]
    if moex_seen:
        stale = min(moex_seen) < stale_cutoff
    elif live:
        stale = any(_legacy_stale(e["rated_on"], as_of) for e in live)
    else:
        stale = False

    agencies = sorted(national + display_only,
                      key=lambda e: (e["scale"] != "national", e["status"] not in LIVE,
                                     -_rank((e["grade"] or "").split(".")[0] or None), e["agency"]))
    for e in agencies:
        e["used"] = bool(grade) and e["scale"] == "national" and e["grade"] == grade
    return {
        "grade": grade,
        "agency": agency,
        "rated_on": rated_on,
        "level": level if grade else None,
        "split": len({e["grade"] for e in live}) > 1,
        "defaulted": grade == "D",
        "withdrawn_on": (max((e["rated_on"] or "") for e in withdrawn) or None)
                        if withdrawn and not live else None,
        "suspended": not live and any(e["status"] == "suspended" for e in national),
        "sf_grade": sf_grade,
        "source": None if not sources else sources.pop() if len(sources) == 1 else "mixed",
        "stale": stale,
        "last_seen": min(moex_seen) if moex_seen else None,
        "agencies": agencies,
    }


def _legacy_stale(rated_on: str | None, as_of: date) -> bool:
    from .store import db
    if not rated_on:
        return True
    try:
        return db.rating_is_stale(rated_on, as_of)
    except ValueError:
        return True


def resolved_grade(con, inn: str | None, secid: str | None = None) -> str | None:
    """The grade PD/AEY/spread screens use: a canonical 'ruXX' string, 'D', or
    None. default_probs.lookup('D') is None, so a defaulted bond gets no PD."""
    return resolve(con, inn=inn, secid=secid)["grade"]
