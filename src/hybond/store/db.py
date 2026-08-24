"""SQLite persistence. Append-only daily snapshots so the diff engine (Slice 2)
has history. No LLM, no computation beyond storage.

Snapshot discipline: price_snapshot and spread_snapshot carry a `snap_date`;
we never UPDATE a prior day, only INSERT. Static descriptors (bond, issuer) are
upserted.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ..classify_utils import queue_bucket

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
    updated     TEXT,
    issue_size          REAL,          -- MOEX ISSUESIZE: registered issue size
    issue_size_placed   REAL,          -- MOEX ISSUESIZEPLACED: actually placed so far
    placement_pct       REAL,          -- issue_size_placed / issue_size * 100
    placement_partial_since TEXT       -- ISO date this bond was FIRST observed
                                        -- not-yet-fully-placed (placement_pct
                                        -- < ~100%); cleared once it reaches
                                        -- ~100%. See placement_is_stalled().
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
    # WAL lets readers (most interactive hybond commands) proceed while a
    # writer (e.g. the hourly auto-classify cron, which can hold a
    # connection open for 30-40+ minutes given real Ollama latency) is
    # mid-transaction -- the default rollback-journal mode serializes ALL
    # access, which is the actual cause of the recurring "database is
    # locked" errors hit repeatedly this session. busy_timeout makes any
    # genuine writer-vs-writer collision retry for 5s instead of failing
    # immediately. In-memory paths (tests use ":memory:") don't support WAL
    # and don't need it -- skip there rather than let the pragma no-op.
    if str(path) != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # spread_snapshot may already exist from before yield_source was added;
    # CREATE TABLE IF NOT EXISTS above won't retrofit the column onto it.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(spread_snapshot)")}
    if "yield_source" not in cols:
        con.execute(
            "ALTER TABLE spread_snapshot ADD COLUMN yield_source TEXT DEFAULT 'computed'")
    # bond may already exist from before the placement-tracking columns were
    # added; same retrofit pattern as spread_snapshot above.
    bond_cols = {r["name"] for r in con.execute("PRAGMA table_info(bond)")}
    for col, decl in (("issue_size", "REAL"), ("issue_size_placed", "REAL"),
                       ("placement_pct", "REAL"), ("placement_partial_since", "TEXT")):
        if col not in bond_cols:
            con.execute(f"ALTER TABLE bond ADD COLUMN {col} {decl}")
    # event: price-impact fields (BONDEM-layer prep) -- all nullable, existing
    # rows get NULL rather than a fabricated value. See fill_price_impact().
    event_cols = {r["name"] for r in con.execute("PRAGMA table_info(event)")}
    for col in ("price_before_bp", "price_after_3d_bp", "price_after_7d_bp",
                "price_after_30d_bp", "spread_delta_30d_bp"):
        if col not in event_cols:
            con.execute(f"ALTER TABLE event ADD COLUMN {col} REAL")
    if "price_impact_filled_on" not in event_cols:
        con.execute("ALTER TABLE event ADD COLUMN price_impact_filled_on TEXT")
    # event: Ollama auto-classification (classify_ollama.py) provenance --
    # all nullable, existing (manually-classified) rows stay NULL rather than
    # being backfilled with a fabricated 'manual' value.
    if "classified_by" not in event_cols:
        con.execute("ALTER TABLE event ADD COLUMN classified_by TEXT")
    if "confidence" not in event_cols:
        con.execute("ALTER TABLE event ADD COLUMN confidence REAL")
    if "needs_review" not in event_cols:
        con.execute("ALTER TABLE event ADD COLUMN needs_review INTEGER DEFAULT 0")
    # issuer: active flag, defaults to 1 so every existing row is treated as
    # active on retrofit -- lets an issuer be marked inactive (e.g. delisted,
    # matured out, or a defaulted name being wound down) without deleting its
    # history.
    issuer_cols = {r["name"] for r in con.execute("PRAGMA table_info(issuer)")}
    if "active" not in issuer_cols:
        con.execute("ALTER TABLE issuer ADD COLUMN active INTEGER DEFAULT 1")
    # issuer: watchlist_tier replaces the flat active flag with a full
    # lifecycle -- 1=active watchlist (full verdict card), 2=passive
    # monitoring (collapsed dashboard row, no recommendation, data
    # collection identical to tier 1). `active` is kept in sync (1 <->
    # tier=1, 0 <-> tier=2) by every write path below rather than dropped,
    # since it's cheap insurance for anything outside this codebase that
    # might still read it -- but nothing in hybond itself reads `active`
    # anymore (see universe_pipeline.py, which now reads watchlist_tier).
    issuer_cols = {r["name"] for r in con.execute("PRAGMA table_info(issuer)")}
    if "watchlist_tier" not in issuer_cols:
        con.execute("ALTER TABLE issuer ADD COLUMN watchlist_tier INTEGER NOT NULL DEFAULT 2")
        con.execute("UPDATE issuer SET watchlist_tier = 1 WHERE active = 1")
        con.execute("UPDATE issuer SET watchlist_tier = 2 WHERE active = 0 OR active IS NULL")
    # issuer: health_status/health_reason -- color-coded output of `hybond
    # scan-watchlist-health` (Watchlist Health Monitoring task, 2026-08-24;
    # revised from an earlier needs_review boolean to a 4-level
    # green/yellow/orange/red scale so a dashboard card can show severity at
    # a glance, not just "flagged or not"). Distinct from event.needs_review
    # (per-filing classification review queue, added above) -- this one is
    # issuer-level and recomputed fresh on every health-scan run, not a
    # one-time manual-review queue.
    issuer_cols = {r["name"] for r in con.execute("PRAGMA table_info(issuer)")}
    if "health_status" not in issuer_cols:
        con.execute("ALTER TABLE issuer ADD COLUMN health_status TEXT NOT NULL DEFAULT 'green'")
    if "health_reason" not in issuer_cols:
        con.execute("ALTER TABLE issuer ADD COLUMN health_reason TEXT")
    con.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_tier_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer_id TEXT NOT NULL REFERENCES issuer(issuer_id),
            from_tier INTEGER,
            to_tier INTEGER NOT NULL,
            reason TEXT,
            changed_at TEXT NOT NULL
        )
    """)
    con.commit()


def upsert_issuer(con, issuer_id: str, name: str, sector: str | None = None, notes: str | None = None):
    con.execute(
        "INSERT INTO issuer(issuer_id,name,sector,notes) VALUES(?,?,?,?) "
        "ON CONFLICT(issuer_id) DO UPDATE SET name=excluded.name, "
        "sector=excluded.sector, notes=excluded.notes",
        (issuer_id, name, sector, notes),
    )


WATCHLIST_TIERS = {1, 2}


def add_issuer_at_tier(con, issuer_id: str, name: str, watchlist_tier: int = 2,
                        sector: str | None = None, notes: str | None = None) -> None:
    """Insert a brand-new issuer row directly at a given tier -- the
    `promote --confirm` path (manual_candidate -> real issuer). Errors on a
    pre-existing issuer_id rather than silently overwriting it; use
    set_watchlist_tier() to change an existing issuer's tier instead."""
    if watchlist_tier not in WATCHLIST_TIERS:
        raise ValueError(f"watchlist_tier must be one of {sorted(WATCHLIST_TIERS)}, got {watchlist_tier!r}")
    existing = con.execute("SELECT 1 FROM issuer WHERE issuer_id=?", (issuer_id,)).fetchone()
    if existing:
        raise ValueError(f"issuer_id={issuer_id!r} already exists in issuer table")
    con.execute(
        "INSERT INTO issuer(issuer_id,name,sector,notes,active,watchlist_tier) "
        "VALUES(?,?,?,?,?,?)",
        (issuer_id, name, sector, notes, 1 if watchlist_tier == 1 else 0, watchlist_tier),
    )


