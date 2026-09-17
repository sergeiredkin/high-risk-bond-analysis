"""Export everything the dashboard needs into one JSON file:
data/dashboard_data.json.

Sources combined:
  - watchlist.json           issuer metadata (name, sector, risk_tier, rating)
  - data/factlists/*.json    latest per-issuer factlist (bonds, flags, events,
                              six_pillars, thesis_status) — Slice 2 output
  - data/db/hybond.sqlite    full thesis text (assumptions/invalidation),
                              flag history for the sparkline
  - data/report.json         day-1 spread screen + maturity wall (Slice 1)

Does not compute anything new — pure aggregation for the dashboard renderer
(scripts/build_dashboard.py) to consume.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from hybond.classify_utils import queue_bucket  # noqa: E402
from hybond.health import get_unresolved as health_unresolved  # noqa: E402
from hybond.health import log_health  # noqa: E402
from hybond.health import log_health_once_today  # noqa: E402
from hybond.health import resolve_code as health_resolve_code  # noqa: E402
from hybond.health import summary as health_summary  # noqa: E402
from hybond.compute.default_probs import (  # noqa: E402
    DEFAULT_PROBS, LGD, RATING_ORDER, RECOVERY_RATE, implied_fair_spread_bp, lookup)
from hybond.compute import inflection  # noqa: E402
from hybond.scrape.smartlab_news import NEWS_ALIASES  # noqa: E402
from hybond.store import db  # noqa: E402
from hybond import pd_import  # noqa: E402
from hybond import moex_defaults  # noqa: E402
from hybond import portfolio  # noqa: E402
from hybond import promote  # noqa: E402
from hybond import spravka_parser  # noqa: E402
from hybond.universe_pipeline import VDO_MIN_VOLUME  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FACTLIST_DIR = ROOT / "data" / "factlists"
OUT_PATH = ROOT / "data" / "dashboard_data.json"
SPRAVKA_MD_PATH = ROOT / "spravka_vdo.md"

# Rating tiers the market map draws fair-value overlay lines for (spec: AA/A/BBB/BB).
FAIR_VALUE_TIERS = ["ruAA", "ruA", "ruBBB", "ruBB"]

# Sector Labels task (2026-08-17): Russian display names for issuer.sector.
# Built from `SELECT DISTINCT sector FROM issuer` against the live DB, not
# a guessed taxonomy -- see the task's own audit note for the discrepancy
# with an earlier proposed key set. sectorLabel() in the dashboard JS falls
# back to the raw string for any value not covered here.
# Portfolio donut charts (Portfolio donut charts task, 2026-08-29): a fixed
# name->color dict rather than the dashboard's existing sectorColor() (a
# hash of the sector string into SECTOR_PALETTE) -- confirmed live that hash
# actually COLLIDES for sectors this exact feature needs to tell apart
# (mobility_tech/debt_collection/energy all hash to the same
# SECTOR_PALETTE entry, '#2a78d6'), which would make two differently-colored
# donut legend entries indistinguishable on the chart itself. This dict is
# deliberately its own source of truth, shared between this export and the
# donut-drawing JS, rather than "matching" a hash function that can't
# guarantee distinct colors for an arbitrary sector set.
SECTOR_COLORS = {
    'real_estate': '#E87040',
    'leasing': '#4A90D9',
    'mobility_tech': '#7B68EE',
    'debt_collection': '#20B2AA',
    'energy': '#FFD700',
    'construction': '#8B7355',
    'retail': '#FF69B4',
    'agriculture': '#6DBE6D',
    'mfo': '#FF8C00',
    'other': '#888888',
}

SECTOR_LABELS_RU = {
    "agriculture": "Агросектор",
    "commercial_real_estate": "Коммерческая недвижимость",
    "construction": "Строительство",
    "construction_materials": "Стройматериалы",
    "debt_collection": "Коллекторы",
    "energy": "Энергетика",
    "forestry_paper": "Лес и бумага",
    "fuel_retail": "Топливный ретейл",
    "general_industrial": "Пром. производство",
    "gold_mining": "Золотодобыча",
    "holding_diversified": "Диверсифицированный холдинг",
    "industrial": "Промышленность",
    "leasing": "Лизинг",
    "logistics": "Логистика",
    "metals": "Металлургия",
    "microfinance": "МФО",
    "mobility_tech": "Мобильность/технологии",
    "pawnshop_resale": "Ломбарды/скупка",
    "real_estate": "Недвижимость",
    "retail": "Ретейл",
    "services": "Услуги",
    "technology": "Технологии",
    "trading": "Торговля",
}


def latest_factlists() -> dict[str, dict]:
    """One factlist per issuer_id: the most recent snap_date on disk."""
    best: dict[str, tuple[str, dict]] = {}
    for p in FACTLIST_DIR.glob("*.json"):
        issuer_id, snap_date = p.stem.rsplit("_", 1)
        data = json.loads(p.read_text(encoding="utf-8"))
        if issuer_id not in best or snap_date > best[issuer_id][0]:
            best[issuer_id] = (snap_date, data)
    return {k: v[1] for k, v in best.items()}


def flag_history(con) -> dict[str, list[dict]]:
    rows = con.execute(
        "SELECT issuer_id, snap_date, detector, signal, metric, detail "
        "FROM flag_log ORDER BY snap_date"
    ).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["issuer_id"]].append({
            "snap_date": r["snap_date"], "detector": r["detector"],
            "signal": r["signal"], "metric": r["metric"], "detail": r["detail"],
        })
    return out


def filing_counts_by_issuer(con) -> dict[str, dict]:
    rows = con.execute(
        "SELECT issuer_id, COUNT(*) total, SUM(classified) classified "
        "FROM filing GROUP BY issuer_id"
    ).fetchall()
    return {r["issuer_id"]: {"total": r["total"], "classified": r["classified"] or 0}
            for r in rows}


def queue_count(con) -> int:
    """Unclassified filings + market_notes, noise-pattern matches excluded --
    the home screen / Вотчлист tab's "N в очереди на классификацию"
    indicator. Same classify_utils.queue_bucket() the CLI's `hybond queue`/
    `hybond market-notes-queue` three-section display uses, so the
    dashboard's count can never disagree with what those commands show."""
    from hybond.classify_utils import queue_bucket
    filing_rows = con.execute("SELECT title FROM filing WHERE classified=0").fetchall()
    note_rows = con.execute("SELECT headline FROM market_note WHERE severity IS NULL").fetchall()
    n = sum(1 for r in filing_rows if queue_bucket(r["title"]) != "noise")
    n += sum(1 for r in note_rows if queue_bucket(r["headline"]) != "noise")
    return n


def portfolio_data_freshness(con) -> dict[str, str | None]:
    """Latest snapshot date each of the Пресеты Builder's two AEY sources
    was last refreshed. `universe_snapshot` is written by `hybond universe`,
    `spread_snapshot` by `hybond run` -- these two pipelines run on
    different schedules and their snap_date columns can diverge by weeks
    (confirmed live, Portfolio Presets task: the dashboard's exported AEY
    reads universe_snapshot while build_preset() computes fresh from
    spread_snapshot, so the Builder can select a different bond set than a
    CLI portfolio-build run for identical filters). Exposed here so the
    Builder UI can warn when the two have drifted apart rather than the
    mismatch surfacing silently as different results. Reuses the same two
    "latest snapshot date" lookups day1_snap_date/day2_snap_date are already
    built from (db.latest_universe_snap_date, _latest_spread_snap_date)
    rather than a third ad hoc query."""
    return {
        "universe_snapshot_date": db.latest_universe_snap_date(con),
        "spread_snapshot_date": _latest_spread_snap_date(con),
    }


