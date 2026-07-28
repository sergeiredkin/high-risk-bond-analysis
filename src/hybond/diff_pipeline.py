"""Slice 2 pipeline: read snapshots -> run three detectors -> log all flags ->
emit factlists. No LLM.

Assumes Slice 1 has already populated bond / price_snapshot / spread_snapshot
for `snap_date`. This step is the change-detection gate: it decides which
issuers are worth attention (and, in Slice 3, worth an LLM call).

Detector policy (see diff/engine.py):
  - `fixed` DRIVES attention early (works cold).
  - `stat` and `hybrid` LOG silently until they have history, then get graded.
The `driving_detector` argument lets you flip who's in charge after grading.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .diff import engine
from .factlist.assemble import build_factlist
from .store import db


def _prior_traded(con, secid: str, before: str) -> bool:
    hist = db.price_history(con, secid, before, limit=1)
    if not hist:
        return False
    v, t = hist[0]["volume"], hist[0]["num_trades"]
    return bool(v) and bool(t)


def run_diff(
    snap_date: date | None = None,
    driving_detector: str = "fixed",
    new_filings: dict[str, list[str]] | None = None,
    factlist_dir: Path | None = None,
) -> dict:
    """new_filings: optional {secid: [titles]} of disclosures detected today
    (Slice 3 will wire this to e-disclosure; passable manually for now)."""
    snap_date = snap_date or date.today()
    sd = snap_date.isoformat()
    new_filings = new_filings or {}
    con = db.connect()
    db.init_db(con)

    # gather issuers that have a spread snapshot today
    secid_rows = con.execute(
        "SELECT b.secid, b.issuer_id FROM bond b "
        "JOIN spread_snapshot s ON s.secid=b.secid AND s.snap_date=?", (sd,)
    ).fetchall()

    flags_by_issuer: dict[str, list[dict]] = {}
    stat_warming: dict[str, bool] = {}
    all_flags: list[dict] = []

    for r in secid_rows:
        secid, issuer_id = r["secid"], r["issuer_id"]
        sp_today = db.latest_spread(con, secid, sd)
        if not sp_today or sp_today["g_spread_bp"] is None:
            continue
        hist = db.spread_history(con, secid, sd, limit=40)
        prior = hist[0]["g_spread_bp"] if hist else None

        detected: list[engine.Flag] = []
        detected += engine.detect_spread_move(
            secid, issuer_id, sp_today["g_spread_bp"], prior, hist)

        pr_today = db.latest_price(con, secid, sd)
        detected += engine.detect_volume_silence(
            secid, issuer_id,
            pr_today["volume"] if pr_today else None,
            pr_today["num_trades"] if pr_today else None,
            _prior_traded(con, secid, sd))

        detected += engine.detect_new_filing(secid, issuer_id, new_filings.get(secid, []))

        stat_warming[issuer_id] = not engine.stat_is_live(hist)

        for fl in detected:
            fid = db.insert_flag(
                con, snap_date=sd, secid=fl.secid, issuer_id=fl.issuer_id,
                detector=fl.detector, signal=fl.signal, metric=fl.metric,
                detail=fl.detail, driving=1 if fl.detector == driving_detector else 0)
            d = asdict(fl)
            d["flag_id"] = fid
            all_flags.append(d)
            # only the driving detector's flags surface an issuer for attention
            if fl.detector == driving_detector:
                flags_by_issuer.setdefault(issuer_id, []).append(d)

    con.commit()

    # build factlists for issuers the driving detector surfaced today
    factlists = {}
    for issuer_id, iflags in flags_by_issuer.items():
        fl = build_factlist(con, issuer_id, sd, iflags,
                            stat_warming.get(issuer_id, True))
        factlists[issuer_id] = fl.to_json()
        if factlist_dir:
            factlist_dir.mkdir(parents=True, exist_ok=True)
            (factlist_dir / f"{issuer_id}_{sd}.json").write_text(
                json.dumps(fl.to_json(), ensure_ascii=False, indent=2),
                encoding="utf-8")

    con.close()

    # detector tally for the daily read
    tally: dict[str, int] = {}
    for f in all_flags:
        tally[f["detector"]] = tally.get(f["detector"], 0) + 1

    return {
        "snap_date": sd,
        "driving_detector": driving_detector,
        "attention_issuers": list(flags_by_issuer.keys()),
        "flag_tally_by_detector": tally,
        "factlists": factlists,
        "note": "stat/hybrid logging silently until graded; fixed driving.",
    }