def set_watchlist_tier(con, issuer_id: str, to_tier: int, reason: str | None = None,
                        changed_at: str | None = None) -> dict:
    """Move an existing issuer to a new tier, mirroring `active`, and log
    the change to watchlist_tier_log for a full promote/demote audit trail.
    Returns the log row as a dict. Raises ValueError on an unknown
    issuer_id or an invalid to_tier."""
    if to_tier not in WATCHLIST_TIERS:
        raise ValueError(f"to_tier must be one of {sorted(WATCHLIST_TIERS)}, got {to_tier!r}")
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?", (issuer_id,)).fetchone()
    if row is None:
        raise ValueError(f"no issuer row with issuer_id={issuer_id!r}")
    from_tier = row["watchlist_tier"]
    changed_at = changed_at or datetime.now().isoformat(timespec="seconds")
    con.execute("UPDATE issuer SET watchlist_tier=?, active=? WHERE issuer_id=?",
                (to_tier, 1 if to_tier == 1 else 0, issuer_id))
    cur = con.execute(
        "INSERT INTO watchlist_tier_log(issuer_id,from_tier,to_tier,reason,changed_at) "
        "VALUES(?,?,?,?,?)",
        (issuer_id, from_tier, to_tier, reason, changed_at))
    return {"id": cur.lastrowid, "issuer_id": issuer_id, "from_tier": from_tier,
            "to_tier": to_tier, "reason": reason, "changed_at": changed_at}


HEALTH_STATUSES = {"green", "yellow", "orange", "red"}


def set_issuer_health_status(con, issuer_id: str, status: str, reason: str | None = None) -> None:
    """Set issuer.health_status + health_reason -- the color-coded output of
    `hybond scan-watchlist-health`. Called fresh every scan run for every
    tier=1 issuer (including a reset back to 'green' with reason=None when
    no signal fires), so a stale reason string can never survive past a
    resolved signal."""
    if status not in HEALTH_STATUSES:
        raise ValueError(f"status must be one of {sorted(HEALTH_STATUSES)}, got {status!r}")
    con.execute(
        "UPDATE issuer SET health_status=?, health_reason=? WHERE issuer_id=?",
        (status, reason if status != "green" else None, issuer_id))


def list_issuers_by_tier(con, watchlist_tier: int | tuple[int, ...] | list[int]) -> list[dict]:
    """Accepts a single tier (existing callers, e.g. export_dashboard_data.py's
    `list_issuers_by_tier(con, 1)`) or an iterable of tiers (daily-pipeline
    callers that want tier=1 and tier=2 together, e.g. `(1, 2)`)."""
    tiers = (watchlist_tier,) if isinstance(watchlist_tier, int) else tuple(watchlist_tier)
    placeholders = ",".join("?" for _ in tiers)
    return [dict(r) for r in con.execute(
        f"SELECT issuer_id, name, sector, notes, watchlist_tier FROM issuer "
        f"WHERE watchlist_tier IN ({placeholders}) ORDER BY name", tiers)]


ISSUER_BOND_SOURCES = {"watchlist_json", "universe_snapshot", "bond", "manual"}


def init_issuer_bond(con) -> None:
    """Persistent issuer_id -> secid mapping -- the single source of truth
    the daily pipeline (pipeline.py) reads to know which bonds to fetch,
    replacing a live per-run watchlist.json['secids'] read. Needed because
    neither the `bond` table (only populated *by* a prior pipeline.run(), so
    empty for any issuer never run yet) nor `universe_snapshot` (MOEX's live
    broad screen, which drops a bond the moment it delists) is independently
    complete -- confirmed live: Гарант-Инвест's two delisted ISINs exist in
    neither table today, only in watchlist.json's static secids list. See
    Watchlist Tier System follow-up task, 2026-08-20."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS issuer_bond (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer_id TEXT NOT NULL REFERENCES issuer(issuer_id),
            secid TEXT NOT NULL,
            added_at TEXT NOT NULL,
            source TEXT NOT NULL,
            delisted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(issuer_id, secid)
        )
    """)
    con.commit()


def add_issuer_bond(con, issuer_id: str, secid: str, source: str,
                     added_at: str | None = None) -> None:
    if source not in ISSUER_BOND_SOURCES:
        raise ValueError(f"source must be one of {sorted(ISSUER_BOND_SOURCES)}, got {source!r}")
    added_at = added_at or datetime.now().isoformat(timespec="seconds")
    # ON CONFLICT DO NOTHING -- a secid keeps whichever source/added_at first
    # recorded it, and never clobbers a delisted=1 flag a later sync sets.
    con.execute(
        "INSERT INTO issuer_bond(issuer_id,secid,added_at,source) VALUES(?,?,?,?) "
        "ON CONFLICT(issuer_id,secid) DO NOTHING",
        (issuer_id, secid, added_at, source))


def sync_issuer_bond(con, watchlist: dict | None = None) -> None:
    """Reconcile issuer_bond against every currently-known source, for every
    issuer in the `issuer` table. Idempotent and cheap (no network) -- safe
    to call on every pipeline run. Priority order matters only for which
    `source` label a brand-new secid gets recorded with (first-seen wins,
    via add_issuer_bond's ON CONFLICT DO NOTHING): watchlist.json first (it's
    the only place a fully-delisted bond's ISIN may still be recorded),
    then universe_snapshot, then bond. watchlist.json is optional -- absence
    is not an error, just one fewer source to union (Step 5's graceful-
    fallback requirement)."""
    watchlist = watchlist or {}
    issuer_ids = [r["issuer_id"] for r in con.execute("SELECT issuer_id FROM issuer")]
    for issuer_id in issuer_ids:
        for secid in (watchlist.get(issuer_id, {}) or {}).get("secids", []):
            add_issuer_bond(con, issuer_id, secid, source="watchlist_json")
        for r in con.execute(
                "SELECT DISTINCT secid FROM universe_snapshot WHERE emitent_inn=?", (issuer_id,)):
            add_issuer_bond(con, issuer_id, r["secid"], source="universe_snapshot")
        for r in con.execute(
                "SELECT DISTINCT secid FROM bond WHERE issuer_id=?", (issuer_id,)):
            add_issuer_bond(con, issuer_id, r["secid"], source="bond")


def refresh_delisted_flags(con, live_secids: set[str]) -> None:
    """Called after a real MOEX fetch_bond_master() pull -- `live_secids` is
    the actual set MOEX ISS returned this run, so `delisted` reflects a real
    live check, not a guess. Symmetric: a secid can also come back off the
    delisted list if it reappears (rare, but handled for correctness)."""
    rows = [(secid,) for secid in
            {r["secid"] for r in con.execute("SELECT DISTINCT secid FROM issuer_bond")}
            if secid not in live_secids]
    if rows:
        con.executemany("UPDATE issuer_bond SET delisted=1 WHERE secid=? AND delisted=0", rows)
    reappeared = [(secid,) for secid in live_secids]
    if reappeared:
        con.executemany("UPDATE issuer_bond SET delisted=0 WHERE secid=? AND delisted=1", reappeared)


