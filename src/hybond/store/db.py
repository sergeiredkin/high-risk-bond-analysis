"""SQLite persistence. Append-only daily snapshots so the diff engine (Slice 2)
has history. No LLM, no computation beyond storage.

Snapshot discipline: price_snapshot and spread_snapshot carry a `snap_date`;
we never UPDATE a prior day, only INSERT. Static descriptors (bond, issuer) are
upserted.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("data/db/hybond.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS issuer (
    issuer_id   TEXT PRIMARY KEY,      -- INN or stable slug
    name        TEXT NOT NULL,
    sector      TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS bond (
    secid       TEXT PRIMARY KEY,
    isin        TEXT,
    issuer_id   TEXT REFERENCES issuer(issuer_id),
    short_name  TEXT,
    mat_date    TEXT,                  -- ISO date
    face_value  REAL,
    face_unit   TEXT,
    coupon_pct  REAL,
    list_level  INTEGER,
    qualified   INTEGER,               -- 0/1
    updated     TEXT
);

CREATE TABLE IF NOT EXISTS price_snapshot (
    secid       TEXT REFERENCES bond(secid),
    snap_date   TEXT NOT NULL,
    last_pct    REAL,                  -- clean price, % of face
    yield_moex  REAL,                  -- MOEX-reported yield, %
    duration    REAL,                  -- MOEX-reported, days
    volume      REAL,
    num_trades  INTEGER,
    systime     TEXT,
    PRIMARY KEY (secid, snap_date)
);

CREATE TABLE IF NOT EXISTS spread_snapshot (
    secid       TEXT REFERENCES bond(secid),
    snap_date   TEXT NOT NULL,
    ytm         REAL,                  -- YTM, decimal -- see yield_source
    tenor_yrs   REAL,
    rf_rate     REAL,                  -- interpolated CBR, decimal
    g_spread_bp REAL,                  -- G-spread, bp -- see yield_source
    mod_dur     REAL,                  -- modified duration (or MOEX days/365
                                        -- proxy when yield_source='moex')
    convexity   REAL,                  -- NULL when yield_source='moex'
    yield_source TEXT DEFAULT 'computed', -- 'computed' (from cashflows) or
                                        -- 'moex' (floater w/ unfixed future
                                        -- coupons -- see pipeline.py)
    PRIMARY KEY (secid, snap_date)
);

-- placeholders wired in later slices
CREATE TABLE IF NOT EXISTS financials (
    issuer_id   TEXT REFERENCES issuer(issuer_id),
    period      TEXT,                  -- e.g. 2025Q4
    metric      TEXT,
    value       REAL,
    source      TEXT,
    entered     TEXT,
    PRIMARY KEY (issuer_id, period, metric)
);

CREATE TABLE IF NOT EXISTS event (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id   TEXT REFERENCES issuer(issuer_id),
    occurred    TEXT,
    category    TEXT,
    severity    TEXT,
    summary     TEXT,
    source_url  TEXT
);

CREATE TABLE IF NOT EXISTS thesis (
    issuer_id   TEXT PRIMARY KEY REFERENCES issuer(issuer_id),
    thesis      TEXT,
    assumptions TEXT,
    invalidation TEXT,
    status      TEXT,
    updated     TEXT
);

-- Slice 2: every detector logs what it WOULD flag, every run, so the three
-- versions can be graded against one shared history. `driving` marks which
-- detector actually drove attention that day (Version A early on).
CREATE TABLE IF NOT EXISTS flag_log (
    flag_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date   TEXT NOT NULL,
    secid       TEXT REFERENCES bond(secid),
    issuer_id   TEXT REFERENCES issuer(issuer_id),
    detector    TEXT NOT NULL,          -- 'fixed' | 'stat' | 'hybrid'
    signal      TEXT NOT NULL,          -- 'spread_move' | 'volume_zero' | ...
    metric      REAL,                   -- the raw delta / z that fired
    detail      TEXT,                   -- human-readable reason
    driving     INTEGER DEFAULT 0       -- 1 if this detector drove attention
);

-- Slice 2: outcome grading. Filled in weeks later — did a real event follow a
-- flag? Left NULL until graded. This is the signal-scoring feedback loop.
CREATE TABLE IF NOT EXISTS flag_outcome (
    flag_id     INTEGER PRIMARY KEY REFERENCES flag_log(flag_id),
    followed_by TEXT,                   -- what actually happened, if anything
    graded_true INTEGER,                -- 1 true positive, 0 false positive, NULL ungraded
    graded_on   TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    # Resolve at call time so DB_PATH can be reassigned (tests, alt configs)
    # after import without the default being frozen at definition time.
    path = path if path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # spread_snapshot may already exist from before yield_source was added;
    # CREATE TABLE IF NOT EXISTS above won't retrofit the column onto it.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(spread_snapshot)")}
    if "yield_source" not in cols:
        con.execute(
            "ALTER TABLE spread_snapshot ADD COLUMN yield_source TEXT DEFAULT 'computed'")
    con.commit()


def upsert_issuer(con, issuer_id: str, name: str, sector: str | None = None, notes: str | None = None):
    con.execute(
        "INSERT INTO issuer(issuer_id,name,sector,notes) VALUES(?,?,?,?) "
        "ON CONFLICT(issuer_id) DO UPDATE SET name=excluded.name, "
        "sector=excluded.sector, notes=excluded.notes",
        (issuer_id, name, sector, notes),
    )


def upsert_bond(con, **f):
    con.execute(
        "INSERT INTO bond(secid,isin,issuer_id,short_name,mat_date,face_value,"
        "face_unit,coupon_pct,list_level,qualified,updated) "
        "VALUES(:secid,:isin,:issuer_id,:short_name,:mat_date,:face_value,"
        ":face_unit,:coupon_pct,:list_level,:qualified,:updated) "
        "ON CONFLICT(secid) DO UPDATE SET isin=excluded.isin, "
        "issuer_id=excluded.issuer_id, short_name=excluded.short_name, "
        "mat_date=excluded.mat_date, face_value=excluded.face_value, "
        "face_unit=excluded.face_unit, coupon_pct=excluded.coupon_pct, "
        "list_level=excluded.list_level, qualified=excluded.qualified, "
        "updated=excluded.updated",
        f,
    )


def insert_price_snapshot(con, **f):
    con.execute(
        "INSERT OR REPLACE INTO price_snapshot(secid,snap_date,last_pct,"
        "yield_moex,duration,volume,num_trades,systime) VALUES(:secid,:snap_date,"
        ":last_pct,:yield_moex,:duration,:volume,:num_trades,:systime)",
        f,
    )


def insert_spread_snapshot(con, **f):
    f.setdefault("yield_source", "computed")
    con.execute(
        "INSERT OR REPLACE INTO spread_snapshot(secid,snap_date,ytm,tenor_yrs,"
        "rf_rate,g_spread_bp,mod_dur,convexity,yield_source) VALUES(:secid,"
        ":snap_date,:ytm,:tenor_yrs,:rf_rate,:g_spread_bp,:mod_dur,:convexity,"
        ":yield_source)",
        f,
    )


# --- Slice 2: history reads + flag logging --------------------------------

def spread_history(con, secid: str, before: str, limit: int = 40) -> list:
    """Prior spread snapshots for one bond, newest first, strictly before
    `before` (ISO date). Feeds both the 1-day delta and the rolling z-score."""
    return con.execute(
        "SELECT snap_date, ytm, g_spread_bp, mod_dur FROM spread_snapshot "
        "WHERE secid=? AND snap_date<? ORDER BY snap_date DESC LIMIT ?",
        (secid, before, limit),
    ).fetchall()


def price_history(con, secid: str, before: str, limit: int = 40) -> list:
    return con.execute(
        "SELECT snap_date, last_pct, yield_moex, volume, num_trades "
        "FROM price_snapshot WHERE secid=? AND snap_date<? "
        "ORDER BY snap_date DESC LIMIT ?",
        (secid, before, limit),
    ).fetchall()


def latest_spread(con, secid: str, on_date: str):
    return con.execute(
        "SELECT snap_date, ytm, g_spread_bp, mod_dur, yield_source "
        "FROM spread_snapshot WHERE secid=? AND snap_date=?",
        (secid, on_date),
    ).fetchone()


def latest_price(con, secid: str, on_date: str):
    return con.execute(
        "SELECT snap_date, last_pct, yield_moex, volume, num_trades "
        "FROM price_snapshot WHERE secid=? AND snap_date=?",
        (secid, on_date),
    ).fetchone()


def insert_flag(con, **f) -> int:
    cur = con.execute(
        "INSERT INTO flag_log(snap_date,secid,issuer_id,detector,signal,"
        "metric,detail,driving) VALUES(:snap_date,:secid,:issuer_id,:detector,"
        ":signal,:metric,:detail,:driving)",
        f,
    )
    return cur.lastrowid


def flags_for_grading(con, detector: str | None = None) -> list:
    q = ("SELECT f.* FROM flag_log f LEFT JOIN flag_outcome o "
         "ON f.flag_id=o.flag_id WHERE o.flag_id IS NULL")
    args: tuple = ()
    if detector:
        q += " AND f.detector=?"
        args = (detector,)
    return con.execute(q, args).fetchall()


# --- Slice 3: filing feed (dedupe memory for the scraper) -----------------

FILING_SCHEMA = """
CREATE TABLE IF NOT EXISTS filing (
    body_hash    TEXT PRIMARY KEY,      -- stable id (sha1 of title+url)
    issuer_id    TEXT REFERENCES issuer(issuer_id),
    edisclosure_id TEXT,
    published    TEXT,
    title        TEXT,
    url          TEXT,
    fetched_on   TEXT,
    classified   INTEGER DEFAULT 0      -- 1 once hy-event-classify has run
);
"""


def init_filing(con) -> None:
    con.executescript(FILING_SCHEMA)
    con.commit()


def seen_filing_hashes(con, issuer_id: str | None = None) -> set:
    if issuer_id:
        rows = con.execute("SELECT body_hash FROM filing WHERE issuer_id=?",
                           (issuer_id,)).fetchall()
    else:
        rows = con.execute("SELECT body_hash FROM filing").fetchall()
    return {r["body_hash"] for r in rows}


def insert_filing(con, **f) -> None:
    con.execute(
        "INSERT OR IGNORE INTO filing(body_hash,issuer_id,edisclosure_id,"
        "published,title,url,fetched_on,classified) VALUES(:body_hash,:issuer_id,"
        ":edisclosure_id,:published,:title,:url,:fetched_on,0)", f)


def unclassified_filings(con, issuer_id: str | None = None) -> list:
    q = "SELECT * FROM filing WHERE classified=0"
    args: tuple = ()
    if issuer_id:
        q += " AND issuer_id=?"
        args = (issuer_id,)
    return con.execute(q + " ORDER BY published DESC", args).fetchall()


def mark_classified(con, body_hash: str) -> None:
    con.execute("UPDATE filing SET classified=1 WHERE body_hash=?", (body_hash,))


def insert_event(con, **f) -> int:
    cur = con.execute(
        "INSERT INTO event(issuer_id,occurred,category,severity,summary,source_url) "
        "VALUES(:issuer_id,:occurred,:category,:severity,:summary,:source_url)", f)
    return cur.lastrowid


def events_since(con, issuer_id: str, since_date: str) -> list:
    return con.execute(
        "SELECT * FROM event WHERE issuer_id=? AND occurred>=? ORDER BY occurred DESC",
        (issuer_id, since_date)).fetchall()


# --- Slice 4: financials snapshot + pillars + thesis ----------------------

PILLARS_SCHEMA = """
CREATE TABLE IF NOT EXISTS financials_snapshot (
    issuer_id   TEXT REFERENCES issuer(issuer_id),
    period      TEXT,                  -- reporting year
    fetched_on  TEXT,
    has_consolidated INTEGER,
    has_audit   INTEGER,
    payload     TEXT,                  -- full normalised FinancialsRecord JSON
    PRIMARY KEY (issuer_id, period)
);

