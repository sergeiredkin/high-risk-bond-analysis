"""Agency-rating store fed by MOEX CCI (ingest/moex_ratings.py).

Deliberately separate from the legacy `rating` table: `rating` has a foreign
key to `issuer`, and `issuer` rows are what scope scrape/financials/universe
work (see AGENTS.md on watchlist_tier) -- ~370 universe issuers must not
become `issuer` rows just to hold a rating. `rating` stays as the manual /
legacy source; ratings.py's resolver reads both.

Four tables:
  agency_rating          what MOEX currently lists, one row per
                         (emitent_id, isin, agency, book); isin='' is the
                         issuer-level rating
  agency_rating_history  every distinct (level, date) ever observed, so a
                         downgrade or withdrawal leaves a trail
  agency_rating_fetch    per-target fetch bookkeeping: last attempt, last
                         success, outcome, and since when a previously
                         populated target has come back empty
  rating_change          every detected rating change, per agency and level,
                         with the agency's action date and the day we saw it
                         -- what the dashboard's change feed/alerts read

Change detection has a baseline rule: a target's FIRST successful fetch
records nothing (the first production run would otherwise announce ~750
"new" ratings, and a bond entering the universe is not a rating action).
Confirmations (same level, newer date) are not changes either.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..compute import default_probs

SCHEMA = """
CREATE TABLE IF NOT EXISTS agency_rating (
    emitent_id   INTEGER NOT NULL,
    isin         TEXT NOT NULL DEFAULT '',
    inn          TEXT,
    agency       TEXT NOT NULL,
    agency_id    INTEGER,
    book_id      INTEGER NOT NULL,
    raw_level    TEXT NOT NULL,
    grade        TEXT,
    scale        TEXT NOT NULL,
    status       TEXT NOT NULL,
    rated_on     TEXT,
    published_on TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'moex_cci',
    PRIMARY KEY (emitent_id, isin, agency, book_id)
);
CREATE INDEX IF NOT EXISTS idx_agency_rating_inn ON agency_rating(inn, isin);
CREATE INDEX IF NOT EXISTS idx_agency_rating_isin ON agency_rating(isin);

CREATE TABLE IF NOT EXISTS agency_rating_history (
    emitent_id   INTEGER NOT NULL,
    isin         TEXT NOT NULL DEFAULT '',
    inn          TEXT,
    agency       TEXT NOT NULL,
    book_id      INTEGER NOT NULL,
    raw_level    TEXT NOT NULL,
    grade        TEXT,
    scale        TEXT NOT NULL,
    status       TEXT NOT NULL,
    rated_on     TEXT NOT NULL DEFAULT '',
    observed_on  TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'moex_cci',
    PRIMARY KEY (emitent_id, isin, agency, book_id, raw_level, rated_on)
);
CREATE INDEX IF NOT EXISTS idx_agency_rating_history_inn ON agency_rating_history(inn);

CREATE TABLE IF NOT EXISTS rating_change (
    change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_on     TEXT NOT NULL,
    rated_on        TEXT,
    emitent_id      INTEGER,
    isin            TEXT NOT NULL DEFAULT '',
    inn             TEXT,
    agency          TEXT NOT NULL,
    book_id         INTEGER,
    scale           TEXT NOT NULL DEFAULT 'national',
    kind            TEXT NOT NULL,
    old_grade       TEXT,
    new_grade       TEXT,
    old_raw         TEXT,
    new_raw         TEXT,
    resolved_before TEXT,
    resolved_after  TEXT,
    severity        TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'moex_cci',
    UNIQUE (emitent_id, isin, agency, book_id, old_raw, new_raw, rated_on)
);
CREATE INDEX IF NOT EXISTS idx_rating_change_detected ON rating_change(detected_on);
CREATE INDEX IF NOT EXISTS idx_rating_change_inn ON rating_change(inn);