def list_bonds_for_issuer(con, issuer_id: str) -> list[str]:
    """tier=2 (passive monitoring / confirmed-default historical monitoring)
    issuers keep delisted bonds in scope -- that's the whole point of the
    Гарант-Инвест case. tier=1 issuers only get currently-listed bonds."""
    row = con.execute("SELECT watchlist_tier FROM issuer WHERE issuer_id=?",
                       (issuer_id,)).fetchone()
    if row and row["watchlist_tier"] == 2:
        q = "SELECT secid FROM issuer_bond WHERE issuer_id=?"
        params = (issuer_id,)
    else:
        q = "SELECT secid FROM issuer_bond WHERE issuer_id=? AND delisted=0"
        params = (issuer_id,)
    return [r["secid"] for r in con.execute(q, params)]


PLACEMENT_FULL_THRESHOLD_PCT = 99.5  # >= this counts as "fully placed" (odd-lot
                                      # rounding rarely lands exactly on 100.0)


def upsert_bond(con, **f):
    """`issue_size`/`issue_size_placed`/`placement_pct` are optional (default
    None) so existing callers that don't pass them keep working unchanged.

    `placement_partial_since` is NOT a caller-supplied field -- it's tracked
    here: set to `updated` the first time a bond is observed below
    PLACEMENT_FULL_THRESHOLD_PCT, held at that original date on every
    subsequent still-partial run (not bumped forward each time), and cleared
    back to NULL once placement reaches the threshold. This is what lets
    placement_is_stalled() answer "how long has this sat partially placed",
    the same style of question rating_is_stale() answers for rated_on."""
    f.setdefault("issue_size", None)
    f.setdefault("issue_size_placed", None)
    f.setdefault("placement_pct", None)
    # if THIS call doesn't carry placement data (fields omitted / MOEX
    # columns missing this run), COALESCE keeps whatever was already stored
    # rather than overwriting real data with NULL -- callers that never pass
    # these fields (e.g. existing tests) must not silently erase them.
    pct = f["placement_pct"]
    initial_partial_since = f["updated"] if (pct is not None and pct < PLACEMENT_FULL_THRESHOLD_PCT) else None
    con.execute(
        "INSERT INTO bond(secid,isin,issuer_id,short_name,mat_date,face_value,"
        "face_unit,coupon_pct,list_level,qualified,updated,"
        "issue_size,issue_size_placed,placement_pct,placement_partial_since) "
        "VALUES(:secid,:isin,:issuer_id,:short_name,:mat_date,:face_value,"
        ":face_unit,:coupon_pct,:list_level,:qualified,:updated,"
        ":issue_size,:issue_size_placed,:placement_pct,:initial_partial_since) "
        "ON CONFLICT(secid) DO UPDATE SET isin=excluded.isin, "
        "issuer_id=excluded.issuer_id, short_name=excluded.short_name, "
        "mat_date=excluded.mat_date, face_value=excluded.face_value, "
        "face_unit=excluded.face_unit, coupon_pct=excluded.coupon_pct, "
        "list_level=excluded.list_level, qualified=excluded.qualified, "
        "updated=excluded.updated, "
        "issue_size=COALESCE(excluded.issue_size, bond.issue_size), "
        "issue_size_placed=COALESCE(excluded.issue_size_placed, bond.issue_size_placed), "
        "placement_pct=COALESCE(excluded.placement_pct, bond.placement_pct), "
        "placement_partial_since = CASE "
        "  WHEN excluded.placement_pct IS NULL THEN bond.placement_partial_since "
        f"  WHEN excluded.placement_pct >= {PLACEMENT_FULL_THRESHOLD_PCT} THEN NULL "
        "  WHEN bond.placement_partial_since IS NOT NULL THEN bond.placement_partial_since "
        "  ELSE excluded.updated "
        "END",
        {**f, "initial_partial_since": initial_partial_since},
    )


PLACEMENT_STALL_WEEKS = 4            # "extended period without progressing"
PLACEMENT_STALL_THRESHOLD_PCT = 50.0  # "low placement percentage"


def placement_is_stalled(placement_pct: float | None, partial_since: str | None,
                          as_of: date | None = None,
                          weeks: int = PLACEMENT_STALL_WEEKS,
                          threshold_pct: float = PLACEMENT_STALL_THRESHOLD_PCT) -> bool:
    """True when a bond has been sitting below `threshold_pct` placed for at
    least `weeks` since it was first observed partially placed
    (placement_partial_since, tracked by upsert_bond). Mirrors
    rating_is_stale()'s shape -- a simple elapsed-time-since-a-tracked-date
    check, not a trend/velocity model. First cut per the task's own framing
    ("otherwise just surface the raw percentage... revisit staleness logic
    later") -- doesn't distinguish "climbing slowly" from "flat at 12% for
    two months", both currently read as stalled once past the threshold."""
    if placement_pct is None or partial_since is None:
        return False
    if placement_pct >= threshold_pct:
        return False
    as_of = as_of or date.today()
    since = datetime.strptime(partial_since, "%Y-%m-%d").date()
    return (as_of - since).days >= weeks * 7


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


# --- Part B: survival-weighted expected-return projections (compute/
# expected_return.py). Computed once per watchlist bond during pipeline.py's
# `hybond run` (where the real coupon/amortization schedule + dirty price are
# already in hand from the YTM/duration calc, so this is free to compute
# alongside them, not a second fetch) and persisted here so
# export_dashboard_data.py can read them offline -- same division of labour
# as spread_snapshot (computed once, read many times), and it keeps the
# dashboard export script from needing a live MOEX call of its own.

EXPECTED_RETURN_SCHEMA = """
CREATE TABLE IF NOT EXISTS expected_return_snapshot (
    secid           TEXT REFERENCES bond(secid),
    snap_date       TEXT NOT NULL,
    horizon_label   TEXT NOT NULL,      -- '1y' | '2y' | 'maturity'
    horizon_yr      REAL,               -- actual horizon used (may be capped)
    irr_pct         REAL,
    undiscounted_npv REAL,
    undiscounted_total_cashflow REAL,
    horizon_capped_by_maturity INTEGER, -- 0/1
    uses_extrapolated_pd INTEGER,       -- 0/1 -- see default_probs.survival_probability
    PRIMARY KEY (secid, snap_date, horizon_label)
);
"""


def init_expected_return(con: sqlite3.Connection) -> None:
    con.executescript(EXPECTED_RETURN_SCHEMA)
    con.commit()


def insert_expected_return_snapshot(con, **f):
    con.execute(
        "INSERT OR REPLACE INTO expected_return_snapshot(secid,snap_date,"
        "horizon_label,horizon_yr,irr_pct,undiscounted_npv,"
        "undiscounted_total_cashflow,horizon_capped_by_maturity,"
        "uses_extrapolated_pd) VALUES(:secid,:snap_date,:horizon_label,"
        ":horizon_yr,:irr_pct,:undiscounted_npv,:undiscounted_total_cashflow,"
        ":horizon_capped_by_maturity,:uses_extrapolated_pd)",
        f,
    )


def latest_expected_returns(con, secid: str, on_date: str) -> list:
    return con.execute(
        "SELECT horizon_label, horizon_yr, irr_pct, undiscounted_npv, "
        "undiscounted_total_cashflow, horizon_capped_by_maturity, "
        "uses_extrapolated_pd FROM expected_return_snapshot "
        "WHERE secid=? AND snap_date=?",
        (secid, on_date),
    ).fetchall()


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


