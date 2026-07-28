"""CLI for the Slice 1 pipeline.

  python -m hybond selftest              # verify live MOEX/CBR connectivity
  python -m hybond run watchlist.json    # full ingest -> screen -> wall
  python -m hybond run watchlist.json --out report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def cmd_selftest(_args) -> int:
    from .ingest import sources
    ok = True
    try:
        master = sources.fetch_bond_master(snapshot=False)
        print(f"[ok]  MOEX bond master: {len(master)} bonds")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] MOEX bond master: {e}")
        ok = False
    try:
        curve = sources.fetch_cbr_zcyc(snapshot=False)
        # Sanity range for eyeballing selftest output, not enforced in code —
        # CBR cut the key rate to 14% on 25.07.2026, so 1y should print
        # roughly 0.13-0.15 (13-15%) now, not the ~0.16 seen before the cut
        # or anything near 0 (that magnitude of miss is the comma/dot parsing
        # bug this file's _parse_cbr_zcyc was fixed for — see its comments).
        print(f"[ok]  CBR ZCYC: {len(curve)} points, "
              f"1y={dict(curve).get(1.0, 'n/a')}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] CBR ZCYC: {e}")
        ok = False
    return 0 if ok else 1


def cmd_run(args) -> int:
    from . import pipeline
    report = pipeline.run(Path(args.watchlist), snap_date=date.today())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    # brief human summary
    ss = report["spread_screen"]
    if ss:
        print(f"\nTop by G-spread:", file=sys.stderr)
        for r in ss[:5]:
            print(f"  {r['issuer']:<24} {r['secid']:<14} "
                  f"{r['g_spread_bp']:>6.0f}bp  ytm {r['ytm_pct']}%",
                  file=sys.stderr)
    return 0


def cmd_diff(args) -> int:
    from . import diff_pipeline
    from pathlib import Path
    report = diff_pipeline.run_diff(
        driving_detector=args.driving,
        factlist_dir=Path(args.factlist_dir) if args.factlist_dir else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    att = report["attention_issuers"]
    print(f"\n{len(att)} issuer(s) surfaced by '{report['driving_detector']}'. "
          f"Detector tally: {report['flag_tally_by_detector']}", file=sys.stderr)
    return 0


def cmd_grade(args) -> int:
    from . import grade
    from .store import db
    con = db.connect()
    db.init_db(con)
    print(json.dumps(grade.summarise(con), ensure_ascii=False, indent=2))
    q = grade.ungraded_flags(con)
    print(f"\n{len(q)} flag(s) awaiting a verdict.", file=sys.stderr)
    con.close()
    return 0


def cmd_scrape(args) -> int:
    from . import scrape_pipeline
    from pathlib import Path
    summary = scrape_pipeline.run_scrape(
        Path(args.watchlist), use_selenium=args.selenium,
        fetch_bodies=not args.no_bodies, debug=args.debug,
        use_smartlab=args.smartlab)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n{summary['new_total']} new filing(s) across "
          f"{len(summary['issuers'])} issuer(s). "
          f"{len(summary['errors'])} error(s).", file=sys.stderr)
    return 0


def cmd_queue(_args) -> int:
    from . import scrape_pipeline
    view = scrape_pipeline.classify_queue_view()
    print(json.dumps(view, ensure_ascii=False, indent=2))
    print(f"\n{view['unclassified_count']} filing(s) awaiting classification. "
          f"Invoke hy-event-classify on each.", file=sys.stderr)
    return 0


def cmd_universe(args) -> int:
    from . import universe_pipeline
    r = universe_pipeline.run_universe(refresh_emitent_map=args.refresh_emitent_map)
    print(f"Universe snapshot: {r['snap_date']}")
    print(f"Total bonds pulled from MOEX: {r['total_pulled']}")
    print(f"After screening: {r['after_screening']} bonds")
    print(f"With G-spread computed: {r['with_g_spread']} bonds")
    print(f"With rating/mispricing: {r['with_rating']} bonds")
    print(f"In watchlist: {r['in_watchlist']} bonds")
    print(f"Signal excluded (commodity-linked/near-maturity-thin): {r['signal_excluded']} bonds")
    fallback_tag = " [FALLBACK — no CBR meeting analysis available]" if r["aey_is_fallback"] else ""
    print(f"AEY scenarios sourced from CBR meeting: {r['aey_source_date']}{fallback_tag}")
    print()
    print("Top 10 by G-spread (bps):")
    for row in r["top_by_g_spread"]:
        rating = row["rating"] or "—"
        if row["mispricing_bp"] is not None:
            tag = "cheap" if row["mispricing_bp"] > 0 else "rich"
            misprice_s = f"{row['mispricing_bp']:+.0f}bp {tag}"
        else:
            misprice_s = "н/д"
        wl_tag = " ★" if row["in_watchlist"] else ""
        print(f"  {(row['short_name'] or row['emitent_title'])[:24]:<24} "
              f"{row['secid']:<14} {row['g_spread_bp']:>6.0f}bp  "
              f"{row['ytm_pct']:.1f}% YTM  {rating:<8} {misprice_s}{wl_tag}")
    return 0


def cmd_bonds(args) -> int:
    from .ingest import sources
    bonds = sources.fetch_bonds_by_inn(args.inn)
    if not bonds:
        print(f"No outstanding bonds found on MOEX for ИНН {args.inn}")
        return 1
    md = {r["SECID"]: r for r in sources.fetch_marketdata(
        [b["SECID"] for b in bonds], snapshot=False)}
    bonds.sort(key=lambda b: b.get("MATDATE") or "9999-99-99")

    print(f"{'SECID':<14} {'SHORTNAME':<16} {'MATDATE':<11} "
          f"{'COUPON%':>8} {'LAST':>10}")
    for b in bonds:
        q = md.get(b["SECID"], {})
        last = q.get("LAST")
        last_s = f"{last:.2f}" if last is not None else "нет цены"
        coupon = b.get("COUPONPERCENT")
        coupon_s = f"{coupon:.2f}" if coupon is not None else "—"
        print(f"{b['SECID']:<14} {(b.get('SHORTNAME') or '—'):<16} "
              f"{b.get('MATDATE') or '—':<11} {coupon_s:>8} {last_s:>10}")
    print(f"\n{len(bonds)} outstanding series for ИНН {args.inn}", file=sys.stderr)
    return 0


def cmd_rate(args) -> int:
    from datetime import date
    from .store import db
    con = db.connect()
    db.init_db(con)
    db.init_rating(con)

    if args.list:
        wl_names = {}
        try:
            wl_names = {k: v["name"] for k, v in json.loads(
                Path("watchlist.json").read_text(encoding="utf-8")).items()
                if not k.startswith("_")}
        except FileNotFoundError:
            pass
        rows = con.execute(
            "SELECT issuer_id, agency, rating, outlook, action, rated_on "
            "FROM rating WHERE is_current=1 ORDER BY issuer_id").fetchall()
        print(f"{'ИНН':<14} {'Name':<35} {'Rating':<10} {'Agency':<10} "
              f"{'Outlook':<10} {'Action':<10} {'Rated on'}")
        for r in rows:
            print(f"{r['issuer_id']:<14} {wl_names.get(r['issuer_id'], '')[:34]:<35} "
                  f"{r['rating']:<10} {r['agency']:<10} "
                  f"{r['outlook'] or '—':<10} {r['action'] or '—':<10} {r['rated_on']}")
        print(f"\n{len(rows)} current rating(s).", file=sys.stderr)
        con.close()
        return 0

    if not (args.inn and args.agency and args.rating):
        print("--inn, --agency and --rating are required unless --list is given",
              file=sys.stderr)
        con.close()
        return 1

    rated_on = args.date or date.today().isoformat()
    db.upsert_rating(con, issuer_id=args.inn, agency=args.agency,
                      rating=args.rating, outlook=args.outlook,
                      action=args.action, rated_on=rated_on, source="manual")
    con.commit()
    print(f"Recorded {args.rating} ({args.agency}) for ИНН {args.inn} on {rated_on}")
    con.close()
    return 0


def cmd_financials(args) -> int:
    from . import financials_pipeline
    from pathlib import Path
    summary = financials_pipeline.run_financials(Path(args.watchlist),
                                                 api_key=args.key)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if "error" in summary:
        print(f"\n{summary['error']}", file=sys.stderr)
        return 1
    print(f"\n{len(summary['issuers'])} issuer(s) scored, "
          f"{len(summary['errors'])} error(s).", file=sys.stderr)
    return 0


_CBR_TEXT_FIELDS = ["press_release_text", "nabiullina_speech_text", "resume_text",
                    "forecast_summary", "comment_text"]


def _print_fetch_result(fetch_date: str, is_anchor: bool, result: dict) -> None:
    print(f"Fetched {fetch_date} (anchor={is_anchor}):")
    for f in _CBR_TEXT_FIELDS:
        n = len(result.get(f) or "")
        print(f"  {f}: {n} chars" + (" [empty/not yet published]" if n == 0 else ""))
    print(f"  rate_before={result.get('rate_before')} rate_after={result.get('rate_after')} "
          f"change_bp={result.get('rate_change_bp')}")


def cmd_cbr(args) -> int:
    from . import cbr_tracker
    from .store import db

    if args.install_cron:
        print(cbr_tracker.generate_cron_entries(str(Path.cwd())))
        return 0

    if args.list:
        con = db.connect()
        db.init_db(con)
        db.init_cbr(con)
        print(f"{'Date':<12} {'Anchor':<7} {'Rate':<8} {'Change':<7} {'Press':<6} "
              f"{'Speech':<7} {'Résumé':<7} {'Forecast':<9} {'Comment':<8} {'Analysis'}")
        for m in cbr_tracker.CBR_MEETINGS_2026:
            row = db.get_cbr_meeting(con, m["date"])
            row = dict(row) if row else {}
            def has(field):
                return "yes" if row.get(field) else "—"
            rate = f"{row.get('rate_after')}" if row.get("rate_after") is not None else "—"
            change = f"{row.get('rate_change_bp'):+d}" if row.get("rate_change_bp") is not None else "—"
            print(f"{m['date']:<12} {'★' if m['anchor'] else ' ':<7} {rate:<8} {change:<7} "
                  f"{has('press_release_text'):<6} {has('nabiullina_speech_text'):<7} "
                  f"{has('resume_text'):<7} {has('forecast_summary'):<9} "
                  f"{has('comment_text'):<8} {has('analysis_json')}")
        con.close()
        return 0

    if args.fetch_all:
        from datetime import date as _date
        results = cbr_tracker.fetch_all_meetings()
        past_meetings = [m for m in cbr_tracker.CBR_MEETINGS_2026
                        if _date.fromisoformat(m["date"]) <= _date.today()]
        for m, result in zip(past_meetings, results):
            _print_fetch_result(m["date"], m["anchor"], result)
            print()
        return 0

    if args.fetch:
        meeting = next((m for m in cbr_tracker.CBR_MEETINGS_2026 if m["date"] == args.fetch), None)
        if not meeting:
            print(f"{args.fetch} is not in CBR_MEETINGS_2026 -- add it there first.", file=sys.stderr)
            return 1
        result = cbr_tracker.fetch_meeting(args.fetch, meeting["anchor"])
        con = db.connect()
        db.init_db(con)
        cbr_tracker.store_meeting(con, result)
        con.close()
        _print_fetch_result(args.fetch, meeting["anchor"], result)
        return 0

    if args.diff_all:
        diffs = cbr_tracker.compute_all_diffs()
        for d in diffs:
            _print_diff(d)
            print()
        print(f"{len(diffs)} diff(s) computed.", file=sys.stderr)
        return 0

    if args.diff:
        con = db.connect()
        db.init_db(con)
        db.init_cbr(con)
        db.init_cbr_diff(con)
        row = db.get_cbr_meeting_diff(con, args.diff)
        con.close()
        if not row:
            print(f"No diff stored for {args.diff} -- run --fetch-all or --diff-all first "
                  f"(a meeting's diff needs a prior meeting with rate_after set).", file=sys.stderr)
            return 1
        _print_diff(dict(row))
        return 0

    print("Specify --fetch YYYY-MM-DD, --fetch-all, --list, --diff YYYY-MM-DD, --diff-all, or --install-cron",
          file=sys.stderr)
    return 1


def _print_diff(d: dict) -> None:
    from .store import db
    con = db.connect()
    prior_row = db.get_cbr_meeting(con, d["prior_meeting_date"])
    this_row = db.get_cbr_meeting(con, d["meeting_date"])
    con.close()
    prior_is_anchor = bool(prior_row and prior_row["is_anchor"])
    print(f"Meeting: {d['meeting_date']} vs prior {'anchor ' if prior_is_anchor else ''}{d['prior_meeting_date']}")
    change = d.get("rate_change_bp")
    prior_change = prior_row["rate_change_bp"] if prior_row else None
    if change is not None and prior_change is not None:
        print(f"  Rate pace:     {d.get('pace_change')} ({abs(prior_change)}bp → {abs(change)}bp)")
    else:
        print(f"  Rate pace:     {d.get('pace_change')}" +
              (f" ({abs(change)}bp)" if change is not None else ""))
    if d.get("forecast_2026_shift_bp") is not None:
        this_2026 = this_row["rate_2026_mid"] if this_row else "?"
        prior_2026 = prior_row["rate_2026_mid"] if prior_row else "?"
        print(f"  2026 forecast: {d['forecast_2026_shift_bp']:+d}bp ({prior_2026}% → {this_2026}%)")
    if d.get("forecast_2027_shift_bp") is not None:
        this_2027 = this_row["rate_2027_mid"] if this_row else "?"
        prior_2027 = prior_row["rate_2027_mid"] if prior_row else "?"
        print(f"  2027 forecast: {d['forecast_2027_shift_bp']:+d}bp ({prior_2027}% → {this_2027}%)")
    print(f"  Portfolio:     {d.get('portfolio_signal', '').upper()} ({d.get('signal_magnitude')} signal)")
    print(f"  Tone shift:    {d.get('tone_shift')}")
    print(f"  Note: {d.get('diff_note_ru')}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hybond")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    pr = sub.add_parser("run")
    pr.add_argument("watchlist")
    pr.add_argument("--out")
    pr.set_defaults(func=cmd_run)
    pd = sub.add_parser("diff", help="Slice 2: run detectors on today's snapshot")
    pd.add_argument("--driving", default="fixed",
                    choices=["fixed", "stat", "hybrid"],
                    help="which detector drives attention (fixed early on)")
    pd.add_argument("--factlist-dir", help="write per-issuer factlists here")
    pd.set_defaults(func=cmd_diff)
    pg = sub.add_parser("grade", help="Slice 2: detector scorecard (run weeks later)")
    pg.set_defaults(func=cmd_grade)
    ps = sub.add_parser("scrape", help="Slice 3: pull e-disclosure message feed")
    ps.add_argument("watchlist")
    ps.add_argument("--selenium", action="store_true",
                    help="use headless browser for JS-gated pages")
    ps.add_argument("--no-bodies", action="store_true",
                    help="skip fetching message bodies (faster, list only)")
    ps.add_argument("--debug", action="store_true",
                    help="dump first issuer's raw rendered HTML to "
                         "data/raw/edisclosure/debug_page.html before parsing")
    ps.add_argument("--smartlab", action="store_true",
                    help="also scrape smart-lab.ru bonds feed (secondary "
                         "source, supplements AZIPI for Interfax-only issuers)")
    ps.set_defaults(func=cmd_scrape)
    pq = sub.add_parser("queue", help="Slice 3: show filings awaiting classification")
    pq.set_defaults(func=cmd_queue)
    pu = sub.add_parser("universe", help="Full MOEX corporate-bond universe screen + G-spread + mispricing")
    pu.add_argument("--refresh-emitent-map", action="store_true",
                     help="force a fresh paginated SECID->ИНН fetch instead of the cached "
                          "emitent_map table (normally refreshed weekly)")
    pu.set_defaults(func=cmd_universe)
    pb = sub.add_parser("bonds", help="Slice 1: all outstanding MOEX series for one issuer by ИНН")
    pb.add_argument("inn")
    pb.set_defaults(func=cmd_bonds)
    pr_ = sub.add_parser("rate", help="Manual credit-rating entry (feeds the default-probability layer)")
    pr_.add_argument("--inn", help="issuer ИНН")
    pr_.add_argument("--agency", help='e.g. "АКРА", "Expert RA", "NKR", "NRA"')
    pr_.add_argument("--rating", help="ruXX-format rating, e.g. ruAA-")
    pr_.add_argument("--outlook", help='e.g. "stable", "positive", "negative"')
    pr_.add_argument("--action", help='e.g. "confirmed", "upgraded", "downgraded"')
    pr_.add_argument("--date", help="rated-on date, ISO (defaults to today)")
    pr_.add_argument("--list", action="store_true", help="show current ratings for all issuers")
    pr_.set_defaults(func=cmd_rate)
    pf = sub.add_parser("financials",
                        help="Slice 4: fetch ГИР БО financials + compute pillars")
    pf.add_argument("watchlist")
    pf.add_argument("--key", help="parser-api key (or set PARSER_API_KEY)")
    pf.set_defaults(func=cmd_financials)
    pc = sub.add_parser("cbr", help="CBR key-rate meeting tracker")
    pc.add_argument("--fetch", metavar="YYYY-MM-DD",
                    help="fetch/backfill all available documents for one meeting date")
    pc.add_argument("--fetch-all", action="store_true",
                    help="backfill every past 2026 meeting date")
    pc.add_argument("--list", action="store_true",
                    help="show fetch status for every 2026 meeting date")
    pc.add_argument("--install-cron", action="store_true",
                    help="print crontab entries for every upcoming 2026 meeting/résюме date")
    pc.add_argument("--diff", metavar="YYYY-MM-DD",
                    help="show the stored meeting-over-meeting diff for one date")
    pc.add_argument("--diff-all", action="store_true",
                    help="(re)compute diffs for every meeting that has a qualifying prior")
    pc.set_defaults(func=cmd_cbr)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