def export_portfolio_allocation(con, portfolio_id: int | None = None) -> dict:
    """Sector/rating/health allocation for one saved portfolio -- the kind
    `hybond portfolio-build --save` writes, distinct from the client-side
    Presets Builder preview (which never touches the DB). Returns {} if the
    portfolio doesn't exist (or, with portfolio_id=None, if no active
    portfolio exists at all), or if it has zero bonds -- the dashboard shows
    an empty state in either case rather than an empty/misleading chart.

    portfolio_id=None (default) keeps the original single-portfolio
    behavior: the most recently created status='active' portfolio. Pass an
    explicit id (Multiple portfolio comparison task, 2026-08-31) to export
    one specific portfolio out of several active ones -- see
    export_all_active_portfolios() below, which drives the Портфель tab's
    new Сравнение sub-tab.

    Joins portfolio_bond.isin -> issuer_bond.secid -> issuer (sector,
    health_status) for sector/health, and universe_snapshot (latest
    snap_date only -- it's one row per (secid, snap_date), so joining
    without that filter would fan out across every historical snapshot)
    for rating/aey_base. isin and secid are the same string for every bond
    in this DB (confirmed: 0 mismatches across the `bond` table) -- Russian
    corporate bonds use ISIN as their MOEX SECID, so no extra join through
    `bond` is needed.

    avg_aey (per bucket and overall) is weighted by each bond's own
    portfolio weight, computed only over bonds with a known aey_base
    (renormalized by their weight sum) -- a bond missing a current rating/
    AEY (e.g. not yet re-confirmed by the latest `hybond universe` run)
    still counts toward bond_count/weight in its bucket, it just doesn't
    silently pull the average toward 0.
    """
    db.init_portfolio(con)
    db.init_issuer_bond(con)
    db.init_universe(con)

    if portfolio_id is None:
        row = con.execute(
            "SELECT id, name, preset_type FROM portfolio WHERE status='active' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
    else:
        row = con.execute(
            "SELECT id, name, preset_type FROM portfolio WHERE id = ?",
            (portfolio_id,)).fetchone()
    if row is None:
        return {}
    portfolio_id, name, preset_type = row["id"], row["name"], row["preset_type"]

    latest_snap = db.latest_universe_snap_date(con)
    bond_rows = con.execute(
        "SELECT pb.isin, pb.weight, i.sector, i.health_status, i.name AS issuer_name, "
        "ib.issuer_id, us.rating, us.aey_base, us.short_name, us.mod_dur "
        "FROM portfolio_bond pb "
        "JOIN issuer_bond ib ON ib.secid = pb.isin "
        "JOIN issuer i ON i.issuer_id = ib.issuer_id "
        "LEFT JOIN universe_snapshot us ON us.secid = pb.isin AND us.snap_date = ? "
        "WHERE pb.portfolio_id = ?",
        (latest_snap, portfolio_id)).fetchall()
    if not bond_rows:
        return {}

    total_weight = sum(b["weight"] or 0.0 for b in bond_rows) or 1.0

    def _new_bucket() -> dict:
        return {"weight": 0.0, "bond_count": 0, "_aey_wsum": 0.0, "_aey_wtot": 0.0}

    sector_alloc: dict[str, dict] = {}
    rating_alloc: dict[str, dict] = {}
    health_alloc: dict[str, dict] = {}
    overall_aey_wsum = overall_aey_wtot = 0.0
    issuer_ids: set[str] = set()
    max_duration: float | None = None

    for b in bond_rows:
        weight = (b["weight"] or 0.0) / total_weight
        aey = b["aey_base"]
        sector = b["sector"] or "other"
        rating = b["rating"] or "no_rating"
        health = b["health_status"] or "green"
        if b["issuer_id"]:
            issuer_ids.add(b["issuer_id"])
        if b["mod_dur"] is not None:
            max_duration = b["mod_dur"] if max_duration is None else max(max_duration, b["mod_dur"])

        for store, key in ((sector_alloc, sector), (rating_alloc, rating)):
            bucket = store.setdefault(key, _new_bucket())
            bucket["weight"] += weight
            bucket["bond_count"] += 1
            if aey is not None:
                bucket["_aey_wsum"] += weight * aey
                bucket["_aey_wtot"] += weight

        h = health_alloc.setdefault(health, {"weight": 0.0, "bond_count": 0})
        h["weight"] += weight
        h["bond_count"] += 1

        if aey is not None:
            overall_aey_wsum += weight * aey
            overall_aey_wtot += weight

    for store in (sector_alloc, rating_alloc):
        for bucket in store.values():
            bucket["avg_aey"] = (bucket["_aey_wsum"] / bucket["_aey_wtot"]
                                  if bucket["_aey_wtot"] > 0 else None)
            del bucket["_aey_wsum"], bucket["_aey_wtot"]

    for key, bucket in sector_alloc.items():
        bucket["color"] = SECTOR_COLORS.get(key, SECTOR_COLORS["other"])

    # Per-bond rows, not just aggregates -- the dashboard's sector-donut
    # click-to-filter (Portfolio donut charts task, Phase 2) needs an actual
    # bond list to filter, and the saved active portfolio has no other
    # dashboard-visible bond list to reuse (this DB portfolio is entirely
    # separate from the Обзор tab's own client-side position-picker
    # preview, which never touches these tables -- see save_portfolio()'s
    # docstring in portfolio.py).
    bonds = [{
        "isin": b["isin"],
        "short_name": b["short_name"] or b["issuer_name"] or b["isin"],
        "sector": b["sector"] or "other",
        "rating": b["rating"],
        "issuer_id": b["issuer_id"],
        "health_status": b["health_status"] or "green",
        "weight": (b["weight"] or 0.0) / total_weight,
        "aey_base": b["aey_base"],
    } for b in bond_rows]

    # expected_loss/net_return come from the latest portfolio_snapshot, not
    # recomputed here -- Multiple portfolio comparison task (2026-08-31).
    # monitor_portfolios()'s daily snapshot can have a NARROWER metrics_json
    # than save_portfolio()'s first one (avg_aey/expected_loss/net_return/
    # n_alerts only, no n_issuers/n_sectors -- confirmed live for
    # portfolio_id=1 after a monitor run), so n_issuers/max_duration below
    # are computed fresh from bond_rows instead of trusted from there.
    snap_row = con.execute(
        "SELECT expected_loss FROM portfolio_snapshot WHERE portfolio_id = ? "
        "ORDER BY id DESC LIMIT 1", (portfolio_id,)).fetchone()
    expected_loss = snap_row["expected_loss"] if snap_row else None
    avg_aey = (overall_aey_wsum / overall_aey_wtot) if overall_aey_wtot > 0 else None
    net_return = (avg_aey - expected_loss) if (avg_aey is not None and expected_loss is not None) else None

    return {
        "bonds": bonds,
        "sector_allocation": sector_alloc,
        "rating_allocation": rating_alloc,
        "health_allocation": health_alloc,
        "summary": {
            "total_bonds": len(bond_rows),
            "n_sectors": len(sector_alloc),
            "n_issuers": len(issuer_ids),
            # "no_rating" isn't a rating -- excluded from the count, though
            # its bucket stays in rating_allocation so the donut's slices
            # still sum to 100% of the portfolio.
            "n_ratings": len([k for k in rating_alloc if k != "no_rating"]),
            "avg_aey": avg_aey,
            "expected_loss": expected_loss,
            "net_return": net_return,
            "max_duration": max_duration,
            "preset_type": preset_type,
            "name": name,
        },
    }


def export_all_active_portfolios(con, limit: int = 3) -> list[dict]:
    """Up to `limit` most-recently-created status='active' portfolios, each
    with its own full allocation via export_portfolio_allocation() --
    Multiple portfolio comparison task (2026-08-31). Drives the Портфель
    tab's new Сравнение sub-tab. Distinct from portfolio_allocation (kept
    unchanged for the existing single-portfolio Обзор consumers, which
    always show the latest of these same portfolios) -- this exports all of
    them so the dashboard can render up to 3 side by side.
    """
    db.init_portfolio(con)
    rows = con.execute(
        "SELECT id, name, preset_type, sizing_mode, created_at "
        "FROM portfolio WHERE status='active' ORDER BY created_at DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for p in rows:
        alloc = export_portfolio_allocation(con, portfolio_id=p["id"])
        if not alloc:
            continue
        out.append({
            "id": p["id"],
            "name": p["name"],
            "preset_type": p["preset_type"],
            "sizing_mode": p["sizing_mode"],
            "created_at": p["created_at"],
            # The very rules build_preset() enforced when it picked these
            # bonds (portfolio.PRESETS), exported so the dashboard's
            # "Проверка портфеля" can check a portfolio against its own
            # construction rules instead of inventing thresholds
            # (Купон Лаб redesign stage 3, 2026-09-16). An unknown
            # preset_type (hand-built portfolio) carries None.
            "constraints": portfolio.PRESETS.get(p["preset_type"]),
            "allocation": alloc,
        })
    return out


def spread_history_by_secid(con) -> dict[str, list[dict]]:
    rows = con.execute(
        "SELECT secid, snap_date, g_spread_bp, ytm FROM spread_snapshot "
        "ORDER BY snap_date"
    ).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["secid"]].append({
            "snap_date": r["snap_date"], "g_spread_bp": r["g_spread_bp"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
        })
    return out


def spread_history_by_issuer(con, active_watchlist_issuer_ids, today: str) -> tuple[dict, dict]:
    """Вотчлист Restructure task (2026-08-16): daily G-spread history per
    active watchlist issuer, for the Рынок tab's new "Динамика G-спреда"
    delta table + "История G-спреда" chart. Distinct from
    spread_history_by_secid() above (per-bond, unfiltered, unbounded) --
    this is per-ISSUER, capped to 90 days, and picks one representative
    bond per issuer (highest average traded volume, from price_snapshot --
    spread_snapshot itself has no volume column) so multi-bond issuers
    don't show overlapping/conflicting spread lines.
    """
    history: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    issuer_names = {r["issuer_id"]: r["name"] for r in con.execute("SELECT issuer_id, name FROM issuer")}
    for issuer_id in active_watchlist_issuer_ids:
        rep = con.execute(
            "SELECT b.secid, AVG(ps.volume) avg_vol FROM bond b "
            "JOIN price_snapshot ps ON ps.secid = b.secid "
            "WHERE b.issuer_id = ? GROUP BY b.secid ORDER BY avg_vol DESC LIMIT 1",
            [issuer_id],
        ).fetchone()
        if not rep:
            continue
        rows = con.execute(
            "SELECT ss.snap_date, ss.g_spread_bp FROM spread_snapshot ss "
            "WHERE ss.secid = ? AND ss.snap_date >= date(?, '-90 days') "
            "AND ss.g_spread_bp IS NOT NULL AND ss.g_spread_bp > 0 "
            "ORDER BY ss.snap_date ASC",
            [rep["secid"], today],
        ).fetchall()
        if len(rows) >= 2:
            history[issuer_id] = [{"date": r["snap_date"], "g_spread_bp": r["g_spread_bp"]} for r in rows]
            name = issuer_names.get(issuer_id, issuer_id)
            labels[issuer_id] = name if len(name) <= 20 else name[:20]
    return history, labels


def convexity_by_secid(con) -> dict[str, float | None]:
    """secid -> latest computed convexity from spread_snapshot -- Task B's
    verdict component (per-bond drawer, Layer 3/pro-only) wants it alongside
    G-spread/mod_dur, neither of which the factlist bond dict currently
    carries convexity for (assemble.py's Bond dataclass stops at mod_dur).
    NULL when yield_source='moex' (floaters -- see spread_snapshot's own
    column comment), same as every other convexity consumer in this
    codebase."""
    rows = con.execute(
        "SELECT secid, convexity FROM spread_snapshot s1 WHERE snap_date = "
        "(SELECT MAX(snap_date) FROM spread_snapshot s2 WHERE s2.secid = s1.secid)"
    ).fetchall()
    return {r["secid"]: r["convexity"] for r in rows}


def mispricing_delta_by_secid(con) -> dict[str, dict]:
    """Per-bond mispricing_bp trend: current vs. previous available
    universe_snapshot day for that secid. A plain two-point comparison (see
    compute.pillars.pillars_delta's docstring for why this isn't a
    statistical model) -- universe_snapshot already stores one row per day
    per secid, so this is a query over existing data, not new storage."""
    rows = con.execute(
        "SELECT secid, snap_date, mispricing_bp FROM universe_snapshot "
        "WHERE mispricing_bp IS NOT NULL ORDER BY secid, snap_date DESC"
    ).fetchall()
    by_secid: dict[str, list] = defaultdict(list)
    for r in rows:
        by_secid[r["secid"]].append((r["snap_date"], r["mispricing_bp"]))
    out: dict[str, dict] = {}
    for secid, vals in by_secid.items():
        if len(vals) < 2:
            out[secid] = {"delta_bp": None, "direction": None}
            continue
        cur, prev = vals[0][1], vals[1][1]
        d = cur - prev
        out[secid] = {
            "delta_bp": round(d, 0),
            "direction": "up" if d > 0 else "down" if d < 0 else "flat",
            "since_date": vals[1][0],
        }
    return out


def full_events_by_issuer(con) -> dict[str, list[dict]]:
    """ALL classified events/market_notes per issuer, not just the 30-day
    'since_last' window assemble.py builds for the notification panel
    (events_since_last/market_notes_since_last -- deliberately narrow for
    that use). Part D's per-bond detail panel needs the real history (the
    2026-07-31 classification pass alone covered filings back to January),
    so this is a second, separate query rather than widening the shared
    30-day one and risking changing what the notification panel shows."""
    events_by_issuer: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute(
            "SELECT issuer_id, occurred, category, severity, summary "
            "FROM event ORDER BY occurred DESC"):
        events_by_issuer[r["issuer_id"]].append({
            "occurred": r["occurred"], "category": r["category"],
            "severity": r["severity"], "summary": r["summary"], "source": "event",
        })
    try:
        for r in con.execute(
                "SELECT issuer_id, occurred, category, severity, headline, source, url "
                "FROM market_note WHERE severity IS NOT NULL ORDER BY occurred DESC"):
            events_by_issuer[r["issuer_id"]].append({
                "occurred": r["occurred"], "category": r["category"],
                "severity": r["severity"], "summary": r["headline"],
                "source": r["source"], "url": r["url"],
            })
    except Exception:  # noqa: BLE001 -- market_note may not exist yet
        pass
    for issuer_id, evs in events_by_issuer.items():
        evs.sort(key=lambda e: e["occurred"] or "", reverse=True)
    return events_by_issuer


def market_wide_notes(con) -> list[dict]:
    """MOEX Listing/CBR Press task (2026-08-17): market_note rows with no
    issuer_id (moex_listing/cbr_press -- see rss_generic.py's docstring for
    why these aren't issuer-scoped like smartlab_news). full_events_by_issuer()
    above structurally can't surface these -- it's keyed by issuer_id (a
    NULL-keyed bucket in that dict never gets attached to any real issuer in
    main()'s per-watchlist-issuer loop) AND filters `WHERE severity IS NOT
    NULL` (these rows are never LLM-classified -- hy-event-classify has no
    market_note input path yet, per that module's own docstring). Without
    this separate export, Лента событий has structurally nothing to show
    for these two feeds regardless of any display-label change.

    `severity` here is a DISPLAY-ONLY pseudo-severity from queue_bucket()
    (priority->high, normal->medium, noise->low), NOT the real classification
    column (which stays NULL in the DB, untouched) -- same "display filter
    only, never written back" contract classify_utils.queue_bucket()'s own
    docstring already commits to elsewhere in this codebase."""
    try:
        rows = con.execute(
            "SELECT occurred, source, headline, url FROM market_note "
            "WHERE issuer_id IS NULL ORDER BY occurred DESC LIMIT 200"
        ).fetchall()
    except Exception:  # noqa: BLE001 -- market_note may not exist yet
        return []
    out = []
    for r in rows:
        bucket = queue_bucket(r["headline"], source=r["source"])
        pseudo_severity = {"priority": "high", "normal": "medium", "noise": "low"}[bucket]
        out.append({
            "occurred": r["occurred"], "category": None, "severity": pseudo_severity,
            "summary": r["headline"], "source": r["source"], "url": r["url"],
        })
    return out


# google_news headlines that actually signal a NEW bond placement (not a
# routine coupon payment on an already-outstanding issue, which dominates
# this feed's "купон"-matching rows -- "Компания X выплатила N-й купон" is
# the single most common headline shape and is explicitly excluded below).
_PLACEMENT_INCLUDE_RE = [re.compile(p, re.IGNORECASE) for p in (
    r"размеща", r"размещение\s+облигаци", r"новый\s+выпуск",
    r"первичк", r"календарь\s+первич",
)]
_PLACEMENT_EXCLUDE_RE = [re.compile(p, re.IGNORECASE) for p in (
    r"выплати", r"\d+[-–]?(й|го)\s*купон", r"опроверг",
)]
_QUOTED_NAME_RE = re.compile(r"[«\"]([^»\"]{2,40})[»\"]")
_YTM_RE = re.compile(r"YTM\s*([\d]+[,.]?\d*)\s*%", re.IGNORECASE)


def _is_placement_headline(headline: str) -> bool:
    return (any(p.search(headline) for p in _PLACEMENT_INCLUDE_RE)
            and not any(p.search(headline) for p in _PLACEMENT_EXCLUDE_RE))


def _extract_placement_issuer(headline: str) -> tuple[str | None, str | None]:
    """(issuer_name, issuer_id). Prefers a NEWS_ALIASES match (the same
    name/brand -> watchlist issuer_id table smartlab_news.py already
    maintains and validates) -- confident, links to a real issuer_id. Falls
    back to the first «quoted»/"quoted" span in the headline as a display-
    only name with no issuer_id, since that's frequently the company name
    even for issuers with no NEWS_ALIASES entry. Returns (None, None) rather
    than guessing when neither matches."""
    for name, issuer_id in NEWS_ALIASES.items():
        if name in headline:
            return name, issuer_id
    m = _QUOTED_NAME_RE.search(headline)
    return (m.group(1), None) if m else (None, None)


def _extract_ytm(headline: str) -> str | None:
    m = _YTM_RE.search(headline)
    return m.group(1).replace(",", ".") + "%" if m else None


def primary_placements(con, today: str) -> list[dict]:
    """Первичные размещения task (2026-08-25), updated same day per a
    follow-up task: switched from market_note(source='moex_listing') to
    source='google_news' after a data audit found moex_listing structurally
    can't support issuer/YTM (issuer_id NULL, summary truncated to ~200
    chars -- see git history for that finding) while google_news actually
    carries usable placement text (republished Smart-Lab content, tagged
    "- Smart-Lab" in the headline, arriving via the Google News RSS pipeline
    rather than smartlab_news.py's own direct scrape -- source='smartlab_news'
    itself only had 2 matching rows in a live audit, both coupon-payment
    news, not placements).

    Filtered to headlines that read as an actual NEW placement (see
    _is_placement_headline) -- google_news/Smart-Lab content is dominated by
    routine "N-й купон выплачен" notices on already-outstanding issues,
    which are explicitly excluded, not just under-matched.

    issuer/ytm are BEST-EFFORT, not comprehensive: live audit (2026-08-25)
    found 21 genuine placement-signal rows in the current DB, of which 7
    (33%) got a confident NEWS_ALIASES issuer match and 4 (19%) got a YTM
    regex match -- real signal, not full coverage. Never fabricated: a
    headline that doesn't match either pattern gets issuer=null/ytm=null,
    not a guessed value.

    Window: last 30 days through next 14 days (per the original spec) --
    the upper bound is currently a no-op since `occurred` is always in the
    past (these are RSS items already published), kept for when/if a source
    ever forward-dates an announcement."""
    try:
        lo = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
        hi = (date.fromisoformat(today) + timedelta(days=14)).isoformat()
        rows = con.execute(
            "SELECT occurred, headline, url FROM market_note "
            "WHERE source='google_news' AND occurred >= ? AND occurred <= ? "
            "ORDER BY occurred DESC", (lo, hi)
        ).fetchall()
    except Exception:  # noqa: BLE001 -- market_note may not exist yet
        return []
    out = []
    for r in rows:
        headline = r["headline"] or ""
        if not _is_placement_headline(headline):
            continue
        issuer_name, issuer_id = _extract_placement_issuer(headline)
        out.append({
            "date": r["occurred"], "headline": headline, "url": r["url"],
            "issuer": issuer_name, "issuer_id": issuer_id,
            "ytm": _extract_ytm(headline),
        })
    return out


def expected_returns_by_secid(con) -> dict[str, dict]:
    """Part B projections (compute/expected_return.py), keyed by secid, each
    holding whatever of {1y, 2y, maturity} was computed. Reads the MOST
    RECENT snap_date available per secid rather than requiring an exact
    match to today -- pipeline.py (hybond run) computes these, and if that
    hasn't run yet today (or a network hiccup skipped a bond), this degrades
    to the last good value instead of silently going blank. Empty for any
    secid pipeline.py hasn't reached yet (e.g. floaters, which have no fixed
    schedule to project -- see expected_return.py's own scoping note)."""
    try:
        rows = con.execute(
            "SELECT secid, snap_date, horizon_label, horizon_yr, irr_pct, "
            "undiscounted_npv, undiscounted_total_cashflow, "
            "horizon_capped_by_maturity, uses_extrapolated_pd "
            "FROM expected_return_snapshot ORDER BY secid, snap_date DESC"
        ).fetchall()
    except Exception:  # noqa: BLE001 -- table may not exist yet
        return {}
    out: dict[str, dict] = {}
    seen_date: dict[str, str] = {}
    for r in rows:
        secid = r["secid"]
        if secid in seen_date and seen_date[secid] != r["snap_date"]:
            continue  # already have this secid's most-recent date; skip older rows
        seen_date[secid] = r["snap_date"]
        out.setdefault(secid, {})[r["horizon_label"]] = {
            "horizon_yr": r["horizon_yr"], "irr_pct": r["irr_pct"],
            "undiscounted_npv": r["undiscounted_npv"],
            "undiscounted_total_cashflow": r["undiscounted_total_cashflow"],
            "horizon_capped_by_maturity": bool(r["horizon_capped_by_maturity"]),
            "uses_extrapolated_pd": bool(r["uses_extrapolated_pd"]),
        }
    return out


def data_conflicts_for_issuer(con, issuer_id: str, ratings_all: list[dict]) -> list[dict]:
    """Detects genuine disagreements between hybond's own DB rows for one
    issuer, for the narrative-card generator (skills/bond-narrative-card) to
    surface explicitly rather than silently resolving. Two kinds, matching
    the two the prompt's Data Sources section calls out:
      - cross-agency rating disagreement: more than one currently-live
        `rating` row (db.all_current_ratings) whose letter grade differs.
        Differing outlook/action alone is not a conflict (agencies routinely
        run on different review cadences); differing grade is.
      - pillars re-computation drift: the two most recent pillars_snapshot
        rows share a fiscal `period` but their payload disagrees on a
        headline metric -- a same-period recompute that changed the answer
        (e.g. a restated financials_snapshot feeding it), not just a new
        period arriving.
    Best-effort: returns [] rather than raising if a table/column is
    missing, since this is a display-only enrichment, not core pipeline
    output."""
    conflicts: list[dict] = []

    grades = {r["rating"] for r in ratings_all if r.get("rating")}
    if len(grades) > 1:
        conflicts.append({
            "type": "rating_disagreement",
            "detail": [
                {"agency": r["agency"], "rating": r["rating"],
                 "outlook": r.get("outlook"), "rated_on": r.get("rated_on")}
                for r in ratings_all
            ],
        })

    try:
        hist = db.pillars_history(con, issuer_id, limit=2)
    except Exception:  # noqa: BLE001 -- pillars_snapshot may not exist yet
        hist = []
    if len(hist) == 2 and hist[0]["period"] == hist[1]["period"]:
        try:
            cur = json.loads(hist[0]["payload"])
            prev = json.loads(hist[1]["payload"])
        except (ValueError, TypeError, KeyError):
            cur = prev = None
        if cur is not None and prev is not None and cur != prev:
            conflicts.append({
                "type": "pillars_recompute_drift",
                "period": hist[0]["period"],
                "detail": [
                    {"computed_on": hist[1]["computed_on"], "payload": prev},
                    {"computed_on": hist[0]["computed_on"], "payload": cur},
                ],
            })

    return conflicts


def _nearest_snap_date_on_or_before(con, cutoff: str) -> str | None:
    row = con.execute(
        "SELECT snap_date FROM universe_snapshot WHERE snap_date<=? "
        "ORDER BY snap_date DESC LIMIT 1", (cutoff,)).fetchone()
    return row["snap_date"] if row else None


def _latest_spread_snap_date(con) -> str | None:
    """day2_snap_date (day1_snap_date's counterpart, MIN(snap_date) FROM
    universe_snapshot -- see universe_snapshot_bundle()'s earliest_snap_date):
    the most recent date `hybond run` actually wrote spread_snapshot rows,
    i.e. the dashboard's "how fresh is our data" signal (see the day2_snap_date
    comment in main()). Bug fix (2026-08-26): previously read
    issuers[0]["snap_date"] -- whichever issuer happened to be first in the
    per-issuer export loop, not guaranteed to be the most recent date across
    the DB -- which could under-report freshness if that issuer's own last
    factlist/rating touch predates the actual latest pipeline run."""
    row = con.execute("SELECT MAX(snap_date) m FROM spread_snapshot").fetchone()
    return row["m"] if row else None


def cleanup_export_stale_duplicates(con) -> None:
    """One-time cleanup (2026-08-28): EXPORT_STALE was logged on every
    30-min rebuild_dashboard.sh cycle before log_health_once_today existed,
    producing same-day duplicate unresolved rows. Idempotent past the first
    real run -- once deduped, there's nothing left for this to resolve, so
    it's safe to leave running on every export rather than needing a
    separate one-shot flag/migration.

    Unlike log_health()/log_health_once_today(), resolve_code() has no
    "never raises" contract -- confirmed live: this exact call crashed the
    whole export under real concurrent-cron lock contention
    (rebuild_dashboard.sh runs every 30 min against the same DB). A health-
    log housekeeping step must never be able to take down the real export.
    """
    try:
        health_resolve_code(con, 'EXPORT_STALE',
                             'Deduplicated — replaced with once-today guard')
    except sqlite3.Error as e:
        print(f"pipeline_health cleanup skipped (DB error): {e}", file=sys.stderr)


def pipeline_health_export(con, latest_spread: str | None) -> dict:
    """Pipeline Health Dashboard task, Phase 1 (2026-08-28): EXPORT_STALE
    check + summary()/get_unresolved() assembly for DATA.pipeline_health.

    'unresolved' is added beyond the task spec's literal JSON shape (which
    only listed summary()'s aggregate fields) -- Phase 3's health panel
    needs each entry's id/code/detail/recorded_at to render the list and
    build a per-entry "hybond health --resolve ID" command, which the
    aggregate summary alone can't supply.

    Staleness is checked on the underlying data (dates only, day-level
    granularity) -- "24h" is interpreted as "not today's snapshot", the
    finest resolution these snapshots actually carry.
    """
    latest_universe = db.latest_universe_snap_date(con)
    today_iso = date.today().isoformat()
    for label, snap in (("spread_snapshot", latest_spread), ("universe_snapshot", latest_universe)):
        if snap and snap < today_iso:
            # log_health_once_today (2026-08-28 fix): this export script runs
            # every 30 min via rebuild_dashboard.sh -- an unresolved stale
            # snapshot was re-logged on every single cycle, flooding the
            # health log with same-day duplicates. Dedup by code+day; note
            # this means if BOTH spread_snapshot and universe_snapshot are
            # stale the same day, only the first EXPORT_STALE row of the day
            # logs (dedup key is code alone, not code+label).
            log_health_once_today(con, 'dashboard', 'export', 'warn', 'EXPORT_STALE',
                                   f'{label} latest date is {snap}, expected {today_iso}')

    result = health_summary(con)
    # Two queries, not one: get_unresolved() orders by recorded_at and caps,
    # so a burst of warnings buries every older error and the Данные page
    # ends up listing warnings under a header that says "4 ошибки". Pulling
    # errors/criticals separately guarantees the serious ones are always in
    # the payload, however many warnings landed after them (Купон Лаб
    # redesign stage 3, 2026-09-16). Merged by id, newest first.
    serious = health_unresolved(con, min_level="error", limit=20)
    recent = health_unresolved(con, min_level="warn", limit=20)
    merged = {row["id"]: row for row in serious}
    merged.update({row["id"]: row for row in recent})
    result["unresolved"] = sorted(merged.values(),
                                  key=lambda r: r["recorded_at"], reverse=True)
    return result


def _coupon_freq_bucket(coupon_period: int | None) -> str | None:
    """MOEX ISS COUPONPERIOD is days between coupons -- bucket into the
    approximate payment frequency the filter panel offers. Thresholds are
    generous bands around the nominal 30/91/182/365-day periods to absorb
    real-world day-count variance (leap years, non-30-day months), not exact
    calendar-day cutoffs. None when the field itself is unknown (e.g. rows
    from a snapshot taken before this column existed) -- never guessed."""
    if coupon_period is None or coupon_period <= 0:
        return None  # 0/negative -- MOEX reports this for a handful of zero-coupon/discount
                      # instruments with no periodic coupon at all, not a monthly one
    if coupon_period <= 35:
        return "monthly"
    if coupon_period <= 100:
        return "quarterly"
    if coupon_period <= 200:
        return "semiannual"
    return "annual"


def _bonds_for_snap_date(con, snap_date: str, issuer_sectors: dict[str, str]) -> list[dict]:
    """One time-bucket of universe_snapshot, shaped for the screener AND the
    market-map tabs. Coupon type is a best-effort heuristic (COUPONPERCENT
    null -> floater), NOT a reliable classification -- confirmed false
    negatives exist (e.g. some known floaters report a current-period
    percent and read as 'fixed' here). Getting this right needs per-bond
    bondization, which universe_pipeline.py deliberately never calls.
    Surfaced in the UI as a caveat, not hidden.

    Market-map fields (pd_market/pd_rating/pd_gap/mod_dur): pd_market is
    the default probability implied by today's market price alone (same
    g_spread_bp -> PD inversion the mispricing signal already uses, just
    exposed as a probability instead of a spread); pd_rating is the
    agency-implied PD looked up from the same default_probs table
    mispricing_bp is built from, so pd_gap is arithmetically identical
    information to mispricing_bp -- just rescaled for the map's PD axis
    options. mod_dur reuses duration_moex (MOEX's own reported modified
    duration) -- this pipeline never computes duration from cashflows for
    universe-wide bonds, same tradeoff as the g_spread proxy itself.
    """
    rows = db.universe_snapshot_rows(con, snap_date)
    bonds = []
    for r in rows:
        signal_excluded = bool(r["signal_excluded"])
        rating = r["rating"]
        g_spread_bp = r["g_spread_bp"]
        # implied_fair_spread_bp/mispricing_bp are already NULL in the DB
        # when signal_excluded (universe_pipeline.py nulls them before
        # insert) -- but pd_market/pd_rating/pd_gap are recomputed here from
        # the still-present raw rating/g_spread_bp, so they need the same
        # exclusion applied explicitly or they'd silently un-exclude the
        # signal on the PD axis while the bp axis stays correctly excluded.
        pd_rating = lookup(rating)["yr1"] if (rating and not signal_excluded) else None
        pd_market = ((g_spread_bp / 10000.0) / LGD
                     if (g_spread_bp is not None and not signal_excluded) else None)
        pd_gap = (pd_market - pd_rating) if (pd_market is not None and pd_rating is not None) else None
        mod_dur = round(r["duration_moex"] / 365.0, 2) if r["duration_moex"] else None
        bonds.append({
            "secid": r["secid"],
            "short_name": r["short_name"],
            "emitent_title": r["emitent_title"],
            "emitent_inn": r["emitent_inn"],
            "sector": issuer_sectors.get(r["emitent_inn"]),
            "mat_date": r["mat_date"],
            "offer_date": r["offer_date"],
            "coupon_pct": r["coupon_pct"],
            "coupon_type": "fixed" if r["coupon_pct"] is not None else "floater",
            "coupon_period": r["coupon_period"],
            "coupon_freq": _coupon_freq_bucket(r["coupon_period"]),
            "list_level": r["list_level"],
            "qualified": bool(r["qualified"]),
            "last_pct": r["last_pct"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
            "g_spread_bp": round(g_spread_bp, 0) if g_spread_bp is not None else None,
            "duration_yrs": mod_dur,
            "mod_dur": mod_dur,
            "tenor_yrs": round(r["tenor_yrs"], 2) if r["tenor_yrs"] is not None else None,
            "rf_rate_pct": round(r["rf_rate"] * 100, 3) if r["rf_rate"] is not None else None,
            "volume": r["volume"],
            "num_trades": r["num_trades"],
            "yield_source": r["yield_source"],
            "rating": rating,
            "implied_fair_spread_bp": (round(r["implied_fair_spread_bp"], 0)
                                        if r["implied_fair_spread_bp"] is not None else None),
            "mispricing_bp": (round(r["mispricing_bp"], 0)
                              if r["mispricing_bp"] is not None else None),
            "pd_market": round(pd_market, 5) if pd_market is not None else None,
            "pd_rating": round(pd_rating, 5) if pd_rating is not None else None,
            "pd_gap": round(pd_gap, 5) if pd_gap is not None else None,
            "in_watchlist": bool(r["in_watchlist"]),
            "signal_excluded": signal_excluded,
            "vdo_scope": bool(r["vdo_scope"]),
            "aey_base_pct": round(r["aey_base"] * 100, 2) if r["aey_base"] is not None else None,
            "aey_stress_pct": (round(r["aey_stress"] * 100, 2)
                                if r["aey_stress"] is not None else None),
            "aey_bull_pct": round(r["aey_bull"] * 100, 2) if r["aey_bull"] is not None else None,
            # ОНЕГДКП «Рисковый» (captain's Q1): its own column, not the stress
            "aey_risky_pct": (round(r["aey_risky"] * 100, 2)
                               if "aey_risky" in r.keys() and r["aey_risky"] is not None else None),
            "aey_base_risk_adj_pct": (round(r["aey_base_risk_adj"] * 100, 2)
                                       if r["aey_base_risk_adj"] is not None else None),
            "aey_risk_decomposition": _aey_risk_decomposition_pct(r["aey_base"], rating),
            # Task A -- PD-adjusted expected-value inflection
            # (compute/inflection.py), computed and stored per-bond by
            # universe_pipeline.run_universe()'s second pass. yield_zone is
            # one of 'green'/'caution'/'negative_ev'/'unrated'; ev_positive
            # is the pure floor_ytm check (None when unrated/unclassifiable,
            # distinct from False).
            "yield_zone": r["yield_zone"],
            "ev_positive": bool(r["ev_positive"]) if r["ev_positive"] is not None else None,
            # Universe Health Scan (2026-08-29, `hybond scan-universe-health`):
            # health_source distinguishes a real full-signal computation
            # ('full', tier=1 issuers only) from YTM/G-spread-only
            # ('quantitative', everything else) -- see watchlist_health.
            # compute_universe_bond_health(). Both null until that command
            # has run at least once against this snap_date.
            "health_status": r["health_status"],
            "health_source": r["health_source"],
        })
    return bonds


def _aey_risk_decomposition_pct(aey_base: float | None, rating: str | None) -> dict | None:
    """Percent-scaled view of portfolio.aey_risk_decomposition() for export --
    same PD/recovery-rate split that aey_base_risk_adj_pct already blends,
    just exposed as the two scenarios instead of only their weighted sum.
    expected_value_pct always equals aey_base_risk_adj_pct (mod rounding)."""
    d = portfolio.aey_risk_decomposition(aey_base, rating)
    if d is None:
        return None
    return {
        "p_survive_pct": round(d["p_survive"] * 100, 3),
        "return_if_survive_pct": round(d["return_if_survive"] * 100, 2),
        "p_default_pct": round(d["p_default"] * 100, 3),
        "return_if_default_pct": round(d["return_if_default"] * 100, 2),
        "expected_value_pct": round(d["expected_value"] * 100, 2),
    }


# Rating Trajectory task (2026-08-24): a presentation-only numeric scale for
# up/down trend detection on the dashboard, deliberately separate from
# default_probs.RATING_ORDER/rating_rank -- that one is ranked worst-first
# (0=ruAAA) for PD lookups and has no ruD entry (no observed defaults at
# that grade in the Эксперт РА table it's built from). This one is exactly
# the scale specified for the Rating Trajectory task: higher = better,
# ruD=1 included so a full default is still a valid (if extreme) data point.
RATING_TREND_VALUE: dict[str, int] = {
    "ruAAA": 20, "ruAA+": 19, "ruAA": 18, "ruAA-": 17, "ruA+": 16, "ruA": 15, "ruA-": 14,
    "ruBBB+": 13, "ruBBB": 12, "ruBBB-": 11, "ruBB+": 10, "ruBB": 9, "ruBB-": 8,
    "ruB+": 7, "ruB": 6, "ruB-": 5, "ruCCC": 4, "ruCC": 3, "ruC": 2, "ruD": 1,
}


def rating_trend(history: list[dict]) -> dict | None:
    """Trend arrow direction + tooltip from a rating_history list (as built
    by rating_history_by_issuer() below): compares only the LAST transition
    (most recent two entries) for direction -- "last action was an upgrade/
    downgrade" per the task spec -- but the tooltip chains the full
    trajectory. Returns None for an issuer with no rating history at all
    (nothing to show); 'flat'/grey for a single data point or an
    unrecognised rating on either side of the last transition."""
    if not history:
        return None
    tooltip = " → ".join(h["rating"] for h in history) + f" ({history[-1]['agency']})"
    if len(history) < 2:
        return {"direction": "flat", "tooltip": tooltip}
    prev_val = RATING_TREND_VALUE.get(history[-2]["rating"])
    cur_val = RATING_TREND_VALUE.get(history[-1]["rating"])
    if prev_val is None or cur_val is None:
        direction = "flat"
    elif cur_val > prev_val:
        direction = "up"
    elif cur_val < prev_val:
        direction = "down"
    else:
        direction = "flat"
    return {"direction": direction, "tooltip": tooltip}


def rating_history_by_issuer(con, issuer_ids: list[str]) -> dict[str, list[dict]]:
    """All rating rows (every agency, including superseded/is_current=0 --
    the point is the full historical trajectory, not just the current
    winner) for the given issuers, ordered oldest-first. Excludes
    agency='manual' -- that's the watchlist-seed placeholder agency (see
    promotion_audit.py's comment on the same convention), and its rows carry
    a bulk backfill date (2026-07-27 for essentially every issuer) that is
    not a real rating action; including it would show a spurious, often
    wrong-direction "action" on that date for almost every issuer."""
    if not issuer_ids:
        return {}
    placeholders = ",".join("?" * len(issuer_ids))
    out: dict[str, list[dict]] = {}
    for r in con.execute(
        f"SELECT issuer_id, agency, rating, rated_on FROM rating "
        f"WHERE issuer_id IN ({placeholders}) AND agency != 'manual' "
        f"ORDER BY issuer_id, rated_on ASC", issuer_ids):
        out.setdefault(r["issuer_id"], []).append(
            {"date": r["rated_on"], "rating": r["rating"], "agency": r["agency"]})
    return out


def ofz_bundle(con, scenarios: dict) -> dict:
    """Real OFZ data (ofz_pipeline.py, `hybond ofz`) + carry/roll-down
    projections under the same base/stress/bull CBR scenarios used
    everywhere else in this dashboard -- not a separate/hardcoded scenario
    set. `scenarios` is the SAME dict main() already computed once via
    portfolio.get_rate_scenarios(con), passed in rather than recomputed."""
    from hybond import ofz_pipeline

    db.init_ofz(con)
    snap_date = db.latest_ofz_snap_date(con)
    if not snap_date:
        return {"snap_date": None, "bonds": [], "is_fallback": scenarios.get("is_fallback"),
                "rate_scenarios": {}}

    rows = db.ofz_snapshot_rows(con, snap_date)
    projections_by_secid = {
        p["secid"]: p["projections"]
        for p in ofz_pipeline.carry_rolldown_for_snapshot(con, snap_date, scenarios)
    }
    # Ставка и ОФЗ page (2026-09-17): horizon returns priced off each issue's
    # own cash flows (ofz_carry.horizon_return). Additive -- carry_rolldown
    # above stays exactly as the legacy dashboard reads it.
    db.init_rates(con)
    zc = db.latest_zcyc_snapshot(con)
    curve = zc[1] if zc else None
    returns_by_secid = ofz_pipeline.horizon_returns_for_snapshot(con, snap_date, scenarios, curve)

    bonds = []
    for r in rows:
        bonds.append({
            "secid": r["secid"], "short_name": r["short_name"], "isin": r["isin"],
            "board_id": r["board_id"], "instrument_type": r["instrument_type"],
            "mat_date": r["mat_date"], "coupon_pct": r["coupon_pct"],
            "last_pct": r["last_pct"], "dirty_price_pct": r["dirty_price_pct"],
            "ytm_pct": round(r["ytm"] * 100, 2) if r["ytm"] is not None else None,
            "mod_dur": round(r["mod_dur"], 3) if r["mod_dur"] is not None else None,
            "convexity": round(r["convexity"], 3) if r["convexity"] is not None else None,
            "tenor_yrs": r["tenor_yrs"], "volume": r["volume"], "num_trades": r["num_trades"],
            "yield_source": r["yield_source"],
            "carry_rolldown": projections_by_secid.get(r["secid"]),  # None if excluded (near-maturity/no coupon)
            "benchmark": ofz_pipeline.is_benchmark(r, curve),
            "curve_gap_bp": (round(ofz_pipeline.curve_gap_bp(r, curve))
                             if ofz_pipeline.curve_gap_bp(r, curve) is not None else None),
            "returns": returns_by_secid.get(r["secid"]),
        })

    return {
        "snap_date": snap_date,
        "bonds": bonds,
        "rate_scenarios": {
            "base": scenarios.get("base"), "stress": scenarios.get("stress"),
            "bull": scenarios.get("bull"), "source_meeting": scenarios.get("source_date"),
            # Provenance fields for the OFZ tab's Base/Stress/Bull labels
            # (Task 5): the exact meeting date + forecast ranges the base/
            # stress/bull deltas above were derived from, straight from the
            # same get_rate_scenarios() dict -- no second copy, no separate
            # lookup that could drift from it.
            "current_rate": scenarios.get("current_rate"),
            "cbr_2026": scenarios.get("cbr_2026"),
            "cbr_2027": scenarios.get("cbr_2027"),
            "cbr_2029": scenarios.get("cbr_2029"),
        },
        "is_fallback": scenarios.get("is_fallback"),
        # bp deltas every `returns` entry was computed under, per scenario key
        "scenario_deltas_bp": {k: (round(scenarios[k] * 10000) if scenarios.get(k) is not None else None)
                               for k in ofz_pipeline.RETURN_SCENARIOS},
        "max_curve_gap_bp": ofz_pipeline.MAX_CURVE_GAP_BP,
        "min_tenor_for_projection_years": ofz_pipeline.MIN_TENOR_FOR_PROJECTION_YEARS,
        "graduated_shift": ofz_pipeline.ofz_carry.GRADUATED_SHIFT,
    }


def universe_snapshot_bundle(con) -> dict:
    """Three time-buckets of universe data for the market map's time toggle
    (Сейчас / -1 нед. / -1 мес.), plus the derived curve/fair-value overlays.

    universe_snapshot only has history for days `hybond universe` actually
    ran -- on a fresh install that's a single date. Each bucket falls back
    to the nearest available snap_date ON OR BEFORE its target cutoff (never
    a future date relative to the bucket's intent) and reports how many days
    stale that fallback is, so the dashboard can show an honest "no history
    yet" note instead of silently mislabeling today's data as a week old.
    """
    db.init_universe(con)
    latest = db.latest_universe_snap_date(con)
    if not latest:
        empty = {"snap_date": None, "bonds": []}
        return {"universe_now": empty, "universe_week_ago": empty,
                "universe_month_ago": empty, "rf_curve": [], "earliest_snap_date": None,
                "fair_value_bp_by_tier": {}, "yield_inflection_by_rating": {},
                "yield_inflection_ceiling_multiple": None,
                "yield_inflection_caution_multiple": None,
                "yield_inflection_unrated_max_pct": inflection.UNRATED_MAX_YIELD_PCT,
                "yield_inflection_rated_absolute_max_pct": inflection.RATED_ABSOLUTE_MAX_AEY_PCT}

    issuer_sectors = {r["issuer_id"]: r["sector"]
                      for r in con.execute("SELECT issuer_id, sector FROM issuer")}
    latest_d = date.fromisoformat(latest)
    earliest = con.execute(
        "SELECT MIN(snap_date) m FROM universe_snapshot").fetchone()["m"]

    buckets = {}
    for key, days_back in (("universe_now", 0), ("universe_week_ago", 7),
                           ("universe_month_ago", 30)):
        cutoff = (latest_d - timedelta(days=days_back)).isoformat()
        sd = _nearest_snap_date_on_or_before(con, cutoff)
        no_history = False
        if not sd:
            # Not enough accumulated history for this lookback yet -- fall
            # back to the oldest snapshot we do have rather than showing an
            # empty chart; if that's the same day as "now" (fresh install,
            # one day of data total), there's genuinely no comparison point.
            sd = earliest
            no_history = (sd == latest) if days_back > 0 else False
        stale_days = (date.fromisoformat(latest) - date.fromisoformat(sd)).days
        buckets[key] = {
            "snap_date": sd,
            "bonds": _bonds_for_snap_date(con, sd, issuer_sectors),
            "stale_days": stale_days,
            "no_history": no_history,
        }

    # rf_curve: derived from the (tenor_yrs, rf_rate) pairs already
    # interpolated per bond for today's run -- no extra network fetch,
    # always in sync with the curve actually used for today's G-spreads.
    seen = {}
    for b in buckets["universe_now"]["bonds"]:
        if b["tenor_yrs"] is not None and b["rf_rate_pct"] is not None:
            seen[round(b["tenor_yrs"], 1)] = b["rf_rate_pct"]
    rf_curve = [{"tenor_yrs": t, "rf_pct": r} for t, r in sorted(seen.items())]

    fair_value_bp_by_tier = {
        tier[2:]: round(implied_fair_spread_bp(tier), 0)
        for tier in FAIR_VALUE_TIERS
    }

    # Task A -- PD-adjusted expected-value inflection (compute/inflection.py):
    # per-rating-tier floor_ytm/ceiling for the Рынок tab's "Показать предел
    # доходности" overlay. The ceiling/caution multiples must match what
    # universe_pipeline.run_universe() actually used to classify today's
    # bonds' yield_zone -- recomputed here from the SAME live distribution
    # (today's rated, non-excluded bonds' g_spread_bp/implied_fair_spread_bp
    # ratios) rather than re-derived independently, since the per-run
    # multiples themselves aren't persisted anywhere, only their effect
    # (yield_zone/ev_positive per bond).
    now_bonds = buckets["universe_now"]["bonds"]
    ratios = [
        b["g_spread_bp"] / b["implied_fair_spread_bp"]
        for b in now_bonds
        if not b["signal_excluded"] and b["implied_fair_spread_bp"]
        and b["g_spread_bp"] is not None and b["g_spread_bp"] > 0
    ]
    ceiling_mult, caution_mult = inflection.ceiling_multiples_from_distribution(ratios)
    inflection_table = inflection.yield_inflection_by_rating(ceiling_mult, caution_mult)
    yield_inflection_by_rating = {
        rating: {
            "floor_ytm_pct": round(row["floor_ytm"] * 100, 2),
            "ceiling_spread_bp": round(row["ceiling_spread_bp"], 0),
            "caution_spread_bp": round(row["caution_spread_bp"], 0),
        }
        for rating, row in inflection_table.items()
    }

    return {**buckets, "rf_curve": rf_curve, "earliest_snap_date": earliest,
            "fair_value_bp_by_tier": fair_value_bp_by_tier,
            "yield_inflection_by_rating": yield_inflection_by_rating,
            "yield_inflection_ceiling_multiple": round(ceiling_mult, 2),
            "yield_inflection_caution_multiple": round(caution_mult, 2),
            "yield_inflection_unrated_max_pct": inflection.UNRATED_MAX_YIELD_PCT,
            "yield_inflection_rated_absolute_max_pct": inflection.RATED_ABSOLUTE_MAX_AEY_PCT}


def known_issuers_meta(con) -> dict[str, dict]:
    """Every issuer_id already in the `issuer` table -- NOT just the current
    watchlist.json subset (DATA.issuers). The issuer table also holds
    held-back candidates that were investigated and explicitly NOT promoted
    (e.g. Альфа Дон Транс, ЛКХ, ПЛАЗА-Телеком, СМАК -- see
    project_watchlist_flags memory) -- those must not be re-surfaced as
    "new" scan candidates just because they're absent from watchlist.json.

    Also carries current rating (value + rated_on) and filing count/latest
    published date per issuer, so the dashboard's company-scan panel can
    detect "hybond's own data changed since you last scanned this issuer"
    (Part B: new rating captured, new AZIPI filing appeared) without a
    second export round-trip -- a plain snapshot comparison against
    whatever was true at scan time (stored client-side), not a live diff.
    """
    rows = con.execute("SELECT issuer_id, name FROM issuer").fetchall()
    filing_stats = {
        r["issuer_id"]: {"count": r["cnt"], "latest_published": r["latest"]}
        for r in con.execute(
            "SELECT issuer_id, COUNT(*) cnt, MAX(published) latest "
            "FROM filing GROUP BY issuer_id")
    }
    out = {}
    for r in rows:
        rating_row = db.current_rating_row(con, r["issuer_id"])
        fstat = filing_stats.get(r["issuer_id"], {"count": 0, "latest_published": None})
        out[r["issuer_id"]] = {
            "name": r["name"],
            "rating": rating_row["rating"] if rating_row else None,
            "rated_on": rating_row["rated_on"] if rating_row else None,
            "rating_needs_verification": bool(rating_row["needs_verification"]) if rating_row else None,
            "filing_count": fstat["count"],
            "filing_latest_published": fstat["latest_published"],
        }
    return out


def passive_events_since(con, issuer_id: str, since: str) -> list[dict]:
    """Recent classified event/market_note rows for one tier=2 issuer, same
    30-day-window query shape as assemble.py's _events_since_last/
    _market_notes_since_last (Слайс 3), but called directly here rather
    than through build_factlist() -- tier=2 issuers deliberately don't get
    a full factlist built (see passive_issuers_export()'s docstring), so
    reusing the heavy factlist path just to get events would mean building
    bonds/six_pillars/thesis nobody asked for. These two DB helpers are
    already issuer_id-agnostic to tier, so calling them straight from the
    export script is a cheap, self-contained way to feed Лента событий
    without touching the daily pipeline (2026-08-21 event-feed-filtering task)."""
    out = []
    try:
        for r in db.events_since(con, issuer_id, since):
            out.append({"occurred": r["occurred"], "category": r["category"],
                        "severity": r["severity"], "summary": r["summary"], "source": "event"})
    except Exception:  # noqa: BLE001 -- event table may not exist yet
        pass
    try:
        for r in db.market_notes_since(con, issuer_id, since):
            out.append({"occurred": r["occurred"], "category": r["category"],
                        "severity": r["severity"], "summary": r["headline"],
                        "source": r["source"], "url": r["url"]})
    except Exception:  # noqa: BLE001 -- market_note table may not exist yet
        pass
    return out


def passive_issuers_export(con, tier2_rows: list[dict], watchlist: dict, snap_date: str | None) -> list[dict]:
    """Lightweight rows for tier=2 (passive monitoring) issuers -- the
    collapsed Вотчлист section. No verdict/recommendation fields (bonds,
    six_pillars, thesis) by design; those stay tier=1-only. `events` is the
    one exception, added for the event-feed-filtering task (2026-08-21):
    Лента событий was silently tier=1-only even though tier=2 issuers can
    have material classified events too -- see passive_events_since() above.
    `name`/`sector` fall back to watchlist.json when the issuer happens to
    be enriched there, else the bare `issuer` table values."""
    aey_by_inn: dict[str, list[float]] = defaultdict(list)
    if snap_date:
        for r in con.execute(
                "SELECT emitent_inn, aey_base_risk_adj, aey_base FROM universe_snapshot "
                "WHERE snap_date=?", (snap_date,)):
            aey = r["aey_base_risk_adj"] if r["aey_base_risk_adj"] is not None else r["aey_base"]
            if aey is not None:
                aey_by_inn[r["emitent_inn"]].append(aey)
    since = (date.today() - timedelta(days=30)).isoformat()

    out = []
    for row in tier2_rows:
        issuer_id = row["issuer_id"]
        meta = watchlist.get(issuer_id, {})
        rating_row = db.current_rating_row(con, issuer_id)
        aeys = aey_by_inn.get(issuer_id, [])
        out.append({
            "issuer_id": issuer_id,
            "name": meta.get("name") or row["name"],
            "sector": meta.get("sector") or row["sector"],
            "rating": rating_row["rating"] if rating_row else meta.get("rating"),
            "last_aey_base_risk_adj_pct": round(max(aeys) * 100, 2) if aeys else None,
            "events": passive_events_since(con, issuer_id, since),
        })
    return out


def data_notes(con, watchlist: dict) -> dict:
    """Slice status banners, computed live from the store every export run --
    these used to be hand-maintained strings (a stale one claimed synthetic
    day-2 data and an '11 issuers' Slice 4 count long after both were
    overtaken by real runs, 2026-07-30). Regenerating from queries means the
    banner can't drift from reality again the way the strings did."""
    real_dates = [r[0] for r in con.execute(
        "SELECT DISTINCT snap_date FROM spread_snapshot ORDER BY snap_date")]
    synthetic_events = con.execute(
        "SELECT COUNT(*) c FROM event WHERE summary LIKE '%SYNTHETIC%'").fetchone()["c"]

    slice1_slice2 = (
        f"Real data throughout: {len(real_dates)} trading day(s) now in "
        f"spread_snapshot ({real_dates[0]} to {real_dates[-1]}) from live "
        f"`hybond run` pulls -- no synthetic/jittered data remains (the "
        f"original single-day demo, scripts/seed_day2_demo.py, was superseded "
        f"by real runs on those same calendar dates). Floaters with a "
        f"not-yet-fixed next coupon can't be priced from cashflows (see "
        f"pipeline.py) -- those use MOEX's own reported yield as a G-spread "
        f"proxy instead (marked '~'/'*' in the dashboard, yield_source='moex' "
        f"in spread_snapshot). A bond still shows no price only if it had "
        f"zero trades on the snapshot date."
    )

    classified_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM event")}
    filing_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM filing")}
    backlog_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM filing WHERE classified=0")}
    wl_ids = [k for k in watchlist if not k.startswith("_")]
    zero_filing_ids = [i for i in wl_ids if i not in filing_issuers]
    zero_filing_names = ", ".join(
        watchlist[i].get("name", i) for i in zero_filing_ids)

    slice3 = (
        f"{len(classified_issuers)} issuer(s) have at least one real "
        f"CLASSIFIED event (hy-event-classify), {len(backlog_issuers)} "
        f"issuer(s) have real filings scraped from e-disclosure.azipi.ru "
        f"sitting unclassified in the `filing` table (pipeline backlog, not "
        f"missing data -- see `hybond queue`), and {len(zero_filing_ids)} "
        f"issuer(s) genuinely have zero filings scraped at all: "
        f"{zero_filing_names or '(none)'}."
        + (f" {synthetic_events} [SYNTHETIC DEMO] placeholder event(s) still "
           f"remain -- investigate before trusting this note." if synthetic_events
           else " No [SYNTHETIC DEMO] placeholder events remain platform-wide.")
    )

    pillars_issuers = {r["issuer_id"] for r in con.execute(
        "SELECT DISTINCT issuer_id FROM pillars_snapshot")}
    zero_pillars_ids = [i for i in wl_ids if i not in pillars_issuers]
    zero_pillars_names = ", ".join(
        watchlist[i].get("name", i) for i in zero_pillars_ids)

    slice4 = (
        f"Slice 4 financials: real data only. {len(pillars_issuers)} of "
        f"{len(wl_ids)} watchlist issuers have a real ГИР БО financials row "
        f"(parser-api.com, `hybond financials`) feeding hybond.compute.pillars. "
        f"{len(zero_pillars_ids)} issuer(s) have no financials_snapshot/"
        f"pillars_snapshot row: {zero_pillars_names or '(none)'} -- for the "
        f"MFI-regulated names among these this is a confirmed structural gap "
        f"(regulated under CBR, not indexed in ГИР БО), not a fetch failure; "
        f"see their watchlist.json notes. The weekly cron "
        f"(scripts/weekly_financials.sh) that keeps this current was fixed "
        f"2026-07-30 but had not yet reached its first scheduled Sunday run "
        f"as of this export -- check financials_snapshot dates directly "
        f"rather than assuming the cron has already run."
    )

    return {"slice1_slice2": slice1_slice2, "slice3": slice3, "slice4": slice4}


def _aey_source_note(scenarios: dict) -> str:
    if scenarios["is_fallback"]:
        return ("AEY рассчитан по консервативным значениям по умолчанию "
                 "(нет данных последнего заседания ЦБ)")
    d = scenarios["source_date"]
    if scenarios.get("scenario_source") == "cbr_publications":
        return (f"AEY рассчитан по прогнозу ЦБ от {d[8:10]}.{d[5:7]}.{d[0:4]} (база) и сценариям "
                f"ОНЕГДКП на {scenarios.get('horizon_year')} г. (стресс — Проинфляционный, "
                f"бычий — Дезинфляционный)")
    return f"AEY рассчитан по данным заседания ЦБ от {d[8:10]}.{d[5:7]}.{d[0:4]}"


def universe_for_dashboard(con, scenarios: dict) -> dict:
    """Back-compat shape for the existing 'Универсум' screener tab (single
    latest snapshot, no market-map fields it doesn't use)."""
    db.init_universe(con)
    snap_date = db.latest_universe_snap_date(con)
    if not snap_date:
        return {"snap_date": None, "bonds": []}
    issuer_sectors = {r["issuer_id"]: r["sector"]
                      for r in con.execute("SELECT issuer_id, sector FROM issuer")}
    return {
        "snap_date": snap_date,
        "bonds": _bonds_for_snap_date(con, snap_date, issuer_sectors),
        "aey_source_date": scenarios["source_date"],
        "aey_is_fallback": scenarios["is_fallback"],
        "aey_source_note": _aey_source_note(scenarios),
    }


def universe_aey_by_secid(con, snap_date: str | None) -> dict[str, dict]:
    """secid -> {mispricing_bp, aey_base, aey_stress, aey_bull,
    aey_base_risk_adj, yield_zone, ev_positive} from the latest
    universe_snapshot, for enriching the watchlist tab's per-issuer bonds
    (which come from factlists -- a completely separate pipeline from
    universe_pipeline.py). This is a JOIN by secid, not a recomputation --
    there is exactly one AEY calculation in the codebase (portfolio.py), and
    exactly one yield-zone classification (compute/inflection.py, Task A),
    each read from two places. yield_zone/ev_positive feed Task B's verdict
    component on the per-bond detail drawer."""
    if not snap_date:
        return {}
    rows = con.execute(
        "SELECT secid, mispricing_bp, aey_base, aey_stress, aey_bull, aey_base_risk_adj, "
        "yield_zone, ev_positive FROM universe_snapshot WHERE snap_date=?", (snap_date,)).fetchall()
    return {r["secid"]: dict(r) for r in rows}


def cbr_export(con, scenarios: dict) -> dict:
    """CBR key-rate meeting tracker, shaped for the dashboard's fourth tab.
    Reads cbr_meeting + cbr_meeting_diff only -- no computation happens
    here, `hybond cbr --fetch-all`/`--diff-all` (cbr_tracker.py) already did
    it. current_rate/next_meeting/cbr_2026_forecast/cbr_2027_forecast come
    straight from cbr_meeting's own fields (rate_after, rate_2026_mid,
    rate_2027_mid) rather than analysis_json's cbr_2026_avg_forecast/
    cbr_2027_avg_forecast, which still carry a previously-flagged factual
    mismatch against the real forecast documents (analysis_json was seeded
    verbatim from a user-authored draft that predates that correction) --
    cbr_meeting's own extracted fields are the verified source of truth."""
    from hybond import cbr_tracker

    db.init_db(con)
    db.init_cbr(con)
    db.init_cbr_diff(con)

    today = date.today().isoformat()
    # The stored CBR calendar (cbr_tracker.get_schedule), limited to this
    # year onward so the meeting timeline stays the same shape it had when
    # it read the hardcoded 2026 list -- and does not go empty on 1 January.
    year_start = f"{date.today().year}-01-01"
    schedule = [m for m in cbr_tracker.get_schedule(con) if m["date"] >= year_start]

    latest = con.execute(
        "SELECT * FROM cbr_meeting WHERE rate_after IS NOT NULL "
        "ORDER BY meeting_date DESC LIMIT 1").fetchone()
    latest = dict(latest) if latest else None

    next_meeting = next((m["date"] for m in schedule if m["date"] > today), None)

    def _normalise_range(s):
        """"14,5–14,61" -> "14.5-14.61" -- display-only normalisation to the
        dashboard's Latin-decimal convention; does NOT touch the trailing
        digit itself (see cbr_tracker.py's module docstring on the
        occasional fused-footnote-marker caveat -- that's a data-quality
        note for whoever reads the number, not something to silently trim)."""
        return s.replace(",", ".").replace("–", "-").replace("—", "-") if s else s

    # Deltas come from portfolio.get_rate_scenarios(con) -- the SAME live
    # computation universe_pipeline.py uses for per-bond AEY -- rather than
    # analysis_json's static aey_delta_rate_* fields. Reading two different
    # sources for what should be one number was a correctness bug: this
    # tab and the universe/portfolio view could show different scenario
    # deltas for the same meeting date. is_fallback is always surfaced
    # (never silently swallowed) so the dashboard can show a warning badge
    # instead of presenting a fallback constant as a real forecast-derived one.
    rate_scenarios = {
        "base_annual_bp": round(scenarios["base"] * 10000),
        "stress_annual_bp": round(scenarios["stress"] * 10000),
        "bull_annual_bp": round(scenarios["bull"] * 10000),
        "source_meeting": scenarios["source_date"],
        "cbr_2026_forecast": _normalise_range(scenarios["cbr_2026"]),
        "cbr_2027_forecast": _normalise_range(scenarios["cbr_2027"]),
        "is_fallback": scenarios["is_fallback"],
    }

    meetings = []
    for m in schedule:
        row = con.execute("SELECT * FROM cbr_meeting WHERE meeting_date=?",
                          (m["date"],)).fetchone()
        row = dict(row) if row else {}
        diff_row = con.execute("SELECT * FROM cbr_meeting_diff WHERE meeting_date=?",
                               (m["date"],)).fetchone()
        diff = None
        if diff_row:
            diff_row = dict(diff_row)
            diff = {
                "pace_change": diff_row.get("pace_change"),
                "forecast_2026_shift_bp": diff_row.get("forecast_2026_shift_bp"),
                "forecast_2027_shift_bp": diff_row.get("forecast_2027_shift_bp"),
                "portfolio_signal": diff_row.get("portfolio_signal"),
                "signal_magnitude": diff_row.get("signal_magnitude"),
                "tone_shift": diff_row.get("tone_shift"),
                "diff_note_ru": diff_row.get("diff_note_ru"),
            }
        meetings.append({
            "date": m["date"],
            "is_anchor": m["anchor"],
            "resume_date": m.get("resume_date"),
            "rate_after": row.get("rate_after"),
            "rate_change_bp": row.get("rate_change_bp"),
            "is_past": m["date"] <= today,
            "has_press": bool(row.get("press_release_text")),
            "has_forecast": bool(row.get("forecast_summary")),
            "press_release_excerpt": (row.get("press_release_text") or "")[:400] or None,
            "press_release_url": (f"https://cbr.ru/press/pr/?file="
                                   f"{m['date'][8:10]}{m['date'][5:7]}{m['date'][0:4]}_133000key.htm"
                                   if row.get("press_release_text") else None),
            "diff": diff,
        })

    recent_diffs = []
    for r in con.execute(
            "SELECT * FROM cbr_meeting_diff ORDER BY meeting_date DESC LIMIT 3").fetchall():
        r = dict(r)
        recent_diffs.append({
            "date": r["meeting_date"],
            "note": r.get("diff_note_ru"),
            "signal": (r.get("portfolio_signal") or "neutral").upper(),
            "signal_magnitude": r.get("signal_magnitude"),
        })

    return {
        "current_rate": latest.get("rate_after") if latest else None,
        "next_meeting": next_meeting,
        "rate_scenarios": rate_scenarios,
        "meetings": meetings,
        "recent_diffs": recent_diffs,
    }


# ---- Ставка и ОФЗ page (plan rev 2, approved 2026-09-17) --------------------

EXPECTATION_SOURCE_NAMES = {
    "ondkp": "Сценарии ЦБ · ОНЕГДКП",
    "cbr_forecast": "Прогноз ЦБ · опорные заседания",
    "survey": "Макроопрос · аналитики",
    "documents": "Решения, заявления, резюме",
    "calendar": "Календарь заседаний",
    "key_rate": "Ключевая ставка по дням",
}
SCENARIO_ORDER = ("base", "disinflation", "proinflation", "risk")
# The page's scenario pills, in order, and which pipeline scenario key each is.
PAGE_SCENARIOS = (
    ("base", "Базовый", "cbr_forecast"),
    ("bull", "Дезинфляционный", "ondkp"),
    ("stress", "Проинфляционный", "ondkp"),
    ("risky", "Рисковый", "ondkp"),
    ("analysts", "Аналитики", "survey"),
)
EXPECTATION_HISTORY_FROM = "2024-01-01"


def _rng(lo, hi) -> list[float] | None:
    return None if lo is None or hi is None else [round(lo, 2), round(hi, 2)]


def expectations_export(con, scenarios: dict) -> dict:
    """DATA.expectations: what the CBR and the surveyed analysts expect for
    the key rate, with history (hybond expectations -> rate_expectation,
    cbr_document, key_rate_daily, cbr_schedule). Every publication carries
    the official URL it was read from; `sources` carries each source's page
    and the date of its last good fetch, so the page shows how fresh each
    one is instead of going blank when a fetch fails.

    Survey history is trimmed to the years from (current year - 1) on --
    older years are finished and nothing on the page plots them."""
    from hybond import cbr_tracker, expectations

    db.init_rates(con)
    db.init_cbr(con)
    today = date.today()
    min_year = today.year - 1

    forecasts: dict[tuple, dict] = {}
    for r in db.rate_expectations(con, "cbr_forecast"):
        f = forecasts.setdefault((r["edition"], r["published"]), {
            "meeting_date": r["edition"], "published": r["published"], "url": r["url"],
            "years": {}, "rest": None})
        if r["period"] == "rest_of_year":
            f["rest"] = {"year": r["year"], "lo": r["lo"], "hi": r["hi"], "ytd_avg": r["ytd_avg"],
                         "rest_from": r["rest_from"]}
        else:
            f["years"][str(r["year"])] = _rng(r["lo"], r["hi"])

    ondkp: dict[tuple, dict] = {}
    for r in db.rate_expectations(con, "ondkp"):
        e = ondkp.setdefault((r["edition"], r["published"], r["status"]), {
            "edition": r["edition"], "published": r["published"], "status": r["status"], "url": r["url"],
            "scenarios": {k: {} for k in SCENARIO_ORDER}})
        e["scenarios"].setdefault(r["scenario"], {})[str(r["year"])] = _rng(r["lo"], r["hi"])

    surveys: dict[str, dict] = {}
    survey_url = None
    for r in db.rate_expectations(con, "survey"):
        survey_url = r["url"] or survey_url
        if r["year"] < min_year:
            continue
        sv = surveys.setdefault(r["edition"], {"period": r["edition"], "years": {}})
        sv["years"].setdefault(str(r["year"]), {})[r["scenario"]] = round(r["lo"], 3)

    docs = db.cbr_documents(con)
    by_meeting: dict[str, dict[str, dict]] = defaultdict(dict)
    for d in docs:
        by_meeting[d["meeting_date"]][d["doc_type"]] = d
    decisions, summaries = [], []
    for meeting, kinds in sorted(by_meeting.items(), reverse=True):
        if meeting < EXPECTATION_HISTORY_FROM:
            continue
        pr = kinds.get("press_release")
        if pr:
            row = db.get_cbr_meeting(con, meeting)
            before, after, change = cbr_tracker.extract_rate_decision(pr["title"] or "")
            if after is None and pr["text"]:
                before, after, change = cbr_tracker.extract_rate_decision(pr["text"])
            if after is None and row is not None:
                after, change = row["rate_after"], row["rate_change_bp"]
            decisions.append({
                "date": meeting, "rate": after, "change_bp": change, "title": pr["title"],
                "signal": expectations.extract_signal(pr["text"]),
                "url": pr["url"], "statement_url": (kinds.get("statement") or {}).get("url"),
                "forecast_url": (kinds.get("forecast") or {}).get("url"),
            })
        sm = kinds.get("summary")
        if sm:
            parts = expectations.extract_summary_options(sm["text"])
            summaries.append({"meeting_date": meeting, "published": sm["published"], "url": sm["url"],
                              "options": parts["options"], "positions": parts["positions"]})

    steps = db.key_rate_steps(con, since=EXPECTATION_HISTORY_FROM)

    stored_sources = db.expectation_sources(con)
    sources = []
    for key, name in EXPECTATION_SOURCE_NAMES.items():
        row = stored_sources.get(key, {})
        sources.append({"key": key, "name": name,
                        "page_url": row.get("page_url") or expectations.SOURCE_PAGES.get(key),
                        "last_published": row.get("last_published"), "last_ok": row.get("last_ok"),
                        "last_error": row.get("last_error"), "last_error_at": row.get("last_error_at")})

    levels = scenarios.get("levels") or {}
    page_scenarios = []
    for key, label, source in PAGE_SCENARIOS:
        lv = levels.get(key)
        page_scenarios.append({
            "key": key, "label": label, "source": source,
            "range": _rng(lv["lo"], lv["hi"]) if lv else None,
            "delta_bp": round(scenarios[key] * 10000) if scenarios.get(key) is not None else None,
            "published": lv["published"] if lv else None,
            "edition": lv["edition"] if lv else None,
            "status": lv["status"] if lv else None,
            "url": lv["url"] if lv else None,
        })

    return {
        "as_of": today.isoformat(),
        "scenario_source": scenarios.get("scenario_source"),
        "horizon_year": scenarios.get("horizon_year"),
        "current_rate": scenarios.get("current_rate"),
        "current_rate_date": scenarios.get("current_rate_date"),
        "scenarios": page_scenarios,
        "key_rate_steps": [[d, r] for d, r in steps],
        "forecasts": sorted(forecasts.values(), key=lambda f: f["published"]),
        "ondkp": sorted(ondkp.values(), key=lambda e: e["published"]),
        "surveys": sorted((s for s in surveys.values() if s["years"]), key=lambda s: s["period"]),
        "survey_url": survey_url,
        "decisions": decisions,
        "summaries": summaries,
        "schedule": [m for m in cbr_tracker.get_schedule(con) if m["date"] >= f"{today.year}-01-01"],
        "sources": sources,
    }


def zcyc_export(con) -> dict:
    """DATA.zcyc: the latest stored CBR zero-coupon curve, rates in percent."""
    db.init_rates(con)
    latest = db.latest_zcyc_snapshot(con)
    if not latest:
        return {"snap_date": None, "points": []}
    snap, pts = latest
    return {"snap_date": snap, "points": [[t, round(r * 100, 3)] for t, r in pts]}


OFZ_DURATION_MATCH_MAX_DIFF_YEARS = 2.0  # same limit the old dashboard's premium used
OFZ_BENCHMARK_MIN_TENOR_YEARS = 1.0


def rates_premium_export(con, scenarios: dict, portfolios: list[dict], ofz_bonds: list[dict]) -> dict:
    """DATA.rates_premium: each preset portfolio's AEY after credit losses
    against the nearest-duration nominal OFZ, per scenario, with ONE formula
    on both legs -- portfolio.bond_aey() (YTM minus modified duration times
    the scenario shift), PD-weighted through pd_adjusted_aey() for the
    corporate leg only.

    The old «Премия к ОФЗ» subtracted an OFZ carry-per-year number (coupon /
    price with 40% of the shift and no pull to par) from a corporate AEY with
    the full shift applied at once, which overstated the premium by a median
    10.8 p.p. across green-zone bonds (plan finding P1).

    Positions resolve against the latest universe_snapshot, the same rows the
    Портфель page reads. The OFZ leg is a benchmark-grade issue (ПД/АД, YTM
    solved on its cash flows, at least a year to maturity) within
    OFZ_DURATION_MATCH_MAX_DIFF_YEARS of the portfolio's weighted duration;
    beyond that the comparison is not made."""
    snap = db.latest_universe_snap_date(con)
    rows = {r["secid"]: r for r in db.universe_snapshot_rows(con, snap)} if snap else {}
    candidates = [o for o in ofz_bonds if o.get("benchmark") and (o.get("tenor_yrs") or 0) >= OFZ_BENCHMARK_MIN_TENOR_YEARS]
    keys = [k for k, _, _ in PAGE_SCENARIOS if scenarios.get(k) is not None]
    out = []
    for pf in portfolios:
        positions = (pf.get("allocation") or {}).get("bonds") or []
        dur_num = dur_den = 0.0
        covered = 0
        aey_sums = {k: [0.0, 0.0] for k in keys}
        for pos in positions:
            r = rows.get(pos.get("isin"))
            w = pos.get("weight") or 0
            if r is None or not w:
                continue
            mdur = r["duration_moex"] / 365.0 if r["duration_moex"] else None
            if mdur is not None:
                dur_num += mdur * w
                dur_den += w
            if r["signal_excluded"] or r["ytm"] is None or mdur is None or not r["rating"]:
                continue
            ok = False
            for k in keys:
                a = portfolio.pd_adjusted_aey(portfolio.bond_aey(r["ytm"], mdur, scenarios[k]), r["rating"], con=con)
                if a is not None:
                    aey_sums[k][0] += a * w
                    aey_sums[k][1] += w
                    ok = True
            covered += 1 if ok else 0
        avg_dur = dur_num / dur_den if dur_den else None
        bench = None
        if avg_dur is not None and candidates:
            best = min(candidates, key=lambda o: abs(o["mod_dur"] - avg_dur))
            if abs(best["mod_dur"] - avg_dur) <= OFZ_DURATION_MATCH_MAX_DIFF_YEARS:
                bench = best
        by_scenario = {}
        for k in keys:
            num, den = aey_sums[k]
            pf_aey = num / den * 100 if den else None
            ofz_aey = (portfolio.bond_aey(bench["ytm_pct"] / 100, bench["mod_dur"], scenarios[k]) * 100
                       if bench else None)
            by_scenario[k] = {
                "pf_aey_pct": round(pf_aey, 2) if pf_aey is not None else None,
                "ofz_aey_pct": round(ofz_aey, 2) if ofz_aey is not None else None,
                "premium_pp": round(pf_aey - ofz_aey, 2) if pf_aey is not None and ofz_aey is not None else None,
            }
        out.append({
            "id": pf["id"], "name": pf["name"], "preset_type": pf["preset_type"],
            "positions": len(positions), "covered": covered,
            "avg_mod_dur": round(avg_dur, 2) if avg_dur is not None else None,
            "ofz": ({"secid": bench["secid"], "short_name": bench["short_name"], "ytm_pct": bench["ytm_pct"],
                     "mod_dur": bench["mod_dur"], "mat_date": bench["mat_date"]} if bench else None),
            "by_scenario": by_scenario,
        })
    return {"match_max_diff_years": OFZ_DURATION_MATCH_MAX_DIFF_YEARS, "universe_snap_date": snap,
            "portfolios": out}


def company_scan_history_export(con) -> list[dict]:
    """Latest company_scan_result row per ИНН (src/hybond/company_scan_pipeline.py,
    `hybond scan-company`/`scan-candidates`) -- lets the dashboard's
    "Проверить компанию" panel render a past scan straight from the DB, no
    fresh API call needed. Free-text queries that never resolved to an ИНН
    (see company_scan_pipeline.resolve_inn) aren't trackable this way and
    are excluded here, same limitation as the browser's own localStorage
    history -- they simply don't appear in this list.

    json_addendum is stored as a TEXT blob in the DB (see
    db.insert_company_scan_result) -- parsed back into a real object here so
    the dashboard can feed it straight into renderNarrativeCardHtml() without
    a second JSON.parse of a JSON string inside JSON."""
    db.init_company_scan(con)
    latest = db.latest_company_scan_by_inn(con)
    out = []
    for inn, row in sorted(latest.items(), key=lambda kv: kv[1].get("scanned_at") or "", reverse=True):
        addendum = row.get("json_addendum")
        card = None
        if addendum:
            try:
                card = json.loads(addendum)
            except json.JSONDecodeError:
                card = None
        out.append({
            "id": row["id"], "inn": inn, "query": row["query"],
            "issuer_name": row.get("issuer_name") or row["query"],
            "scanned_at": row["scanned_at"], "model": row.get("model"),
            "issuer_quality": row.get("issuer_quality"),
            "bond_suitability": row.get("bond_suitability"),
            "confidence": row.get("confidence"),
            "data_coverage_pct": row.get("data_coverage_pct"),
            "raw_markdown": row.get("raw_markdown"),
            "card": card,
            "input_tokens": row.get("input_tokens"), "output_tokens": row.get("output_tokens"),
            "cache_read_tokens": row.get("cache_read_tokens"), "web_searches": row.get("web_searches"),
        })
    return out


def main() -> None:
    watchlist = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    watchlist = {k: v for k, v in watchlist.items() if not k.startswith("_")}

    report = json.loads((ROOT / "data" / "report.json").read_text(encoding="utf-8"))
    factlists = latest_factlists()

    con = db.connect()
    db.init_db(con)
    db.init_pipeline_health(con)
    cleanup_export_stale_duplicates(con)
    db.init_cbr(con)
    scenarios = portfolio.get_rate_scenarios(con)  # single computation, shared below
    universe = universe_for_dashboard(con, scenarios)
    market_map = universe_snapshot_bundle(con)
    ofz = ofz_bundle(con, scenarios)
    cbr = cbr_export(con, scenarios)
    fhist = flag_history(con)
    shist = spread_history_by_secid(con)
    # Tier=1 (active watchlist, full verdict card) drives the main issuer
    # loop below; tier=2 (passive monitoring) gets a lightweight collapsed
    # row instead -- see passive_issuers_export(). The `issuer` DB table is
    # the tier source of truth (Watchlist Tier System task, 2026-08-18);
    # watchlist.json remains an enrichment lookup for risk_tier/note/
    # edisclosure_id per issuer_id, not the list of which issuers appear.
    tier1_rows = db.list_issuers_by_tier(con, 1)
    tier2_rows = db.list_issuers_by_tier(con, 2)
    # health_status/health_reason (Watchlist Health Monitoring task,
    # 2026-08-24): list_issuers_by_tier()'s SELECT is shared by several
    # callers, so this is a separate lookup rather than widening that query.
    health_by_issuer = {
        r["issuer_id"]: {"health_status": r["health_status"], "health_reason": r["health_reason"]}
        for r in con.execute(
            "SELECT issuer_id, health_status, health_reason FROM issuer "
            "WHERE watchlist_tier = 1")
    }
    # pillars_date (Analytical Status Metadata task, 2026-08-24): last date
    # ANY pillars_snapshot was computed for the issuer, regardless of fiscal
    # period -- MAX(computed_on) across all period rows, not tied to a
    # specific report.
    pillars_date_by_issuer = {
        r["issuer_id"]: r["computed_on"]
        for r in con.execute(
            "SELECT issuer_id, MAX(computed_on) AS computed_on FROM pillars_snapshot "
            "GROUP BY issuer_id")
    }
    # rating_history/rating_trend (Rating Trajectory task, 2026-08-24).
    rating_history_by_issuer_map = rating_history_by_issuer(
        con, [r["issuer_id"] for r in tier1_rows])
    spread_history, spread_history_labels = spread_history_by_issuer(
        con, [r["issuer_id"] for r in tier1_rows], date.today().isoformat())
    fcounts = filing_counts_by_issuer(con)
    aey_by_secid = universe_aey_by_secid(con, universe["snap_date"])
    convexity_by_secid_map = convexity_by_secid(con)
    mis_delta_by_secid = mispricing_delta_by_secid(con)
    events_full_by_issuer = full_events_by_issuer(con)
    market_wide = market_wide_notes(con)
    placements = primary_placements(con, date.today().isoformat())
    expected_returns = expected_returns_by_secid(con)
    portfolio_freshness = portfolio_data_freshness(con)
    portfolio_allocation = export_portfolio_allocation(con)
    all_active_portfolios = export_all_active_portfolios(con)
    expectations_data = expectations_export(con, scenarios)
    zcyc = zcyc_export(con)
    rates_premium = rates_premium_export(con, scenarios, all_active_portfolios, ofz["bonds"])
    pd_table_date_iso = db.latest_pd_table_date(con)
    pd_table_date = (pd_import.format_report_date(pd_table_date_iso)
                      if pd_table_date_iso else None)
    # MOEX defaults registry (2026-08-29): grouped by inn for the O(1)
    # per-issuer lookup below, rather than one query per tier=1 issuer.
    # init_moex_default() first -- this export must not crash on a DB where
    # `hybond moex-defaults import` has never been run.
    db.init_moex_default(con)
    moex_defaults_by_issuer: dict[str, list[dict]] = defaultdict(list)
    for r in moex_defaults.get_watchlist_defaults(con):
        moex_defaults_by_issuer[r["inn"]].append(r)

    issuers = []
    for issuer_row in tier1_rows:
        issuer_id = issuer_row["issuer_id"]
        meta = watchlist.get(issuer_id, {})
        fl = factlists.get(issuer_id, {})
        thesis_row = db.get_thesis(con, issuer_id)
        thesis = None
        if thesis_row:
            thesis = {
                "thesis": thesis_row["thesis"],
                "assumptions": thesis_row["assumptions"],
                "invalidation": thesis_row["invalidation"],
                "status": thesis_row["status"],
                "updated": thesis_row["updated"],
            }
        rating_row = db.current_rating_row(con, issuer_id)
        rating_needs_verification = bool(rating_row["needs_verification"]) if rating_row else False
        ratings_all = db.all_current_ratings(con, issuer_id)
        rating_detail = None
        if rating_row:
            rated_on = rating_row["rated_on"]
            is_stale = None
            if rated_on:
                try:
                    is_stale = db.rating_is_stale(rated_on)
                except ValueError:
                    is_stale = None
            rating_detail = {
                "agency": rating_row["agency"], "rated_on": rated_on,
                "outlook": rating_row["outlook"], "action": rating_row["action"],
                "is_stale": is_stale,
            }

        bonds = fl.get("bonds", [])
        wall_by_secid = report.get("maturity_wall_by_secid", {})
        for b in bonds:
            b["spread_history"] = shist.get(b["secid"], [])
            # Real per-bond monthly coupon/principal buckets (pipeline.py,
            # same build_wall() the watchlist-wide maturity wall uses, just
            # scoped to this one secid's own cashflows) -- feeds the
            # Портфель tab's weight-scaled combined wall. Empty for floaters
            # (no fixed schedule, same gap the watchlist-wide wall already
            # has) or if `hybond run` hasn't been re-run since this field
            # was added.
            b["maturity_wall"] = wall_by_secid.get(b["secid"], [])
            aey = aey_by_secid.get(b["secid"])
            b["mispricing_bp"] = round(aey["mispricing_bp"], 0) if aey and aey["mispricing_bp"] is not None else None
            b["mispricing_trend"] = mis_delta_by_secid.get(
                b["secid"], {"delta_bp": None, "direction": None})
            b["aey_base_pct"] = round(aey["aey_base"] * 100, 2) if aey and aey["aey_base"] is not None else None
            b["aey_stress_pct"] = round(aey["aey_stress"] * 100, 2) if aey and aey["aey_stress"] is not None else None
            b["aey_bull_pct"] = round(aey["aey_bull"] * 100, 2) if aey and aey["aey_bull"] is not None else None
            b["aey_base_risk_adj_pct"] = (round(aey["aey_base_risk_adj"] * 100, 2)
                                          if aey and aey["aey_base_risk_adj"] is not None else None)
            b["aey_risk_decomposition"] = _aey_risk_decomposition_pct(
                aey["aey_base"] if aey else None, meta.get("rating"))
            b["expected_return"] = expected_returns.get(b["secid"], {})
            # Task B -- verdict component on the per-bond detail drawer:
            # yield_zone/ev_positive straight from universe_snapshot (Task
            # A), same JOIN pattern as the AEY fields just above.
            b["yield_zone"] = aey["yield_zone"] if aey else None
            b["ev_positive"] = (bool(aey["ev_positive"])
                                 if aey and aey["ev_positive"] is not None else None)
            b["convexity"] = convexity_by_secid_map.get(b["secid"])
            # Registered-vs-placed volume (Task 3 follow-up): placement_pct/
            # placement_partial_since are raw facts from the bond table
            # (pipeline.py/db.upsert_bond); is_placement_stalled is the same
            # interpretive-flag-computed-at-export-time pattern rating_detail's
            # is_stale already uses (db.rating_is_stale above), not baked into
            # the factlist itself.
            b["is_placement_stalled"] = db.placement_is_stalled(
                b.get("placement_pct"), b.get("placement_partial_since"))

        issuers.append({
            "issuer_id": issuer_id,
            "name": meta.get("name") or issuer_row["name"],
            "sector": meta.get("sector") or issuer_row["sector"],
            "risk_tier": meta.get("risk_tier", "core"),
            "rating": meta.get("rating"),
            "rating_needs_verification": rating_needs_verification,
            "rating_detail": rating_detail,
            "ratings_all": ratings_all,
            "data_conflicts": data_conflicts_for_issuer(con, issuer_id, ratings_all),
            "note": meta.get("note"),
            "edisclosure_id": meta.get("edisclosure_id"),
            "snap_date": fl.get("snap_date"),
            "bonds": bonds,
            "flags_today": fl.get("flags", []),
            "flag_history": fhist.get(issuer_id, []),
            "stat_warming_up": fl.get("stat_warming_up", True),
            "events": fl.get("events_since_last", []),
            "market_notes": fl.get("market_notes_since_last", []),
            "events_full": events_full_by_issuer.get(issuer_id, []),
            "six_pillars": fl.get("six_pillars", {}),
            "six_pillars_delta": fl.get("six_pillars_delta", {}),
            "thesis": thesis,
            "filing_counts": fcounts.get(issuer_id, {"total": 0, "classified": 0}),
            "health_status": health_by_issuer.get(
                issuer_id, {"health_status": "green"})["health_status"],
            "health_reason": health_by_issuer.get(
                issuer_id, {"health_reason": None})["health_reason"],
            # Analytical Status Metadata task (2026-08-24): pillars_date is a
            # fresh MAX(computed_on) lookup (pillars_snapshot has no other
            # home in this loop); rating_date/verified_on reuse rating_row,
            # already fetched above for rating_detail -- verified_on is
            # rating.verified_on (when THIS rating was last cross-checked),
            # not a separate issuer-level field, which the schema has no
            # equivalent of.
            "pillars_date": pillars_date_by_issuer.get(issuer_id),
            "rating_date": rating_row["rated_on"] if rating_row else None,
            "verified_on": rating_row["verified_on"] if rating_row else None,
            "rating_history": rating_history_by_issuer_map.get(issuer_id, []),
            "rating_trend": rating_trend(rating_history_by_issuer_map.get(issuer_id, [])),
            "moex_defaults": moex_defaults_by_issuer.get(issuer_id, []),
        })
    issuers_passive = passive_issuers_export(con, tier2_rows, watchlist, universe.get("snap_date"))
    notes = data_notes(con, watchlist)
    known_issuers = known_issuers_meta(con)
    company_scan_history = company_scan_history_export(con)
    queue_count_val = queue_count(con)
    latest_spread = _latest_spread_snap_date(con)
    pipeline_health = pipeline_health_export(con, latest_spread)

    out = {
        "generated_at": report.get("snap_date"),
        "day1_snap_date": market_map["earliest_snap_date"],
        "day2_snap_date": latest_spread,
        # Wall-clock time this export ran, distinct from day2_snap_date (the
        # DB's data snapshot date) -- one answers "how fresh is the HTML
        # page", the other "how fresh is the underlying data". Deliberately
        # NOT used to override day2_snap_date anywhere: doing so would hide
        # genuine data staleness behind an always-current-looking date.
        "rebuild_at": datetime.now().isoformat(),
        "queue_count": queue_count_val,
        # PD/LGD Recalibration task (2026-08-16): dashboard-visible provenance
        # for the PD table + LGD assumption everything AEY-adjacent derives
        # from -- see default_probs.py's module docstring for the full
        # source/methodology notes this summarises.
        "pd_table_meta": {
            "source": "Эксперт РА",
            "dated": "2026-07-01",
            "applied_on": "2026-08-16",
            "lgd": round(LGD, 3),
            "recovery_rate": RECOVERY_RATE,
        },
        "spread_screen_day1": report.get("spread_screen", []),
        "maturity_wall": report.get("maturity_wall", []),
        "redemptions_by_issuer": report.get("redemptions_by_issuer", {}),
        "issuers": issuers,
        "issuers_passive": issuers_passive,
        "aey_source_note": _aey_source_note(scenarios),
        "aey_is_fallback": scenarios["is_fallback"],
        "universe": universe,
        "universe_now": market_map["universe_now"],
        "universe_week_ago": market_map["universe_week_ago"],
        "universe_month_ago": market_map["universe_month_ago"],
        "rf_curve": market_map["rf_curve"],
        "fair_value_bp_by_tier": market_map["fair_value_bp_by_tier"],
        # Task A -- PD-adjusted expected-value inflection (compute/
        # inflection.py): per-rating floor/ceiling for the Рынок tab's
        # "Показать предел доходности" overlay, plus the live multiples used
        # so the chart can show its own calibration transparently.
        "yield_inflection_by_rating": market_map["yield_inflection_by_rating"],
        "yield_inflection_ceiling_multiple": market_map["yield_inflection_ceiling_multiple"],
        "yield_inflection_caution_multiple": market_map["yield_inflection_caution_multiple"],
        "yield_inflection_unrated_max_pct": market_map["yield_inflection_unrated_max_pct"],
        "yield_inflection_rated_absolute_max_pct": market_map["yield_inflection_rated_absolute_max_pct"],
        # Task B verdict component, Layer 3 (pro-only): "PD table value
        # used" -- the raw 1yr PD (%) default_probs.lookup() reads for a
        # bond's rating, same table pd_adjusted_aey/mispricing_bp already
        # use, just exposed directly instead of only through derived bp/pp
        # figures.
        "pd_by_rating": {rating: round(probs["yr1"] * 100, 3)
                          for rating, probs in DEFAULT_PROBS.items()},
        "ofz": ofz,
        # Single source of truth for the dashboard's "Топ-кандидаты" toggle
        # (Портфельный рейтинг table) -- sourced directly from promote.py's
        # own defaults and default_probs' own rating scale, not hand-copied
        # into JS, so the live-in-browser filter can never silently drift
        # from what `hybond promote` itself would report. Thresholds are a
        # starting point, not final policy -- see promote.py's docstring.
        "promotion_thresholds": {
            "rating_floor": promote.RATING_FLOOR_DEFAULT,
            "n_before_cap": promote.N_BEFORE_CAP_DEFAULT,
            "per_issuer_cap": promote.PER_ISSUER_CAP_DEFAULT,
            "target_after_cap": promote.TARGET_AFTER_CAP_DEFAULT,
            "min_volume": VDO_MIN_VOLUME,
        },
        "rating_order": RATING_ORDER,
        "placement_thresholds": {
            "stall_weeks": db.PLACEMENT_STALL_WEEKS,
            "stall_threshold_pct": db.PLACEMENT_STALL_THRESHOLD_PCT,
            "full_threshold_pct": db.PLACEMENT_FULL_THRESHOLD_PCT,
        },
        "cbr": cbr,
        # Ставка и ОФЗ page (plan rev 2, 2026-09-17) -- additive keys only.
        "expectations": expectations_data,
        "zcyc": zcyc,
        "rates_premium": rates_premium,
        "data_notes": notes,
        "known_issuers": known_issuers,
        "company_scan_history": company_scan_history,
        "spread_history": spread_history,
        "spread_history_labels": spread_history_labels,
        "portfolio_data_freshness": portfolio_freshness,
        "portfolio_allocation": portfolio_allocation,
        "portfolios": all_active_portfolios,
        "pd_table_date": pd_table_date,
        "moex_defaults": {
            "watchlist_matches": moex_defaults.get_watchlist_defaults(con),
            "total_events": con.execute(
                "SELECT COUNT(*) FROM moex_default").fetchone()[0],
            "imported_on": con.execute(
                "SELECT MAX(imported_on) FROM moex_default").fetchone()[0],
        },
        "market_wide_notes": market_wide,
        "primary_placements": placements,
        # Справка tab (2026-08-17): parsed once at build time from
        # spravka_vdo.md -- see hybond/spravka_parser.py. Dashboard is
        # self-contained (no runtime fetch), so the whole guide ships in
        # this JSON. Absent (not an error) if the source file isn't present.
        "spravka": (spravka_parser.parse(SPRAVKA_MD_PATH.read_text(encoding="utf-8"))
                    if SPRAVKA_MD_PATH.exists() else None),
        "sector_labels": SECTOR_LABELS_RU,
        "pipeline_health": pipeline_health,
    }
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    if len(payload.encode("utf-8")) < 10_000:
        # health logging
        log_health(con, 'dashboard', 'export', 'error', 'EXPORT_EMPTY',
                   f'dashboard_data.json payload is {len(payload)} bytes, below the 10KB floor')
    con.close()
    OUT_PATH.write_text(payload, encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(issuers)} issuers)")


if __name__ == "__main__":
    main()