CREATE TABLE IF NOT EXISTS pillars_snapshot (
    issuer_id   TEXT REFERENCES issuer(issuer_id),
    period      TEXT,
    computed_on TEXT,
    payload     TEXT,                  -- full Pillars JSON (metrics + scores)
    PRIMARY KEY (issuer_id, period)
);
"""


def init_pillars(con) -> None:
    con.executescript(PILLARS_SCHEMA)
    con.commit()


def upsert_financials(con, issuer_id, period, fetched_on, has_consolidated,
                      has_audit, payload_json) -> None:
    con.execute(
        "INSERT OR REPLACE INTO financials_snapshot(issuer_id,period,fetched_on,"
        "has_consolidated,has_audit,payload) VALUES(?,?,?,?,?,?)",
        (issuer_id, period, fetched_on, int(bool(has_consolidated)),
         int(bool(has_audit)), payload_json))


def upsert_pillars(con, issuer_id, period, computed_on, payload_json) -> None:
    con.execute(
        "INSERT OR REPLACE INTO pillars_snapshot(issuer_id,period,computed_on,"
        "payload) VALUES(?,?,?,?)",
        (issuer_id, period, computed_on, payload_json))


def latest_pillars(con, issuer_id):
    return con.execute(
        "SELECT * FROM pillars_snapshot WHERE issuer_id=? "
        "ORDER BY period DESC LIMIT 1", (issuer_id,)).fetchone()


def upsert_thesis(con, issuer_id, thesis, assumptions, invalidation, status, updated):
    con.execute(
        "INSERT INTO thesis(issuer_id,thesis,assumptions,invalidation,status,updated) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(issuer_id) DO UPDATE SET thesis=excluded.thesis,"
        "assumptions=excluded.assumptions,invalidation=excluded.invalidation,"
        "status=excluded.status,updated=excluded.updated",
        (issuer_id, thesis, assumptions, invalidation, status, updated))


def get_thesis(con, issuer_id):
    return con.execute("SELECT * FROM thesis WHERE issuer_id=?", (issuer_id,)).fetchone()


def set_thesis_status(con, issuer_id, status, updated):
    con.execute("UPDATE thesis SET status=?, updated=? WHERE issuer_id=?",
                (status, updated, issuer_id))


# --- Ratings: the bridge feeding the default-probability layer ------------
# (see DEFAULT_PROB_FEATURE.md). Historical, append-only by (issuer_id,
# agency, rated_on) -- upsert_rating flips prior rows for the same
# issuer+agency to is_current=0 rather than overwriting them, so
# rating_history stays a real history, not just the latest snapshot.

RATING_SCHEMA = """
CREATE TABLE IF NOT EXISTS rating (
    issuer_id    TEXT REFERENCES issuer(issuer_id),
    secid        TEXT,
    agency       TEXT NOT NULL,
    rating       TEXT NOT NULL,
    outlook      TEXT,
    action       TEXT,
    rated_on     TEXT NOT NULL,
    source       TEXT DEFAULT 'manual',
    is_current   INTEGER DEFAULT 1,
    PRIMARY KEY (issuer_id, agency, rated_on)
);
"""

# Preference order when several agencies currently rate the same issuer.
# 'manual' (the watchlist-seed placeholder agency) is deliberately lowest --
# a real agency rating should always win over the bootstrap value.
_AGENCY_PRIORITY = ["АКРА", "ACRA", "EXPERT RA", "ЭКСПЕРТ РА", "NKR", "НКР",
                    "NRA", "НРА", "MANUAL"]


def init_rating(con) -> None:
    con.executescript(RATING_SCHEMA)
    con.commit()


def upsert_rating(con, issuer_id, agency, rating, outlook, action, rated_on,
                   source="manual", secid=None) -> None:
    con.execute(
        "UPDATE rating SET is_current=0 WHERE issuer_id=? AND agency=? AND rated_on<>?",
        (issuer_id, agency, rated_on))
    con.execute(
        "INSERT INTO rating(issuer_id,secid,agency,rating,outlook,action,"
        "rated_on,source,is_current) VALUES(?,?,?,?,?,?,?,?,1) "
        "ON CONFLICT(issuer_id,agency,rated_on) DO UPDATE SET "
        "secid=excluded.secid, rating=excluded.rating, outlook=excluded.outlook,"
        "action=excluded.action, source=excluded.source, is_current=1",
        (issuer_id, secid, agency, rating, outlook, action, rated_on, source))


def current_rating(con, issuer_id: str) -> str | None:
    """Most recent rating string (ruXX format) for an issuer across all
    agencies, preferring АКРА > Expert RA > НКР > НРА > manual on a tie."""
    rows = con.execute(
        "SELECT agency, rating, rated_on FROM rating "
        "WHERE issuer_id=? AND is_current=1", (issuer_id,)).fetchall()
    if not rows:
        return None

    def sort_key(r):
        agency_upper = (r["agency"] or "").strip().upper()
        try:
            prio = _AGENCY_PRIORITY.index(agency_upper)
        except ValueError:
            prio = len(_AGENCY_PRIORITY)
        return (r["rated_on"], -prio)

    best = max(rows, key=sort_key)
    return best["rating"]


def rating_history(con, issuer_id: str) -> list:
    return con.execute(
        "SELECT * FROM rating WHERE issuer_id=? ORDER BY rated_on DESC",
        (issuer_id,)).fetchall()


# --- Universe screener: the full MOEX corporate-bond universe, not just the
# watchlist (see universe_pipeline.py) -------------------------------------

UNIVERSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS universe_snapshot (
    secid          TEXT NOT NULL,
    snap_date      TEXT NOT NULL,
    short_name     TEXT,
    emitent_inn    TEXT,
    emitent_title  TEXT,
    mat_date       TEXT,
    offer_date     TEXT,
    coupon_pct     REAL,
    list_level     INTEGER,
    qualified      INTEGER,
    last_pct       REAL,
    yield_moex     REAL,
    yield_to_offer REAL,
    accrued_int    REAL,
    volume         REAL,
    num_trades     INTEGER,
    duration_moex  REAL,
    ytm            REAL,
    tenor_yrs      REAL,
    rf_rate        REAL,
    g_spread_bp    REAL,
    yield_source   TEXT,
    rating         TEXT,
    implied_fair_spread_bp REAL,
    mispricing_bp  REAL,
    in_watchlist   INTEGER DEFAULT 0,
    signal_excluded INTEGER DEFAULT 0,
    mod_dur        REAL,
    aey_base       REAL,
    aey_stress     REAL,
    aey_bull       REAL,
    aey_base_risk_adj REAL,
    PRIMARY KEY (secid, snap_date)
);
"""