def skip_noise_filings(con, rows) -> int:
    """Bulk-classify already-identified noise filings (see
    classify_utils.is_likely_noise) without invoking hy-event-classify --
    same event-table write scripts/bulk_mark_routine.py already makes for a
    routine/noise filing (category='routine', severity='noise'), just
    triggered by a keyword-pattern match instead of an exact-title rule.
    `rows` are filing rows (sqlite3.Row, from unclassified_filings()).
    `filing`/`event` have no confidence column (only market_note does), so
    the 0.95 confidence the calling task named isn't stored here -- there's
    nowhere honest to put it."""
    for r in rows:
        insert_event(
            con, issuer_id=r["issuer_id"],
            occurred=r["published"] or r["fetched_on"],
            category="routine", severity="noise",
            summary=r["title"], source_url=r["url"],
        )
        mark_classified(con, r["body_hash"])
    con.commit()
    return len(rows)


# --- market_note: secondary business-news/forum signal (e.g. Smart-Lab's
# general news feed) -- deliberately a SEPARATE table from `event`, which is
# reserved for classified AZIPI/primary material facts. severity/category
# reuse hy-event-classify's taxonomy scale (critical/high/medium/low/noise)
# but are left NULL until classification is wired for this source (a later
# step, not this one -- see scrape/smartlab_news.py). ------------------------

