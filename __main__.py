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
        fetch_bodies=not args.no_bodies)
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
    ps.set_defaults(func=cmd_scrape)
    pq = sub.add_parser("queue", help="Slice 3: show filings awaiting classification")
    pq.set_defaults(func=cmd_queue)
    pf = sub.add_parser("financials",
                        help="Slice 4: fetch ГИР БО financials + compute pillars")
    pf.add_argument("watchlist")
    pf.add_argument("--key", help="parser-api key (or set PARSER_API_KEY)")
    pf.set_defaults(func=cmd_financials)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