CREATE TABLE IF NOT EXISTS agency_rating_fetch (
    emitent_id   INTEGER NOT NULL,
    isin         TEXT NOT NULL DEFAULT '',
    inn          TEXT,
    fetched_at   TEXT NOT NULL,
    ok_at        TEXT,
    first_ok_at  TEXT,
    outcome      TEXT NOT NULL,
    n_rows       INTEGER,
    empty_since  TEXT,
    PRIMARY KEY (emitent_id, isin)
);
"""


def init(con) -> None:
    con.executescript(SCHEMA)
    # agency_rating_fetch shipped before rating_change / first_ok_at (974e452);
    # CREATE TABLE IF NOT EXISTS leaves an already-created table's columns
    # alone, so a production DB from that run needs this column added by hand.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(agency_rating_fetch)")}
    if "first_ok_at" not in cols:
        con.execute("ALTER TABLE agency_rating_fetch ADD COLUMN first_ok_at TEXT")
        con.execute("UPDATE agency_rating_fetch SET first_ok_at = ok_at WHERE ok_at IS NOT NULL")
    con.commit()


def current_rows(con, emitent_id: int, isin: str = "") -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM agency_rating WHERE emitent_id=? AND isin=?", (emitent_id, isin))]


# Scales whose changes are reported. ОСК and other non-credit scales are not
# ratings of the bond (see ingest/moex_ratings.py) and never produce a change.
REPORTED_SCALES = ("national", "sf", "structural")
DOWN_KINDS = ("default", "downgrade", "withdrawn", "suspended", "dropped")

_SEVERITY = {"default": "critical", "downgrade": "high", "withdrawn": "medium",
             "suspended": "medium", "dropped": "medium", "upgrade": "low", "new": "low",
             "changed": "low"}
_SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def _rank(grade: str | None) -> int | None:
    return default_probs.rating_rank(grade.split(".")[0]) if grade else None


def classify_change(old: dict | None, new) -> str | None:
    """Kind of change between a stored current row (`old`, a dict) and a
    freshly fetched ParsedRating (`new`), or None when nothing reportable
    moved: confirmations (same level, newer date) and pure formatting."""
    if old is None and new is None:
        return None
    if new is None:
        return "dropped"
    if old is None:
        return "default" if new.status == "default" else "new"
    if old["raw_level"] == new.raw_level:
        return None
    if new.status == "default":
        return "default"
    if new.status in ("withdrawn", "suspended"):
        return new.status if old["status"] != new.status else None
    if old["status"] in ("withdrawn", "suspended", "unknown") or new.status == "unknown":
        return "new" if new.status == "live" else "changed"
    old_rank, new_rank = _rank(old["grade"]), _rank(new.grade)
    if old_rank is None or new_rank is None:
        return "changed"
    if new_rank > old_rank:
        return "downgrade"
    if new_rank < old_rank:
        return "upgrade"
    return None


def change_severity(kind: str, scale: str) -> str:
    sev = _SEVERITY.get(kind, "low")
    if scale != "national" and kind != "default":
        # a structured-finance move is shown, but it doesn't drive PD
        sev = _SEVERITY_ORDER[min(_SEVERITY_ORDER.index(sev) + 1, len(_SEVERITY_ORDER) - 1)]
    return sev


def _change(kind, *, emitent_id, isin, inn, agency, book_id, scale, old, new, today) -> dict:
    return {
        "detected_on": today.isoformat(),
        "rated_on": (new.rated_on if new is not None else None) or (old or {}).get("rated_on"),
        "emitent_id": emitent_id, "isin": isin, "inn": inn, "level": "issue" if isin else "issuer",
        "agency": agency, "book_id": book_id, "scale": scale, "kind": kind,
        "old_grade": (old or {}).get("grade"), "new_grade": new.grade if new is not None else None,
        "old_raw": (old or {}).get("raw_level"), "new_raw": new.raw_level if new is not None else None,
        "severity": change_severity(kind, scale),
    }


def record_changes(con, changes: list[dict], source: str = "moex_cci") -> int:
    """Insert detected changes; the UNIQUE key makes a re-run of the same day
    (or a replayed fetch) a no-op. Returns rows actually inserted."""
    n = 0
    for c in changes:
        cur = con.execute(
            "INSERT OR IGNORE INTO rating_change(detected_on,rated_on,emitent_id,isin,inn,agency,"
            "book_id,scale,kind,old_grade,new_grade,old_raw,new_raw,resolved_before,resolved_after,"
            "severity,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["detected_on"], c.get("rated_on"), c.get("emitent_id"), c.get("isin") or "",
             c.get("inn"), c["agency"], c.get("book_id"), c.get("scale") or "national", c["kind"],
             c.get("old_grade"), c.get("new_grade"), c.get("old_raw"), c.get("new_raw"),
             c.get("resolved_before"), c.get("resolved_after"), c["severity"], source))
        n += cur.rowcount
    return n


def manual_change(*, inn: str, agency: str, old_grade: str | None, new_grade: str,
                  rated_on: str, today: date, resolved_before: str | None,
                  resolved_after: str | None) -> dict | None:
    """A `hybond rate` entry as a rating_change row (None if the grade didn't move)."""
    if old_grade == new_grade:
        return None
    if new_grade == "D":
        kind = "default"
    elif old_grade is None:
        kind = "new"
    else:
        o, n = _rank(old_grade), _rank(new_grade)
        kind = "changed" if o is None or n is None else "downgrade" if n > o else "upgrade"
    return {"detected_on": today.isoformat(), "rated_on": rated_on, "emitent_id": None, "isin": "",
            "inn": inn, "agency": agency, "book_id": None, "scale": "national", "kind": kind,
            "old_grade": old_grade, "new_grade": new_grade, "old_raw": old_grade, "new_raw": new_grade,
            "resolved_before": resolved_before, "resolved_after": resolved_after,
            "severity": change_severity(kind, "national")}


def recent_changes(con, since: str) -> list[dict]:
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM rating_change WHERE detected_on >= ? "
            "ORDER BY detected_on DESC, rated_on DESC, change_id DESC", (since,))]
    except Exception:  # table not created yet
        return []