MARKET_NOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_note (
    note_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,      -- e.g. 'smartlab_news'
    issuer_id     TEXT REFERENCES issuer(issuer_id),
    occurred      TEXT,               -- publication date/time
    headline      TEXT,
    summary       TEXT,
    url           TEXT,               -- link back to origin, required
    severity      TEXT,               -- critical/high/medium/low/noise, or NULL
    category      TEXT,               -- hy-event-classify taxonomy category, or NULL
    confidence    REAL,
    fetched_on    TEXT,
    body_hash     TEXT UNIQUE         -- dedup key, sha1(title+url), same convention as `filing`
);
"""


def init_market_note(con) -> None:
    con.executescript(MARKET_NOTE_SCHEMA)
    # MOEX Listing/CBR Press RSS task (2026-08-17): feed_url tracks which RSS
    # feed a row came from (multiple feeds can share the same `source`
    # value... actually each source maps 1:1 to a feed here, but the column
    # exists so a future multi-feed source doesn't need another migration).
    # Same retrofit pattern init_db() already uses for spread_snapshot/bond/
    # event -- PRAGMA table_info + conditional ALTER TABLE, not a bare
    # try/except (grepped before writing; no try/except-swallow migration
    # exists anywhere in this file). Existing rows get NULL, not backfilled.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(market_note)")}
    if "feed_url" not in cols:
        con.execute("ALTER TABLE market_note ADD COLUMN feed_url TEXT")
    con.commit()


def insert_market_note(con, **f) -> bool:
    """Returns True if a new row was actually inserted, False if body_hash
    already existed (dedup) -- callers use this to report a 'new' count.
    feed_url defaults to None (same optional-kwarg pattern as
    insert_spread_snapshot's yield_source) so existing callers that don't
    pass it keep working unchanged."""
    f.setdefault("feed_url", None)
    cols = ("source,issuer_id,occurred,headline,summary,url,severity,"
            "category,confidence,fetched_on,body_hash,feed_url")
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    cur = con.execute(
        f"INSERT OR IGNORE INTO market_note({cols}) VALUES({placeholders})", f)
    return cur.rowcount > 0


def seen_market_note_hashes(con) -> set:
    return {r["body_hash"] for r in con.execute("SELECT body_hash FROM market_note")}


def unclassified_market_notes(con, issuer_id: str | None = None) -> list:
    q = "SELECT * FROM market_note WHERE severity IS NULL"
    args: tuple = ()
    if issuer_id:
        q += " AND issuer_id=?"
        args = (issuer_id,)
    return con.execute(q + " ORDER BY occurred DESC", args).fetchall()


def set_market_note_classification(
    con, note_id: int, severity: str, category: str, confidence: float,
) -> None:
    con.execute(
        "UPDATE market_note SET severity=?, category=?, confidence=? WHERE note_id=?",
        (severity, category, confidence, note_id))


def insert_event(con, **f) -> int:
    """classified_by/confidence/needs_review are optional (default None/None/0)
    -- existing callers (manual classification, skip_noise_filings()) that
    don't pass them keep working unchanged, same optional-kwarg pattern as
    upsert_bond's issue_size fields."""
    f.setdefault("classified_by", None)
    f.setdefault("confidence", None)
    f.setdefault("needs_review", 0)
    cur = con.execute(
        "INSERT INTO event(issuer_id,occurred,category,severity,summary,source_url,"
        "classified_by,confidence,needs_review) "
        "VALUES(:issuer_id,:occurred,:category,:severity,:summary,:source_url,"
        ":classified_by,:confidence,:needs_review)", f)
    return cur.lastrowid


# --- Ollama auto-classification (classify_ollama.py / `hybond auto-classify`) --

def get_auto_classify_queue(con, limit: int | None = None, priority_only: bool = False) -> list[dict]:
    """Unclassified filings, joined to issuer for issuer_name, ordered
    tier=1 (active watchlist) before tier=2 before everything else, then
    by content priority (via classify_utils.queue_bucket -- same substring
    patterns the manual `hybond queue` display already uses), then newest
    first. Noise-bucketed filings are NOT excluded here -- they still need
    classify_filing() to run so a real (low-severity) event gets recorded,
    same as manual classification would; queue_bucket is only used for
    ORDERING, not filtering, except when priority_only is set."""
    rows = con.execute(
        "SELECT f.body_hash, f.issuer_id, i.name AS issuer_name, f.title, f.url, "
        "f.published, i.watchlist_tier FROM filing f JOIN issuer i ON i.issuer_id = f.issuer_id "
        "WHERE f.classified = 0 ORDER BY f.published DESC"
    ).fetchall()
    buckets = [(r, queue_bucket(r["title"])) for r in rows]
    if priority_only:
        buckets = [(r, b) for r, b in buckets if b == "priority"]
    else:
        bucket_order = {"priority": 0, "normal": 1, "noise": 2}
        # tier=1 first (0), everything else after (1) -- tier is NOT NULL so
        # this never needs a None case (see issuer.watchlist_tier's schema).
        buckets.sort(key=lambda rb: (0 if rb[0]["watchlist_tier"] == 1 else 1,
                                      bucket_order[rb[1]]))
    out = [dict(r) for r, _ in buckets]
    return out[:limit] if limit is not None else out


def get_auto_classify_market_notes(con, limit: int | None = None, priority_only: bool = False) -> list[dict]:
    """Same queue shape as get_auto_classify_queue() but for market_note --
    unclassified marker is `severity IS NULL` (not a `classified` flag column,
    which market_note doesn't have), and the date field is `occurred` (not
    `published`). Field names in the returned dicts are normalised to match
    get_auto_classify_queue()'s (title/url/published/issuer_name) so the CLI
    loop can treat both queues uniformly; body_hash is replaced by note_id
    since market_note's write-back path (set_market_note_classification) is
    keyed by note_id, not body_hash."""
    rows = con.execute(
        "SELECT n.note_id, n.issuer_id, i.name AS issuer_name, n.headline AS title, "
        "n.url, n.occurred AS published FROM market_note n "
        "LEFT JOIN issuer i ON i.issuer_id = n.issuer_id "
        "WHERE n.severity IS NULL ORDER BY n.occurred DESC"
    ).fetchall()
    buckets = [(r, queue_bucket(r["title"], source=None)) for r in rows]
    if priority_only:
        buckets = [(r, b) for r, b in buckets if b == "priority"]
    else:
        order = {"priority": 0, "normal": 1, "noise": 2}
        buckets.sort(key=lambda rb: order[rb[1]])
    out = [dict(r) for r, _ in buckets]
    return out[:limit] if limit is not None else out


def events_needing_review(con, limit: int | None = None) -> list:
    q = ("SELECT e.*, i.name AS issuer_name FROM event e "
         "JOIN issuer i ON i.issuer_id = e.issuer_id "
         "WHERE e.needs_review = 1 ORDER BY e.occurred DESC")
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    return con.execute(q).fetchall()


def resolve_event_review(con, event_id: int, category: str | None = None,
                          severity: str | None = None) -> None:
    """`hybond review-queue`'s [a]ccept/[e]dit actions: always clears
    needs_review; category/severity are only updated when the operator
    actually edited them (edit action), left untouched on plain accept."""
    if category is not None or severity is not None:
        sets, args = [], []
        if category is not None:
            sets.append("category=?"); args.append(category)
        if severity is not None:
            sets.append("severity=?"); args.append(severity)
        args.append(event_id)
        con.execute(f"UPDATE event SET {', '.join(sets)}, needs_review=0 WHERE event_id=?", args)
    else:
        con.execute("UPDATE event SET needs_review=0 WHERE event_id=?", (event_id,))


def events_since(con, issuer_id: str, since_date: str) -> list:
    return con.execute(
        "SELECT * FROM event WHERE issuer_id=? AND occurred>=? ORDER BY occurred DESC",
        (issuer_id, since_date)).fetchall()


def _price_impact_ref_bond(con, issuer_id: str) -> str | None:
    """The issuer's price-impact reference bond: highest AVERAGE daily
    volume across all its price_snapshot history (not a single day's
    volume) -- same "most liquid bond represents the issuer" principle
    promote.py's VDO_MIN_VOLUME liquidity gate already applies, just picking
    the best bond instead of gating a threshold. None if the issuer has no
    bond with any price_snapshot history at all."""
    row = con.execute(
        "SELECT b.secid FROM bond b JOIN price_snapshot ps ON ps.secid = b.secid "
        "WHERE b.issuer_id = ? GROUP BY b.secid ORDER BY AVG(ps.volume) DESC LIMIT 1",
        (issuer_id,)).fetchone()
    return row["secid"] if row else None


def _spread_before(con, secid: str, occurred: str) -> float | None:
    """Nearest spread_snapshot STRICTLY before occurred -- "G-spread day
    before event_date" (the calling task's own field comment), not the
    event day's own price."""
    row = con.execute(
        "SELECT g_spread_bp FROM spread_snapshot WHERE secid=? AND snap_date<? "
        "ORDER BY snap_date DESC LIMIT 1", (secid, occurred)).fetchone()
    return row["g_spread_bp"] if row else None


def _spread_after_trading_days(con, secid: str, occurred: str, n: int) -> float | None:
    """The Nth spread_snapshot row strictly after `occurred` for this bond --
    a trading-day count (however many calendar days that spans), not a fixed
    calendar offset, since spread_snapshot only has rows for days the
    pipeline actually ran (see daily_run.sh's weekday cron)."""
    row = con.execute(
        "SELECT g_spread_bp FROM spread_snapshot WHERE secid=? AND snap_date>? "
        "ORDER BY snap_date ASC LIMIT 1 OFFSET ?", (secid, occurred, n - 1)).fetchone()
    return row["g_spread_bp"] if row else None


def _spread_after_calendar_days(con, secid: str, occurred: str, n: int) -> float | None:
    """Nearest spread_snapshot on/after occurred+n CALENDAR days -- for the
    30d field, which the calling task specifies as calendar days, unlike
    the 3d/7d trading-day fields above."""
    target = (datetime.fromisoformat(occurred) + timedelta(days=n)).date().isoformat()
    row = con.execute(
        "SELECT g_spread_bp FROM spread_snapshot WHERE secid=? AND snap_date>=? "
        "ORDER BY snap_date ASC LIMIT 1", (secid, target)).fetchone()
    return row["g_spread_bp"] if row else None


def fill_price_impact(con) -> dict:
    """Backfills event.price_before_bp / price_after_{3d,7d,30d}_bp /
    spread_delta_30d_bp for every event row that doesn't have
    price_before_bp yet, using each issuer's price-impact reference bond
    (see _price_impact_ref_bond). Never touches a row that already has
    price_before_bp filled -- this only fills gaps, never revises a prior
    fill. Returns {"updated": n, "still_null": n, "total_checked": n};
    still_null is EXPECTED for events too recent for spread_snapshot to
    have caught up to yet, or for issuers with no priced bond at all --
    not an error.
    """
    events = con.execute(
        "SELECT event_id, issuer_id, occurred FROM event "
        "WHERE price_before_bp IS NULL AND occurred IS NOT NULL").fetchall()
    updated = 0
    still_null = 0
    today = date.today().isoformat()
    ref_bond_cache: dict[str, str | None] = {}
    for ev in events:
        issuer_id, occurred = ev["issuer_id"], ev["occurred"]
        if issuer_id not in ref_bond_cache:
            ref_bond_cache[issuer_id] = _price_impact_ref_bond(con, issuer_id)
        secid = ref_bond_cache[issuer_id]
        if not secid:
            still_null += 1
            continue

        before = _spread_before(con, secid, occurred)
        after_3d = _spread_after_trading_days(con, secid, occurred, 3)
        after_7d = _spread_after_trading_days(con, secid, occurred, 7)
        after_30d = _spread_after_calendar_days(con, secid, occurred, 30)
        if before is None and after_3d is None and after_7d is None and after_30d is None:
            still_null += 1
            continue
        delta_30d = (after_30d - before) if (after_30d is not None and before is not None) else None

        con.execute(
            "UPDATE event SET price_before_bp=?, price_after_3d_bp=?, price_after_7d_bp=?, "
            "price_after_30d_bp=?, spread_delta_30d_bp=?, price_impact_filled_on=? "
            "WHERE event_id=?",
            (before, after_3d, after_7d, after_30d, delta_30d, today, ev["event_id"]))
        updated += 1
    con.commit()
    return {"updated": updated, "still_null": still_null, "total_checked": len(events)}


def market_notes_since(con, issuer_id: str, since_date: str) -> list:
    """Classified (severity IS NOT NULL) market_note rows in the window --
    mirrors events_since. Rows still awaiting classification (severity NULL,
    see hy-event-classify's market_note input path) are excluded, same as an
    unclassified filing never appears in events_since's `event` rows."""
    return con.execute(
        "SELECT * FROM market_note WHERE issuer_id=? AND occurred>=? "
        "AND severity IS NOT NULL ORDER BY occurred DESC",
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
    PRIMARY KEY (issuer_id, period, computed_on)
);
"""


def init_pillars(con) -> None:
    con.executescript(PILLARS_SCHEMA)
    # Original PK was (issuer_id, period) -- period is the fiscal reporting
    # year (e.g. "2025"), not the fetch date, so a weekly re-fetch that lands
    # on the same still-current fiscal year silently OVERWROTE the prior row
    # instead of adding a new one. That meant no issuer could ever accumulate
    # real pillars history no matter how many times the cron ran. Widening
    # the PK to include computed_on lets each fetch date persist as its own
    # row (same-day reruns are still idempotent-replace). SQLite can't ALTER
    # a PRIMARY KEY in place, so rebuild the table when the old PK is found.
    pk_cols = {r["name"]: r["pk"] for r in con.execute("PRAGMA table_info(pillars_snapshot)")}
    if pk_cols and pk_cols.get("computed_on", 0) == 0:
        con.executescript("""
            ALTER TABLE pillars_snapshot RENAME TO pillars_snapshot_old;
            CREATE TABLE pillars_snapshot (
                issuer_id   TEXT REFERENCES issuer(issuer_id),
                period      TEXT,
                computed_on TEXT,
                payload     TEXT,
                PRIMARY KEY (issuer_id, period, computed_on)
            );
            INSERT INTO pillars_snapshot SELECT * FROM pillars_snapshot_old;
            DROP TABLE pillars_snapshot_old;
        """)
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
        "ORDER BY period DESC, computed_on DESC LIMIT 1", (issuer_id,)).fetchone()


def pillars_history(con, issuer_id: str, limit: int = 10) -> list:
    """Ordered newest-first snapshots for one issuer -- now that the PK
    includes computed_on (see init_pillars), this actually accumulates one
    row per fetch date instead of one per fiscal year."""
    return con.execute(
        "SELECT * FROM pillars_snapshot WHERE issuer_id=? "
        "ORDER BY computed_on DESC, period DESC LIMIT ?",
        (issuer_id, limit)).fetchall()


def mispricing_history(con, secid: str, limit: int = 10) -> list:
    """Ordered newest-first per-bond mispricing_bp across universe_snapshot's
    existing daily rows (secid, snap_date) -- no new storage needed, this
    table already accumulates one row per day."""
    return con.execute(
        "SELECT snap_date, mispricing_bp FROM universe_snapshot "
        "WHERE secid=? AND mispricing_bp IS NOT NULL "
        "ORDER BY snap_date DESC LIMIT ?",
        (secid, limit)).fetchall()


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

# Staleness convention: confirming a rating via a fresh web search means "we
# checked, and this is the real agency's most recent action" -- it does NOT
# mean that action is recent. A rating whose own rated_on is old (e.g. an
# agency hasn't re-confirmed it in over a year) is still the best data we
# have, but it deserves another look later, not a clean bill of health.
# Conflating the two (every verification pass before this one set
# needs_verification=0 unconditionally on confirmation) hides genuinely
# aging ratings from the needs_verification queue. 12 months is the default
# threshold -- Russian rating agencies typically re-confirm/act on an issuer
# at least annually, so anything older than that is overdue for a fresh
# agency check regardless of how recently WE looked it up.
RATING_STALENESS_MONTHS = 12


def _add_months(d: date, months: int) -> date:
    import calendar
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def rating_is_stale(rated_on: str, as_of: date | None = None,
                     months: int = RATING_STALENESS_MONTHS) -> bool:
    """True when the agency's own action date (`rated_on`, ISO string) is
    more than `months` months before `as_of` (default: today)."""
    as_of = as_of or date.today()
    rated = datetime.strptime(rated_on, "%Y-%m-%d").date()
    return as_of > _add_months(rated, months)


def verification_flags(rated_on: str, as_of: date | None = None) -> tuple[str, int]:
    """(verified_on, needs_verification) for a rating just confirmed via a
    fresh check. verified_on is always stamped `as_of` (we DID just check
    it) -- but needs_verification flips back to 1 if the underlying
    rated_on is stale, so "confirmed but aging" isn't recorded the same way
    as "confirmed and current". Callers applying a web-search-verified
    rating should use this instead of hand-setting needs_verification=0."""
    as_of = as_of or date.today()
    return as_of.isoformat(), (1 if rating_is_stale(rated_on, as_of) else 0)


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
    verified_on  TEXT,
    needs_verification INTEGER DEFAULT 1,
    PRIMARY KEY (issuer_id, agency, rated_on)
);
"""

# Preference order when several agencies currently rate the same issuer.
# 'manual' (the watchlist-seed placeholder agency) is deliberately lowest --
# a real agency rating should always win over the bootstrap value.
#
# CAVEAT found 2026-07-30: current_rating()'s sort_key below compares
# (rated_on, -priority) as a tuple, so rated_on is compared FIRST -- this
# priority list only breaks a TIE on equal dates. It does NOT make a real
# agency always win over 'manual' as this comment claims; a manual row with
# a later rated_on still beats an earlier real-agency row. This hasn't bitten
# in production yet (no issuer currently carries both a live manual row and a
# live real-agency row at once -- confirmed by inspection 2026-07-30), but
# it's a latent bug, not fixed here (out of scope for the АФК Система
# correction, which sidesteps it by explicitly retiring the old manual row
# instead of relying on this tiebreak).
_AGENCY_PRIORITY = ["АКРА", "ACRA", "EXPERT RA", "ЭКСПЕРТ РА", "NKR", "НКР",
                    "NRA", "НРА", "MANUAL"]


def init_rating(con) -> None:
    con.executescript(RATING_SCHEMA)
    # verified_on/needs_verification added after the table's initial ship --
    # verified_on records when a rating was actually cross-checked against
    # its real agency (independent of rated_on, which is the agency's own
    # action date); needs_verification flags rows that haven't been. Same
    # migration + explicit backfill pattern as init_universe/init_cbr.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(rating)")}
    is_fresh_table = "verified_on" not in cols
    if "verified_on" not in cols:
        con.execute("ALTER TABLE rating ADD COLUMN verified_on TEXT")
    if "needs_verification" not in cols:
        con.execute("ALTER TABLE rating ADD COLUMN needs_verification INTEGER DEFAULT 1")
    if is_fresh_table:
        # Backfill existing rows: the 'manual' bootstrap batch (seed_ratings.py,
        # copied verbatim from watchlist.json with no per-issuer agency
        # verification) needs auditing; rows already carrying a real,
        # dated agency action (via `hybond rate`) don't need this flag.
        con.execute("UPDATE rating SET needs_verification=1 WHERE agency='manual'")
        con.execute("UPDATE rating SET needs_verification=0 WHERE agency!='manual'")
    con.commit()


def upsert_rating(con, issuer_id, agency, rating, outlook, action, rated_on,
                   source="manual", secid=None, verified_on=None,
                   needs_verification=None) -> None:
    con.execute(
        "UPDATE rating SET is_current=0 WHERE issuer_id=? AND agency=? AND rated_on<>?",
        (issuer_id, agency, rated_on))
    con.execute(
        "INSERT INTO rating(issuer_id,secid,agency,rating,outlook,action,"
        "rated_on,source,is_current,verified_on,needs_verification) "
        "VALUES(?,?,?,?,?,?,?,?,1,?,?) "
        "ON CONFLICT(issuer_id,agency,rated_on) DO UPDATE SET "
        "secid=excluded.secid, rating=excluded.rating, outlook=excluded.outlook,"
        "action=excluded.action, source=excluded.source, is_current=1,"
        "verified_on=excluded.verified_on, needs_verification=excluded.needs_verification",
        (issuer_id, secid, agency, rating, outlook, action, rated_on, source,
         verified_on, needs_verification))


def retire_rating(con, issuer_id: str, agency: str) -> None:
    """Explicitly mark an issuer's current rating under one agency as no
    longer current -- needed when superseding a 'manual' bootstrap row with
    a real, differently-dated agency row, since upsert_rating's own
    supersession only retires rows under the SAME agency (see the
    current_rating() sort-order caveat above)."""
    con.execute(
        "UPDATE rating SET is_current=0 WHERE issuer_id=? AND agency=?",
        (issuer_id, agency))


def _current_rating_row(con, issuer_id: str):
    """Shared selection logic behind current_rating() and current_rating_row().
    A real-agency-sourced row always outranks a 'manual' row, regardless of
    which is more recently dated -- 'manual' ratings are an unverified
    bootstrap (see seed_ratings.py), not an independent signal that should
    ever override a genuine agency action. Among rows on the same side of
    that divide, rated_on is the tiebreaker, and _AGENCY_PRIORITY only breaks
    an exact date tie between two real agencies (or two manual rows)."""
    rows = con.execute(
        "SELECT * FROM rating WHERE issuer_id=? AND is_current=1",
        (issuer_id,)).fetchall()
    if not rows:
        return None

    def sort_key(r):
        agency_upper = (r["agency"] or "").strip().upper()
        is_manual = agency_upper == "MANUAL"
        try:
            prio = _AGENCY_PRIORITY.index(agency_upper)
        except ValueError:
            prio = len(_AGENCY_PRIORITY)
        return (0 if is_manual else 1, r["rated_on"], -prio)

    return max(rows, key=sort_key)


def current_rating(con, issuer_id: str) -> str | None:
    """Most recent rating string (ruXX format) for an issuer across all
    agencies -- see _current_rating_row for the selection logic."""
    row = _current_rating_row(con, issuer_id)
    return row["rating"] if row else None


def current_rating_row(con, issuer_id: str) -> dict | None:
    """Like current_rating(), but returns the full row (agency, needs_verification,
    verified_on, ...) -- used where a caller needs to know whether the winning
    rating has actually been verified, not just its value."""
    row = _current_rating_row(con, issuer_id)
    return dict(row) if row else None


def rating_history(con, issuer_id: str) -> list:
    return con.execute(
        "SELECT * FROM rating WHERE issuer_id=? ORDER BY rated_on DESC",
        (issuer_id,)).fetchall()


def all_current_ratings(con, issuer_id: str) -> list:
    """All is_current=1 rows across every agency for an issuer -- unlike
    current_rating()/current_rating_row(), which pick a single winner via the
    agency-priority tiebreak, this exposes every currently-live rating so a
    caller (e.g. the narrative-card export) can detect and surface genuine
    cross-agency disagreement instead of silently hiding it behind the
    tiebreak's choice."""
    rows = con.execute(
        "SELECT * FROM rating WHERE issuer_id=? AND is_current=1 ORDER BY agency",
        (issuer_id,)).fetchall()
    return [dict(r) for r in rows]


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
    vdo_scope      INTEGER DEFAULT 0,
    coupon_period  INTEGER,
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
    # vdo_scope: broad-ВДО-screen flag (rating-band-or-unrated + spread
    # threshold + liquidity floor + bank-subordinated/perpetual exclusion --
    # see universe_pipeline.compute_vdo_scope()). Retrofit, same pattern.
    if "vdo_scope" not in cols:
        con.execute(
            "ALTER TABLE universe_snapshot ADD COLUMN vdo_scope INTEGER DEFAULT 0")
    # ev_positive/yield_zone: PD-adjusted expected-value inflection (Task A,
    # compute/inflection.py) -- ev_positive is the floor_ytm check
    # (aey_base >= break-even for the tier's PD), yield_zone folds that plus
    # the market-spread-vs-implied-fair-spread ceiling into one of 'green'/
    # 'caution'/'negative_ev'/'unrated'. Computed as a second pass in
    # universe_pipeline.run_universe() (needs the whole run's rated-bond
    # spread distribution before any single bond can be classified against
    # it), not inline with the rest of the per-bond fields above. Retrofit,
    # same pattern.
    if "ev_positive" not in cols:
        con.execute("ALTER TABLE universe_snapshot ADD COLUMN ev_positive INTEGER")
    if "yield_zone" not in cols:
        con.execute("ALTER TABLE universe_snapshot ADD COLUMN yield_zone TEXT")
    # coupon_period: MOEX ISS COUPONPERIOD (days between coupons), used to
    # derive the coupon-frequency filter (monthly/quarterly/semi-annual/
    # annual) on the Рынок tab. Retrofit, same pattern.
    if "coupon_period" not in cols:
        con.execute("ALTER TABLE universe_snapshot ADD COLUMN coupon_period INTEGER")
    con.commit()


def insert_universe_snapshot(con, **f) -> None:
    cols = ("secid,snap_date,short_name,emitent_inn,emitent_title,mat_date,"
            "offer_date,coupon_pct,list_level,qualified,last_pct,yield_moex,"
            "yield_to_offer,accrued_int,volume,num_trades,duration_moex,ytm,"
            "tenor_yrs,rf_rate,g_spread_bp,yield_source,rating,"
            "implied_fair_spread_bp,mispricing_bp,in_watchlist,signal_excluded,"
            "mod_dur,aey_base,aey_stress,aey_bull,aey_base_risk_adj,vdo_scope,"
            "coupon_period")
    f.setdefault("vdo_scope", 0)  # added after callers in older tests were written
    f.setdefault("coupon_period", None)  # added after callers in older tests were written
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    con.execute(
        f"INSERT OR REPLACE INTO universe_snapshot({cols}) VALUES({placeholders})",
        f)


def bulk_update_yield_zone(con, rows: list[tuple]) -> None:
    """rows: list of (ev_positive, yield_zone, secid, snap_date). Second-pass
    write for compute/inflection.py's classification -- see universe_pipeline.
    run_universe(), which computes the whole run's rated-bond spread
    distribution before any single bond can be classified, so this can't be
    folded into insert_universe_snapshot()'s per-bond INSERT above."""
    con.executemany(
        "UPDATE universe_snapshot SET ev_positive=?, yield_zone=? "
        "WHERE secid=? AND snap_date=?", rows)


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


# --- OFZ carry/roll-down: real MOEX government-bond data (see
# ofz_pipeline.py). This is the mirror-image of universe_snapshot's OFZ
# exclusion (TQOB/SPOB boards, SU-prefixed secids) -- same MOEX bulk fetch,
# opposite filter, kept in a separate table because OFZ carries no
# credit-rating/mispricing columns (sovereign, no PD layer) and instead
# carries instrument_type + real duration/convexity where computable. -----

OFZ_SCHEMA = """
CREATE TABLE IF NOT EXISTS ofz_snapshot (
    secid           TEXT NOT NULL,
    snap_date       TEXT NOT NULL,
    short_name      TEXT,
    isin            TEXT,
    board_id        TEXT,
    instrument_type TEXT,       -- ПД | ПК | ИН | АД (see ofz_pipeline.classify_instrument_type)
    mat_date        TEXT,
    coupon_pct      REAL,       -- annual coupon, % of face; NULL for floaters between resets
    face_value      REAL,
    last_pct        REAL,       -- clean price, % of face
    accrued_int_pct REAL,       -- НКД, % of face
    dirty_price_pct REAL,
    ytm             REAL,
    mod_dur         REAL,
    convexity       REAL,       -- NULL where not computable (floater/linker proxy path)
    tenor_yrs       REAL,
    volume          REAL,
    num_trades      INTEGER,
    yield_source    TEXT,       -- 'computed' (real cashflows) | 'moex' (proxy)
    PRIMARY KEY (secid, snap_date)
);
"""


def init_ofz(con) -> None:
    con.executescript(OFZ_SCHEMA)
    con.commit()


def upsert_ofz_snapshot(con, **f) -> None:
    cols = ("secid,snap_date,short_name,isin,board_id,instrument_type,mat_date,"
            "coupon_pct,face_value,last_pct,accrued_int_pct,dirty_price_pct,"
            "ytm,mod_dur,convexity,tenor_yrs,volume,num_trades,yield_source")
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    con.execute(
        f"INSERT OR REPLACE INTO ofz_snapshot({cols}) VALUES({placeholders})", f)


def latest_ofz_snap_date(con) -> str | None:
    row = con.execute(
        "SELECT snap_date FROM ofz_snapshot ORDER BY snap_date DESC LIMIT 1"
    ).fetchone()
    return row["snap_date"] if row else None


def ofz_snapshot_rows(con, snap_date: str) -> list:
    return con.execute(
        "SELECT * FROM ofz_snapshot WHERE snap_date=? ORDER BY mat_date",
        (snap_date,)).fetchall()


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


COMPANY_SCAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_scan_result (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    inn                     TEXT,
    query                   TEXT NOT NULL,
    scanned_at              TEXT NOT NULL,
    model                   TEXT,
    issuer_name             TEXT,
    track                   TEXT,
    issuer_quality          TEXT,
    bond_suitability        TEXT,
    confidence              TEXT,
    data_coverage_pct       REAL,
    raw_markdown            TEXT,
    json_addendum           TEXT,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_read_tokens       INTEGER,
    cache_creation_tokens   INTEGER,
    web_searches            INTEGER,
    elapsed_ms              INTEGER,
    rating_snapshot         TEXT,
    rated_on_snapshot       TEXT,
    filing_count_snapshot   INTEGER,
    filing_latest_snapshot  TEXT
);
"""


def init_company_scan(con) -> None:
    """company-scan pipeline (src/hybond/company_scan_pipeline.py):
    server-side port of dashboard/template.html's "Проверить компанию"
    panel. One row per scan (an issuer can be scanned multiple times over
    time -- `id` is the PK, not `inn`), so history/drift can be queried
    later rather than only the latest result."""
    con.executescript(COMPANY_SCAN_SCHEMA)
    # decision/decision_reason/decision_at: the user's OWN follow-up call on
    # a scan result (promote / hold / avoid + why), distinct from the scan's
    # own issuer_quality/bond_suitability verdict -- that's the model's
    # assessment, this is what the user actually decided to do about it.
    # Same PRAGMA table_info + conditional ALTER TABLE retrofit pattern used
    # everywhere else in this file (see market_note.feed_url, event.
    # classified_by, etc.) -- all nullable, existing rows stay NULL.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(company_scan_result)")}
    if "decision" not in cols:
        con.execute("ALTER TABLE company_scan_result ADD COLUMN decision TEXT")
    if "decision_reason" not in cols:
        con.execute("ALTER TABLE company_scan_result ADD COLUMN decision_reason TEXT")
    if "decision_at" not in cols:
        con.execute("ALTER TABLE company_scan_result ADD COLUMN decision_at TEXT")
    con.commit()


def insert_company_scan_result(con, **f) -> int:
    cols = ("inn,query,scanned_at,model,issuer_name,track,issuer_quality,"
            "bond_suitability,confidence,data_coverage_pct,raw_markdown,"
            "json_addendum,input_tokens,output_tokens,cache_read_tokens,"
            "cache_creation_tokens,web_searches,elapsed_ms,rating_snapshot,"
            "rated_on_snapshot,filing_count_snapshot,filing_latest_snapshot")
    values = {c: f.get(c) for c in cols.split(",")}
    placeholders = ",".join(f":{c}" for c in cols.split(","))
    cur = con.execute(
        f"INSERT INTO company_scan_result({cols}) VALUES({placeholders})", values)
    return cur.lastrowid


def latest_company_scan_by_inn(con) -> dict[str, dict]:
    """Most recent company_scan_result row per ИНН (rows with inn IS NULL --
    free-text queries that never resolved to an ИНН, see
    company_scan_pipeline.resolve_inn -- can't be tracked this way, same
    limitation as the browser's localStorage history)."""
    rows = con.execute(
        "SELECT * FROM company_scan_result WHERE inn IS NOT NULL "
        "AND scanned_at = (SELECT MAX(scanned_at) FROM company_scan_result r2 "
        "WHERE r2.inn = company_scan_result.inn)"
    ).fetchall()
    return {r["inn"]: dict(r) for r in rows}


def company_scan_history(con, inn: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM company_scan_result WHERE inn=? ORDER BY scanned_at DESC",
        (inn,)).fetchall()]


# --- Manually-flagged promotion candidates ----------------------------------
# `hybond promote --inn/--name`: promote.py's own ranking only ever sees
# issuers already present in universe_snapshot (MOEX-tracked ВДО bonds) --
# an issuer found purely via company-scan (free-web research, no MOEX quote)
# can never appear there. This table is a lightweight, separate log of
# "flag this issuer for the promotion report too", not a ranking input and
# not a watchlist change -- adding a row here does NOT touch watchlist.json
# or the `issuer` table. One row per ИНН (re-flagging updates it in place)
# since the point is current visibility in the report, not a scan-style
# history -- company_scan_result already owns that history separately.

MANUAL_CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_candidate (
    inn        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    note       TEXT
);
"""


def init_manual_candidate(con) -> None:
    con.executescript(MANUAL_CANDIDATE_SCHEMA)
    con.commit()


def add_manual_candidate(con, inn: str, name: str, note: str | None = None,
                          added_at: str | None = None) -> None:
    added_at = added_at or datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO manual_candidate(inn,name,added_at,note) VALUES(?,?,?,?) "
        "ON CONFLICT(inn) DO UPDATE SET name=excluded.name, "
        "added_at=excluded.added_at, note=excluded.note",
        (inn, name, added_at, note))


def list_manual_candidates(con) -> list[dict]:
    """Excludes ИНН already present in `issuer` (i.e. already actually
    promoted/watchlisted) -- same "don't show stale duplicates" principle
    list_scan_candidates() uses for the company-scan candidate chip list."""
    known = {r["issuer_id"] for r in con.execute("SELECT issuer_id FROM issuer")}
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM manual_candidate ORDER BY added_at DESC")]
    return [r for r in rows if r["inn"] not in known]


SCAN_DECISIONS = {"promote", "hold", "avoid"}


def set_company_scan_decision(con, scan_id: int, decision: str, reason: str | None,
                               decided_at: str | None = None) -> None:
    """Records the user's own follow-up call on one company_scan_result row
    (by `id`, not `inn` -- an issuer can have several scans over time, and a
    decision belongs to a specific scan's findings, not the issuer in
    general). `decided_at` defaults to now (UTC ISO) if not given."""
    if decision not in SCAN_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(SCAN_DECISIONS)}, got {decision!r}")
    decided_at = decided_at or datetime.now().isoformat(timespec="seconds")
    cur = con.execute(
        "UPDATE company_scan_result SET decision=?, decision_reason=?, decision_at=? "
        "WHERE id=?", (decision, reason, decided_at, scan_id))
    if cur.rowcount == 0:
        raise ValueError(f"no company_scan_result row with id={scan_id}")
