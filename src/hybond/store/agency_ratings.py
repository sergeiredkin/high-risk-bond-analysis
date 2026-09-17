"""Agency-rating store fed by MOEX CCI (ingest/moex_ratings.py).

Deliberately separate from the legacy `rating` table: `rating` has a foreign
key to `issuer`, and `issuer` rows are what scope scrape/financials/universe
work (see AGENTS.md on watchlist_tier) -- ~370 universe issuers must not
become `issuer` rows just to hold a rating. `rating` stays as the manual /
legacy source; ratings.py's resolver reads both.

Three tables:
  agency_rating          what MOEX currently lists, one row per
                         (emitent_id, isin, agency, book); isin='' is the
                         issuer-level rating
  agency_rating_history  every distinct (level, date) ever observed, so a
                         downgrade or withdrawal leaves a trail
  agency_rating_fetch    per-target fetch bookkeeping: last attempt, last
                         success, outcome, and since when a previously
                         populated target has come back empty
"""
from __future__ import annotations

from datetime import date, timedelta

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

CREATE TABLE IF NOT EXISTS agency_rating_fetch (
    emitent_id   INTEGER NOT NULL,
    isin         TEXT NOT NULL DEFAULT '',
    inn          TEXT,
    fetched_at   TEXT NOT NULL,
    ok_at        TEXT,
    outcome      TEXT NOT NULL,
    n_rows       INTEGER,
    empty_since  TEXT,
    PRIMARY KEY (emitent_id, isin)
);
"""


def init(con) -> None:
    con.executescript(SCHEMA)
    con.commit()


def current_rows(con, emitent_id: int, isin: str = "") -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM agency_rating WHERE emitent_id=? AND isin=?", (emitent_id, isin))]


def apply_fetch(con, *, emitent_id: int, isin: str, inn: str | None, rows, today: date,
                fetched_at: str, outcome: str, empty_confirm_days: int) -> dict:
    """Write one target's fetch result. Returns what changed:
    {'changed': [(agency, book_id, old_raw, new_raw)], 'emptied': bool}.

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
    changed: list[tuple] = []
    emptied = False
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
                _delete(con, emitent_id, isin, key)
    elif outcome == "empty":
        if existing:
            emptied = True
            empty_since = (prev["empty_since"] if prev and prev["empty_since"] else d)
            since = date.fromisoformat(empty_since)
            if today - since >= timedelta(days=empty_confirm_days):
                for key, old in existing.items():
                    changed.append((key[0], key[1], old["raw_level"], None))
                    _delete(con, emitent_id, isin, key)
                empty_since = None
        else:
            ok_at = fetched_at
    else:
        empty_since = prev["empty_since"] if prev else None

    con.execute(
        "INSERT INTO agency_rating_fetch(emitent_id,isin,inn,fetched_at,ok_at,outcome,n_rows,"
        "empty_since) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(emitent_id,isin) DO UPDATE SET "
        "inn=COALESCE(excluded.inn, inn), fetched_at=excluded.fetched_at, ok_at=excluded.ok_at,"
        "outcome=excluded.outcome, n_rows=excluded.n_rows, empty_since=excluded.empty_since",
        (emitent_id, isin, inn, fetched_at, ok_at, outcome, len(rows), empty_since))
    return {"changed": changed, "emptied": emptied}


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