def baseline_since(con) -> str | None:
    """Day the change log started watching (the first successful fetch)."""
    try:
        row = con.execute("SELECT MIN(first_ok_at) AS t FROM agency_rating_fetch").fetchone()
    except Exception:
        return None
    return (row["t"] or "")[:10] or None if row else None


def apply_fetch(con, *, emitent_id: int, isin: str, inn: str | None, rows, today: date,
                fetched_at: str, outcome: str, empty_confirm_days: int) -> dict:
    """Write one target's fetch result. Returns what changed:
    {'changed': [(agency, book_id, old_raw, new_raw)], 'changes': [change dicts
    for rating_change, not yet recorded], 'emptied': bool, 'baseline': bool}.

    `baseline` is True on the target's first successful fetch: its rows are
    stored, but nothing counts as a change.

    - ok: upsert every row (history row per distinct level+date), and drop
      current rows MOEX no longer lists.
    - empty, target had rows before: keep them (last_seen not bumped, so the
      resolver flags them stale) until the list has stayed empty for
      `empty_confirm_days` -- an anonymous endpoint that silently starts
      answering empty must not wipe known ratings in one run.
    - empty, target never had rows: nothing to keep.
    - denied / error: bookkeeping only.
    """
    d = today.isoformat()
    prev = con.execute("SELECT * FROM agency_rating_fetch WHERE emitent_id=? AND isin=?",
                       (emitent_id, isin)).fetchone()
    existing = {(r["agency"], r["book_id"]): r for r in current_rows(con, emitent_id, isin)}
    baseline = not (prev and prev["ok_at"])
    changed: list[tuple] = []
    changes: list[dict] = []
    emptied = False

    def note(kind, key, old, new):
        scale = new.scale if new is not None else old["scale"]
        if baseline or kind is None or scale not in REPORTED_SCALES:
            return
        changes.append(_change(kind, emitent_id=emitent_id, isin=isin, inn=inn, agency=key[0],
                               book_id=key[1], scale=scale, old=old, new=new, today=today))
    empty_since = None
    ok_at = prev["ok_at"] if prev else None

    if outcome == "ok":
        ok_at = fetched_at
        seen = set()
        for r in rows:
            key = (r.agency, r.book_id)
            if key in seen:
                continue
            seen.add(key)
            old = existing.get(key)
            if old is None or old["raw_level"] != r.raw_level or old["rated_on"] != r.rated_on:
                changed.append((r.agency, r.book_id, old["raw_level"] if old else None, r.raw_level))
                note(classify_change(dict(old) if old else None, r), key, dict(old) if old else None, r)
            con.execute(
                "INSERT INTO agency_rating(emitent_id,isin,inn,agency,agency_id,book_id,raw_level,"
                "grade,scale,status,rated_on,published_on,first_seen,last_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(emitent_id,isin,agency,book_id) DO UPDATE SET "
                "inn=COALESCE(excluded.inn, inn), agency_id=excluded.agency_id,"
                "raw_level=excluded.raw_level, grade=excluded.grade, scale=excluded.scale,"
                "status=excluded.status, rated_on=excluded.rated_on,"
                "published_on=excluded.published_on, last_seen=excluded.last_seen",
                (emitent_id, isin, inn, r.agency, r.agency_id, r.book_id, r.raw_level, r.grade,
                 r.scale, r.status, r.rated_on, r.published_on, d, d))
            con.execute(
                "INSERT OR IGNORE INTO agency_rating_history(emitent_id,isin,inn,agency,book_id,"
                "raw_level,grade,scale,status,rated_on,observed_on) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (emitent_id, isin, inn, r.agency, r.book_id, r.raw_level, r.grade, r.scale,
                 r.status, r.rated_on or "", d))
        for key, old in existing.items():
            if key not in seen:
                changed.append((key[0], key[1], old["raw_level"], None))
                note("dropped", key, old, None)
                _delete(con, emitent_id, isin, key)
    elif outcome == "empty":
        if existing:
            emptied = True
            empty_since = (prev["empty_since"] if prev and prev["empty_since"] else d)
            since = date.fromisoformat(empty_since)
            if today - since >= timedelta(days=empty_confirm_days):
                for key, old in existing.items():
                    changed.append((key[0], key[1], old["raw_level"], None))
                    note("dropped", key, old, None)
                    _delete(con, emitent_id, isin, key)
                empty_since = None
        else:
            ok_at = fetched_at
    else:
        empty_since = prev["empty_since"] if prev else None

    con.execute(
        "INSERT INTO agency_rating_fetch(emitent_id,isin,inn,fetched_at,ok_at,first_ok_at,outcome,"
        "n_rows,empty_since) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(emitent_id,isin) DO UPDATE SET "
        "inn=COALESCE(excluded.inn, inn), fetched_at=excluded.fetched_at, ok_at=excluded.ok_at,"
        "first_ok_at=COALESCE(first_ok_at, excluded.first_ok_at),"
        "outcome=excluded.outcome, n_rows=excluded.n_rows, empty_since=excluded.empty_since",
        (emitent_id, isin, inn, fetched_at, ok_at, ok_at, outcome, len(rows), empty_since))
    return {"changed": changed, "changes": changes, "emptied": emptied, "baseline": baseline}


def _delete(con, emitent_id, isin, key) -> None:
    con.execute("DELETE FROM agency_rating WHERE emitent_id=? AND isin=? AND agency=? AND book_id=?",
                (emitent_id, isin, key[0], key[1]))


def fetch_state(con) -> dict[tuple[int, str], dict]:
    return {(r["emitent_id"], r["isin"]): dict(r)
            for r in con.execute("SELECT * FROM agency_rating_fetch")}


def issuer_rows_by_inn(con, inn: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM agency_rating WHERE inn=? AND isin=''", (inn,))]


def issuer_rows_by_emitent(con, emitent_id: int) -> list[dict]:
    return current_rows(con, emitent_id, "")


def issue_rows(con, isin: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM agency_rating WHERE isin=?", (isin,))]


def has_live_national(rows) -> bool:
    return any(r["scale"] == "national" and r["status"] in ("live", "default") for r in rows)


def last_refresh(con) -> str | None:
    try:
        row = con.execute("SELECT MAX(ok_at) AS t FROM agency_rating_fetch").fetchone()
    except Exception:  # table not created yet
        return None
    return row["t"] if row else None