def init_universe(con) -> None:
    con.executescript(UNIVERSE_SCHEMA)
    # signal_excluded added after the table's initial ship; retrofit onto any
    # universe_snapshot created before that (see universe_pipeline.py's
    # COMMODITY_LINKED / SHORT_TENOR_THIN exclusions).
    cols = {r["name"] for r in con.execute("PRAGMA table_info(universe_snapshot)")}
    if "signal_excluded" not in cols:
        con.execute(
            "ALTER TABLE universe_snapshot ADD COLUMN signal_excluded INTEGER DEFAULT 0")
    # mod_dur/aey_* added for the AEY scenario overlay (see portfolio.py) --
    # same retrofit pattern.
    for col in ("mod_dur", "aey_base", "aey_stress", "aey_bull", "aey_base_risk_adj"):
        if col not in cols:
            con.execute(f"ALTER TABLE universe_snapshot ADD COLUMN {col} REAL")
    con.commit()


def insert_universe_snapshot(con, **f) -> None:
    cols = ("secid,snap_date,short_name,emitent_inn,emitent_title,mat_date,"
            "offer_date,coupon_pct,list_level,qualified,last_pct,yield_moex,"
            "yield_to_offer,accrued_int,volume,num_trades,duration_moex,ytm,"
            "tenor_yrs,rf_rate,g_spread_bp,yield_source,rating,"
            "implied_fair_spread_bp,mispricing_bp,in_watchlist,signal_excluded,"
            "mod_dur,aey_base,aey_stress,aey_bull,aey_base_risk_adj")
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    con.execute(
        f"INSERT OR REPLACE INTO universe_snapshot({cols}) VALUES({placeholders})",
        f)


def latest_universe_snap_date(con) -> str | None:
    row = con.execute(
        "SELECT snap_date FROM universe_snapshot ORDER BY snap_date DESC LIMIT 1"
    ).fetchone()
    return row["snap_date"] if row else None


def universe_snapshot_rows(con, snap_date: str) -> list:
    return con.execute(
        "SELECT * FROM universe_snapshot WHERE snap_date=? ORDER BY g_spread_bp DESC",
        (snap_date,)).fetchall()


def universe_traded_recently(con, secid: str, before_date: str, since_date: str) -> bool:
    """True if `secid` had a universe_snapshot row with num_trades>=1 on any
    day in [since_date, before_date) -- the '5 day' liquidity fallback that
    can only work once universe_snapshot has accumulated history; on the
    first-ever run this always returns False and screening falls back to
    today's num_trades alone (see universe_pipeline.py's module docstring)."""
    row = con.execute(
        "SELECT 1 FROM universe_snapshot WHERE secid=? AND snap_date>=? "
        "AND snap_date<? AND num_trades>=1 LIMIT 1",
        (secid, since_date, before_date)).fetchone()
    return row is not None


EMITENT_MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS emitent_map (
    secid         TEXT PRIMARY KEY,
    emitent_inn   TEXT,
    emitent_title TEXT,
    fetched_on    TEXT
);
"""


def init_emitent_map(con) -> None:
    con.executescript(EMITENT_MAP_SCHEMA)
    con.commit()


def replace_emitent_map(con, rows: dict[str, dict], fetched_on: str) -> None:
    """Overwrite the whole cache with a freshly paginated fetch."""
    con.execute("DELETE FROM emitent_map")
    con.executemany(
        "INSERT INTO emitent_map(secid, emitent_inn, emitent_title, fetched_on) "
        "VALUES(?,?,?,?)",
        [(secid, v.get("emitent_inn"), v.get("emitent_title"), fetched_on)
         for secid, v in rows.items()],
    )
    con.commit()


def get_emitent_map(con) -> dict[str, dict]:
    rows = con.execute("SELECT secid, emitent_inn, emitent_title FROM emitent_map").fetchall()
    return {r["secid"]: {"emitent_inn": r["emitent_inn"], "emitent_title": r["emitent_title"]}
            for r in rows}


def emitent_map_fetched_on(con) -> str | None:
    row = con.execute("SELECT fetched_on FROM emitent_map LIMIT 1").fetchone()
    return row["fetched_on"] if row else None


CBR_MEETING_SCHEMA = """
CREATE TABLE IF NOT EXISTS cbr_meeting (
    meeting_date    TEXT PRIMARY KEY,
    is_anchor       INTEGER,
    rate_before     REAL,
    rate_after      REAL,
    rate_change_bp  INTEGER,
    rate_2026_mid   TEXT,
    rate_2027_mid   TEXT,
    rate_2028_mid   TEXT,
    rate_2029_mid   TEXT,
    rate_neutral    TEXT,
    inflation_forecast_2026 TEXT,
    press_release_text TEXT,
    nabiullina_speech_text TEXT,
    forecast_summary TEXT,
    resume_text     TEXT,
    comment_text    TEXT,
    fetched_on      TEXT,
    analysis_json   TEXT
);
"""


def init_cbr(con) -> None:
    con.executescript(CBR_MEETING_SCHEMA)
    # rate_2028_mid/rate_2029_mid added after the table's initial ship (the
    # AEY scenario layer needs a further-out forecast point for the bull
    # case) -- retrofit onto any cbr_meeting created before that, same
    # pattern as init_universe's signal_excluded retrofit.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(cbr_meeting)")}
    if "rate_2028_mid" not in cols:
        con.execute("ALTER TABLE cbr_meeting ADD COLUMN rate_2028_mid TEXT")
    if "rate_2029_mid" not in cols:
        con.execute("ALTER TABLE cbr_meeting ADD COLUMN rate_2029_mid TEXT")
    con.commit()


def upsert_cbr_meeting(con, **f) -> None:
    """INSERT OR REPLACE, but only over columns actually passed -- callers
    fetch documents incrementally (press release same-day, резюме two weeks
    later), so a later partial call must not clobber fields an earlier call
    already populated with NULL. Reads the existing row first and merges."""
    existing = con.execute(
        "SELECT * FROM cbr_meeting WHERE meeting_date=?", (f["meeting_date"],)
    ).fetchone()
    merged = dict(existing) if existing else {}
    merged.update({k: v for k, v in f.items() if v is not None})
    cols = ("meeting_date,is_anchor,rate_before,rate_after,rate_change_bp,"
            "rate_2026_mid,rate_2027_mid,rate_2028_mid,rate_2029_mid,"
            "rate_neutral,inflation_forecast_2026,"
            "press_release_text,nabiullina_speech_text,forecast_summary,"
            "resume_text,comment_text,fetched_on,analysis_json")
    values = {c: merged.get(c) for c in cols.split(",")}
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    con.execute(
        f"INSERT OR REPLACE INTO cbr_meeting({cols}) VALUES({placeholders})",
        values)
    con.commit()


def get_cbr_meeting(con, meeting_date: str):
    return con.execute(
        "SELECT * FROM cbr_meeting WHERE meeting_date=?", (meeting_date,)).fetchone()


def recent_cbr_meetings(con, limit: int = 3):
    return con.execute(
        "SELECT * FROM cbr_meeting WHERE press_release_text IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT ?", (limit,)).fetchall()


CBR_MEETING_DIFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS cbr_meeting_diff (
    meeting_date        TEXT PRIMARY KEY REFERENCES cbr_meeting(meeting_date),
    prior_meeting_date  TEXT REFERENCES cbr_meeting(meeting_date),
    rate_change_bp      INTEGER,
    pace_change         TEXT,
    forecast_2026_shift_bp  INTEGER,
    forecast_2027_shift_bp  INTEGER,
    tone_shift          TEXT,
    new_risk_factors    TEXT,
    removed_risk_factors TEXT,
    portfolio_signal    TEXT,
    signal_magnitude    TEXT,
    diff_note_ru        TEXT,
    computed_on         TEXT
);
"""


def init_cbr_diff(con) -> None:
    con.executescript(CBR_MEETING_DIFF_SCHEMA)
    con.commit()


def upsert_cbr_meeting_diff(con, **f) -> None:
    cols = ("meeting_date,prior_meeting_date,rate_change_bp,pace_change,"
            "forecast_2026_shift_bp,forecast_2027_shift_bp,tone_shift,"
            "new_risk_factors,removed_risk_factors,portfolio_signal,"
            "signal_magnitude,diff_note_ru,computed_on")
    values = {c: f.get(c) for c in cols.split(",")}
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    con.execute(
        f"INSERT OR REPLACE INTO cbr_meeting_diff({cols}) VALUES({placeholders})",
        values)
    con.commit()


def get_cbr_meeting_diff(con, meeting_date: str):
    return con.execute(
        "SELECT * FROM cbr_meeting_diff WHERE meeting_date=?", (meeting_date,)).fetchone()
