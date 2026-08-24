"""CLI for the Slice 1 pipeline.

  python -m hybond selftest              # verify live MOEX/CBR connectivity
  python -m hybond run watchlist.json    # full ingest -> screen -> wall
  python -m hybond run watchlist.json --out report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
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
    from . import pipeline, pipeline_lock
    watchlist_path = Path(args.watchlist) if getattr(args, "watchlist", None) else None
    try:
        with pipeline_lock.held():
            report = pipeline.run(watchlist_path, snap_date=date.today())
    except pipeline_lock.PipelineInterrupted:
        print("Interrupted", file=sys.stderr)
        return 143
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
    from . import pipeline_lock, scrape_pipeline
    from pathlib import Path
    watchlist_path = Path(args.watchlist) if getattr(args, "watchlist", None) else None
    try:
        with pipeline_lock.held():
            summary = scrape_pipeline.run_scrape(
                watchlist_path, use_selenium=args.selenium,
                fetch_bodies=not args.no_bodies, debug=args.debug,
                use_smartlab=args.smartlab)
    except pipeline_lock.PipelineInterrupted:
        print("Interrupted", file=sys.stderr)
        return 143
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n{summary['new_total']} new filing(s) across "
          f"{len(summary['issuers'])} issuer(s). "
          f"{len(summary['errors'])} error(s).", file=sys.stderr)
    return 0


def cmd_market_notes(_args) -> int:
    from . import scrape_pipeline
    result = scrape_pipeline.run_market_notes()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["error"]:
        print(f"\n{result['error']}", file=sys.stderr)
        return 1
    print(f"\n{result['new']} new market note(s) out of {result['parsed']} parsed.",
          file=sys.stderr)
    return 0


def _print_queue_sections(view: dict, issuer_names: dict[str, str] | None = None) -> None:
    """Shared three-section rendering for `queue` and `market-notes-queue`:
    priority first (needs attention soonest), then the actually-actionable
    normal bucket, then noise collapsed to a count + issuer list (full
    title/date is redundant for rows that are almost certainly routine)."""
    issuer_names = issuer_names or {}

    def issuer_label(issuer_id):
        return issuer_names.get(issuer_id, issuer_id)

    print(f"\n=== ПРИОРИТЕТ ({view['priority_count']} записей) ===")
    for r in view["priority"]:
        title = r.get("title") or r.get("headline")
        date_ = r.get("published") or r.get("occurred")
        print(f"  [{date_}] {issuer_label(r['issuer_id'])}: {title}")

    print(f"\n=== К КЛАССИФИКАЦИИ ({view['normal_count']} записей) ===")
    for r in view["queue"]:
        title = r.get("title") or r.get("headline")
        date_ = r.get("published") or r.get("occurred")
        print(f"  [{date_}] {issuer_label(r['issuer_id'])}: {title}")

    print(f"\n=== ШУМ — пропустить ({view['noise_count']} записей) ===")
    noise_issuers = sorted({issuer_label(r["issuer_id"]) for r in view["noise"]})
    if noise_issuers:
        print("  " + ", ".join(noise_issuers))


def _issuer_name_map(con, issuer_ids) -> dict[str, str]:
    from .store import db
    ids = sorted(set(issuer_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT issuer_id, name FROM issuer WHERE issuer_id IN ({placeholders})", ids
    ).fetchall()
    return {r["issuer_id"]: r["name"] for r in rows}


def cmd_queue(args) -> int:
    from . import scrape_pipeline
    from .store import db

    view = scrape_pipeline.classify_queue_view()
    con = db.connect()
    all_ids = [r["issuer_id"] for r in view["priority"] + view["queue"] + view["noise"]]
    names = _issuer_name_map(con, all_ids)

    if args.skip_noise:
        from .classify_utils import queue_bucket
        # NOT view["noise"] -- classify_queue_view() caps every bucket at
        # [:50] for DISPLAY (same pre-existing convention the JSON dump
        # above already had). --skip-noise is an ACTION, not a display, and
        # must act on every matching row, not just the first 50 shown.
        all_rows = db.unclassified_filings(con)
        target_rows = [r for r in all_rows if queue_bucket(r["title"]) == "noise"]
        if not target_rows:
            print("Нет записей, подходящих под шумовые паттерны — нечего пропускать.")
            con.close()
            return 0
        print(f"{len(target_rows)} filing(s) match a noise pattern and will be marked "
              f"classified=1 (category=routine, severity=noise) without hy-event-classify.")
        answer = input("Продолжить? [y/N]: ").strip().lower()
        if answer != "y":
            print("Отменено.")
            con.close()
            return 0
        n = db.skip_noise_filings(con, target_rows)
        print(f"Marked {n} filing(s) as routine/noise.")
        con.close()
        return 0

    con.close()
    # JSON dump kept as-is (hy-event-classify reads each row's "source"
    # field from this -- see the skill's Inputs section) -- the three
    # sections below are an additional human-readable summary, not a
    # replacement.
    print(json.dumps(view, ensure_ascii=False, indent=2))
    _print_queue_sections(view, names)
    print(f"\n{view['unclassified_count']} filing(s) awaiting classification "
          f"({view['priority_count']} priority, {view['normal_count']} to classify, "
          f"{view['noise_count']} noise). Invoke hy-event-classify on each "
          f"non-noise row.", file=sys.stderr)
    return 0


def cmd_market_notes_queue(_args) -> int:
    from . import scrape_pipeline
    from .store import db

    view = scrape_pipeline.market_note_queue_view()
    con = db.connect()
    all_ids = [r["issuer_id"] for r in view["priority"] + view["queue"] + view["noise"]]
    names = _issuer_name_map(con, all_ids)
    con.close()

    print(json.dumps(view, ensure_ascii=False, indent=2))
    _print_queue_sections(view, names)
    print(f"\n{view['unclassified_count']} market note(s) awaiting classification "
          f"({view['priority_count']} priority, {view['normal_count']} to classify, "
          f"{view['noise_count']} noise). Invoke hy-event-classify on each non-noise "
          f"row (headline+summary input).", file=sys.stderr)
    return 0


AUTO_CLASSIFY_BATCH_SIZE = 5


def _chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _auto_classify_pass(con, args) -> dict:
    from .classify_ollama import MODEL, classify_filing, needs_review
    from .store import db

    # --limit is a COMBINED budget across both queues, not per-queue -- each
    # real classification call measured at 52-77s (see classify_ollama.py),
    # so a per-queue limit could silently double a cron run's worst-case
    # duration (e.g. --limit 30 -> up to 60 calls -> up to ~77 min, longer
    # than the hourly window this is meant to fit inside).
    filings = ([] if args.source == "market_note" else
               db.get_auto_classify_queue(con, limit=args.limit, priority_only=args.priority_only))
    remaining = None if args.limit is None else max(0, args.limit - len(filings))
    notes = ([] if args.source == "filing" else
             db.get_auto_classify_market_notes(con, limit=remaining, priority_only=args.priority_only))

    stats = {"processed": 0, "classified": 0, "skipped_noise": 0,
             "skipped_confidence": 0, "failed": 0, "needs_review": 0}

    # NOT batched via classify_filings_batch (classify_ollama.py) -- tried
    # it live against this machine's real Ollama/qwen3.5 build (2026-08-20)
    # and it does not work: `think: False` does not actually suppress the
    # model's reasoning chain (same finding classify_filing()'s 120s timeout
    # comment already notes for a single item), and batching multiple items
    # multiplies that reasoning cost instead of amortizing it -- two live
    # batch calls (2 and 5 items, num_predict up to 4000) both came back
    # with done_reason='length' and an EMPTY response after ~75s, i.e. worse
    # than baseline (75s for zero classifications vs 52-77s for one).
    # classify_filings_batch() is kept in the codebase, unit-tested, for a
    # future model/build where this might actually work -- just not wired
    # in here until it's been shown to work for real.
    #
    # Previously this connection held one uncommitted transaction open for
    # the ENTIRE run (a single con.commit() at the end) -- with 52-77s/call
    # latency that meant the write lock stayed held for up to ~38 minutes on
    # a --limit 30 run, the actual cause of the "database is locked" errors
    # other hybond commands hit repeatedly against this cron. Committing
    # every AUTO_CLASSIFY_BATCH_SIZE items (and yielding briefly) shrinks
    # that to milliseconds per batch; WAL mode (db.connect()) means readers
    # don't even need to wait for that, but writer-vs-writer collisions
    # still do -- both independently real and verified live.
    for batch in _chunked(filings, AUTO_CLASSIFY_BATCH_SIZE):
        for filing in batch:
            stats["processed"] += 1
            result = classify_filing(title=filing["title"], url=filing["url"],
                                      issuer_name=filing["issuer_name"])
            if result is None:
                stats["failed"] += 1
                continue
            if result["confidence"] < 0.65:
                print(f"Low confidence ({result['confidence']:.2f}) — skipping: {filing['title']}")
                stats["skipped_confidence"] += 1
                continue

            review = needs_review(result["severity"], result["confidence"])
            if not args.dry_run:
                db.insert_event(
                    con, issuer_id=filing["issuer_id"], occurred=filing["published"],
                    category=result["category"], severity=result["severity"],
                    summary=result["summary"], source_url=filing["url"],
                    classified_by=f"ollama:{MODEL}", confidence=result["confidence"],
                    needs_review=1 if review else 0)
                db.mark_classified(con, filing["body_hash"])

            if result["severity"] == "noise":
                stats["skipped_noise"] += 1
            else:
                stats["classified"] += 1
            if review:
                stats["needs_review"] += 1
        if not args.dry_run:
            con.commit()
            time.sleep(0.1)

    for batch in _chunked(notes, AUTO_CLASSIFY_BATCH_SIZE):
        for note in batch:
            stats["processed"] += 1
            result = classify_filing(title=note["title"], url=note["url"],
                                      issuer_name=note["issuer_name"] or "неизвестный эмитент")
            if result is None:
                stats["failed"] += 1
                continue
            if result["confidence"] < 0.65:
                print(f"Low confidence ({result['confidence']:.2f}) — skipping: {note['title']}")
                stats["skipped_confidence"] += 1
                continue

            review = needs_review(result["severity"], result["confidence"])
            # market_note has no classified_by/needs_review columns (Part 1
            # only extended `event`) -- set_market_note_classification()
            # writes the three columns it does have; confidence still gates
            # the write above.
            if not args.dry_run:
                db.set_market_note_classification(
                    con, note["note_id"], severity=result["severity"],
                    category=result["category"], confidence=result["confidence"])

            if result["severity"] == "noise":
                stats["skipped_noise"] += 1
            else:
                stats["classified"] += 1
            if review:
                stats["needs_review"] += 1
        if not args.dry_run:
            con.commit()
            time.sleep(0.1)

    con.commit()
    return stats


AUTO_CLASSIFY_TIMESTAMP_PATH = Path("data/logs/last_classify.timestamp")


def cmd_auto_classify(args) -> int:
    from . import pipeline_lock
    from .store import db

    pause = getattr(args, "pause", None)
    total = {"processed": 0, "classified": 0, "skipped_noise": 0,
              "skipped_confidence": 0, "failed": 0, "needs_review": 0}
    interrupted = False

    try:
        # pipeline_lock.held() is the shared cross-pipeline lock (scrape,
        # auto-classify, and the daily `hybond run` all use it) -- covers
        # the real live-confirmed collision (hourly_filings.sh's scrape
        # step still running past its slot vs. rebuild_dashboard.sh's own
        # classify burst), not just two auto-classify invocations racing
        # each other. Held for the whole call (all bursts under --pause
        # included), not per-burst -- distinct from --pause's DB-connection
        # release.
        with pipeline_lock.held():
            # Without --pause: exactly one pass, one connection held open for
            # its duration -- unchanged from before this flag existed. With
            # --pause: the connection is closed (not just idle) between
            # bursts so it's genuinely released, not merely uncontended --
            # the actual fix for background runs blocking interactive hybond
            # commands. Loops until a burst classifies zero items (queue
            # empty); there is no separate outer cap -- with --pause,
            # --limit is the per-burst size, not a total budget (see
            # cmd_auto_classify's --pause help text).
            first = True
            while True:
                if pause and not first:
                    time.sleep(pause)
                first = False

                con = db.connect()
                db.init_db(con)
                db.init_market_note(con)
                stats = _auto_classify_pass(con, args)
                con.close()

                for k in total:
                    total[k] += stats[k]

                # dry-run never marks items classified, so the same rows
                # would be refetched every burst forever -- one pass is all
                # --dry-run --pause can meaningfully do. A pass that
                # processed fewer than --limit items already drained the
                # queue (get_auto_classify_queue returns up to `limit` rows
                # when that many exist) -- stop now rather than paying one
                # more sleep + an empty probe pass to find that out.
                drained = args.limit is not None and stats["processed"] < args.limit
                if not pause or stats["processed"] == 0 or args.dry_run or drained:
                    break
    except pipeline_lock.PipelineInterrupted:
        interrupted = True

    # dry-run writes nothing to the DB, so it hasn't actually "categorized"
    # anything -- rebuild_dashboard.sh's staleness check should not treat a
    # preview run as a real catch-up. An interrupted run may have partially
    # classified items (already committed per-batch, see _auto_classify_pass)
    # but is not a clean "completion" either -- don't claim the queue is caught up.
    if not args.dry_run and not interrupted and total["processed"] > 0:
        AUTO_CLASSIFY_TIMESTAMP_PATH.write_text(str(int(time.time())))

    if interrupted:
        print(f"Interrupted after processing {total['processed']} items "
              f"({total['classified']} classified, already committed per-batch)")
        return 143

    print(f"Processed: {total['processed']}")
    print(f"Classified: {total['classified']} events written")
    print(f"Noise (skipped): {total['skipped_noise']}")
    print(f"Low confidence (skipped): {total['skipped_confidence']}")
    print(f"Failed (API error): {total['failed']}")
    print(f"Needs review: {total['needs_review']}")
    return 0


def cmd_review_queue(args) -> int:
    from .store import db

    con = db.connect()
    db.init_db(con)
    rows = db.events_needing_review(con, limit=args.limit)

    if not rows:
        print("Нет событий, требующих проверки.")
        con.close()
        return 0

    print(f"=== ТРЕБУЮТ ПРОВЕРКИ ({len(rows)} событий) ===\n")
    for i, r in enumerate(rows, 1):
        print(f"[{i}] {r['occurred']} | {r['issuer_name']}")
        print(f"    Категория: {r['category']} | Severity: {r['severity']} | "
              f"Confidence: {r['confidence']:.2f}" if r["confidence"] is not None
              else f"    Категория: {r['category']} | Severity: {r['severity']} | Confidence: н/д")
        print(f"    Резюме: {r['summary']}")
        print(f"    URL: {r['source_url']}")
        answer = input("    Действие: [a]ccepted / [e]dit / [s]kip > ").strip().lower()
        if answer == "a":
            db.resolve_event_review(con, r["event_id"])
            con.commit()
            print("    -> принято.\n")
        elif answer == "e":
            new_cat = input(f"    Категория [{r['category']}]: ").strip() or None
            new_sev = input(f"    Severity [{r['severity']}]: ").strip() or None
            db.resolve_event_review(con, r["event_id"], category=new_cat, severity=new_sev)
            con.commit()
            print("    -> обновлено.\n")
        else:
            print("    -> пропущено (останется в очереди).\n")

    con.close()
    return 0


def cmd_fill_price_impact(_args) -> int:
    from .store import db
    con = db.connect()
    db.init_db(con)
    result = db.fill_price_impact(con)
    con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n{result['updated']} event row(s) updated with price-impact data. "
          f"{result['still_null']} remain NULL (event too recent for spread_snapshot "
          f"to have caught up, or issuer has no priced bond) -- expected, not an error.",
          file=sys.stderr)
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


def cmd_ofz(_args) -> int:
    from . import ofz_pipeline
    r = ofz_pipeline.run_ofz()
    print(f"OFZ snapshot: {r['snap_date']}")
    print(f"Total OFZ pulled from MOEX (deduped across TQOB/SPOB): {r['total_ofz_pulled']}")
    print(f"Stored: {r['stored']}")
    print(f"Skipped (matured/no maturity date): {r['skipped_matured_or_no_date']}")
    print(f"Skipped (no price): {r['skipped_no_price']}")
    print(f"By instrument type: {r['by_instrument_type']}")
    if r["unrecognised_type_secids"]:
        print(f"Unrecognised SU-code prefix (defaulted to ПД, flagged): {r['unrecognised_type_secids']}")
    print(f"Real computed YTM/duration/convexity: {r['computed_yield_count']} bonds")
    print(f"MOEX-yield proxy (floater/linker, no convexity): {r['moex_proxy_yield_count']} bonds")
    scen = r["rate_scenarios"]
    fallback_tag = " [FALLBACK — no CBR meeting analysis available]" if scen.get("is_fallback") else ""
    print(f"CBR rate scenarios: base={scen.get('base')} stress={scen.get('stress')} "
          f"bull={scen.get('bull')}{fallback_tag}")
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


def cmd_vdo_screen(args) -> int:
    from . import universe_pipeline
    from .store import db
    con = db.connect()
    db.init_db(con)
    db.init_universe(con)
    report = universe_pipeline.vdo_screen_report(con, top_n=args.top)
    con.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nВДО-scope universe: {report['universe_size']} bond(s) as of "
          f"{report['snap_date']}.", file=sys.stderr)
    return 0


TIER_LABELS = {1: "активный мониторинг", 2: "пассивный мониторинг"}


def cmd_promote(args) -> int:
    from . import promote
    from .store import db
    con = db.connect()
    db.init_db(con)
    db.init_universe(con)
    db.init_manual_candidate(con)

    if getattr(args, "confirm", None):
        # manual_candidate -> real issuer row, at the given tier. Requires
        # the ИНН to already be flagged via `promote --inn/--name` -- this
        # command doesn't invent a name out of thin air.
        row = con.execute("SELECT inn, name, note FROM manual_candidate WHERE inn=?",
                           (args.confirm,)).fetchone()
        if row is None:
            print(f"ИНН {args.confirm} не найден в manual_candidate -- "
                  f"сначала выполните `hybond promote --inn {args.confirm} --name ...`",
                  file=sys.stderr)
            con.close()
            return 1
        try:
            db.add_issuer_at_tier(con, row["inn"], row["name"],
                                   watchlist_tier=args.tier, notes=row["note"])
        except ValueError as e:
            print(str(e), file=sys.stderr)
            con.close()
            return 1
        con.execute("DELETE FROM manual_candidate WHERE inn=?", (row["inn"],))
        con.commit()
        con.close()
        print(f"ИНН {row['inn']} добавлен в вотчлист "
              f"(tier={args.tier} — {TIER_LABELS[args.tier]})", file=sys.stderr)
        return 0

    if args.inn or args.name:
        # Manual-flag mode: log one issuer for promotion-report visibility.
        # Does NOT run the universe screen and does NOT touch watchlist.json
        # -- issuers found via company-scan (e.g. ТЕХНО Лизинг) have no
        # universe_snapshot row to rank in the first place.
        if not (args.inn and args.name):
            print("--inn and --name must be given together", file=sys.stderr)
            con.close()
            return 1
        db.add_manual_candidate(con, args.inn, args.name, note=args.note)
        con.commit()
        con.close()
        print(f"Flagged {args.name} (ИНН {args.inn}) as a manual promotion "
              f"candidate -- will appear in the next `hybond promote` report "
              f"until it's actually added to the watchlist.", file=sys.stderr)
        return 0

    kwargs = {}
    if args.rating_floor is not None:
        kwargs["rating_floor"] = args.rating_floor
    if args.n_before_cap is not None:
        kwargs["n_before_cap"] = args.n_before_cap
    if args.cap is not None:
        kwargs["per_issuer_cap"] = args.cap
    if args.target is not None:
        kwargs["target_after_cap"] = args.target
    report = promote.promote_candidates(con, **kwargs)
    report["manual_candidates"] = db.list_manual_candidates(con)
    con.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\n=== Promotion screen as of {report['snap_date']} "
          f"(THRESHOLDS ARE A STARTING POINT, NOT FINAL -- rating_floor="
          f"{report['rating_floor']}, n_before_cap={report['n_before_cap']}, "
          f"per_issuer_cap={report['per_issuer_cap']}) ===", file=sys.stderr)
    print(f"ВДО-scope universe:        {report['scope_total']}", file=sys.stderr)
    print(f"  cut by rating floor:     {report['cut_by_rating_floor']}", file=sys.stderr)
    print(f"  cut by liquidity floor:  {report['cut_by_liquidity_floor']}", file=sys.stderr)
    print(f"  cut (no AEY/no rating):  {report['cut_by_missing_aey']}", file=sys.stderr)
    print(f"  cut by concentration cap:{report['cut_by_concentration_cap']}", file=sys.stderr)
    print(f"FINAL CANDIDATES:          {len(report['candidates'])}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"{'issuer':<40} {'secid':<14} {'rating':<8} {'aey_risk_adj%':>13}",
          file=sys.stderr)
    for r in report["candidates"]:
        aey_pct = r["aey_base_risk_adj"] * 100
        name = (r.get("short_name") or r.get("emitent_title") or "")[:39]
        print(f"{name:<40} {r['secid']:<14} {r['rating'] or '—':<8} {aey_pct:>12.2f}%",
              file=sys.stderr)

    print(f"\n{len(report['unrated_near_miss'])} unrated near-miss bond(s) "
          f"(would qualify if rated) -- top 15 by raw G-spread:", file=sys.stderr)
    for r in report["unrated_near_miss"][:15]:
        name = (r.get("short_name") or r.get("emitent_title") or "")[:39]
        gsp = r.get("g_spread_bp")
        gsp_s = f"{gsp:.0f}bp" if gsp is not None else "—"
        print(f"  {name:<40} {r['secid']:<14} {gsp_s}", file=sys.stderr)

    print(f"\n{len(report['manual_candidates'])} manually-flagged candidate(s) "
          f"(via `hybond promote --inn/--name`, not in universe_snapshot):",
          file=sys.stderr)
    for r in report["manual_candidates"]:
        note = f" -- {r['note']}" if r.get("note") else ""
        print(f"  {r['name']} (ИНН {r['inn']}, flagged {r['added_at'][:10]}){note}",
              file=sys.stderr)
    return 0


def _cmd_watchlist_tier_change(args, to_tier: int) -> int:
    from .store import db
    con = db.connect()
    db.init_db(con)
    try:
        result = db.set_watchlist_tier(con, args.inn, to_tier, reason=args.reason)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        con.close()
        return 1
    con.commit()
    con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_watchlist_demote(args) -> int:
    return _cmd_watchlist_tier_change(args, to_tier=2)


def cmd_watchlist_promote(args) -> int:
    from . import promotion_audit
    from .store import db

    con = db.connect()
    db.init_db(con)
    db.init_universe(con)
    db.init_pillars(con)
    db.init_market_note(con)

    issuer_row = con.execute(
        "SELECT name FROM issuer WHERE issuer_id=?", (args.inn,)).fetchone()
    if issuer_row is None:
        print(f"no issuer row with issuer_id={args.inn!r}", file=sys.stderr)
        con.close()
        return 1
    issuer_name = issuer_row["name"]

    audit = promotion_audit.run_audit(con, args.inn)
    print(promotion_audit.format_report(audit, issuer_name=issuer_name))
    print(f"\nSTATUS: {audit['status']}")

    reason = args.reason
    if audit["blocking"]:
        if not args.force:
            print("\nBLOCKED — re-run with --force to override.", file=sys.stderr)
            con.close()
            return 1
        print("\nBLOCKED conditions present — proceeding because --force was passed.",
              file=sys.stderr)
        reason = f"{reason} [FORCED past blocking audit: {'; '.join(audit['blocking'])}]"
    elif audit["warnings"] and not args.force:
        print("\nWARNINGS FOUND", file=sys.stderr)
        answer = input("Proceed? (y/n): ").strip().lower()
        if answer != "y":
            print("Отменено.")
            con.close()
            return 0

    try:
        result = db.set_watchlist_tier(con, args.inn, args.tier, reason=reason)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        con.close()
        return 1
    con.commit()
    con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_discover(args) -> int:
    from . import discover
    report = discover.run_gap_check(Path(args.watchlist))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    gap_issuers = [r for r in report["issuers"] if r["untracked"]]
    print(f"\n{report['total_untracked']} untracked bond(s) across "
          f"{len(gap_issuers)}/{report['issuers_checked']} issuer(s).", file=sys.stderr)
    for r in gap_issuers:
        print(f"  {r['name']} ({r['issuer_id']}): tracked={r['tracked_count']} "
              f"discovered={r['discovered_count']}", file=sys.stderr)
        for u in r["untracked"]:
            dur = f"{u['mod_dur']:.2f}y" if u["mod_dur"] is not None else "н/д"
            print(f"      {u['secid']:<14} mat={u['mat_date'] or '—':<11} dur={dur}",
                  file=sys.stderr)
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
    summary = financials_pipeline.run_financials(None, api_key=args.key,
                                                 inn=args.inn)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if "error" in summary:
        print(f"\n{summary['error']}", file=sys.stderr)
        return 1
    print(f"\n{len(summary['issuers'])} issuer(s) scored, "
          f"{len(summary['errors'])} error(s).", file=sys.stderr)
    return 0


def cmd_scan_company(args) -> int:
    from . import company_scan_pipeline as csp
    from .store import db

    print("DEPRECATED: prefer asking Claude Code to run the company-scan skill "
          "(skills/company-scan/SKILL.md) -- no ANTHROPIC_API_KEY needed. This keyed "
          "path is kept working for scripted/cron use only.", file=sys.stderr)
    con = db.connect()
    db.init_db(con)
    result = csp.scan_company(con, args.query, api_key=args.key)
    con.close()
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in result.items() if k != "card"},
                     ensure_ascii=False, indent=2))
    card = result.get("card")
    if card:
        print(f"\n{card.get('issuer_name', args.query)}: "
              f"issuer={card.get('issuer_quality', '?')} "
              f"bond={card.get('bond_suitability', '?')} "
              f"confidence={card.get('confidence', '?')} "
              f"coverage={card.get('data_coverage_pct', '?')}%", file=sys.stderr)
    else:
        print(f"\nNo JSON addendum parsed -- see raw_markdown in "
              f"company_scan_result id={result['id']}.", file=sys.stderr)
    return 0


def cmd_scan_candidates(args) -> int:
    from . import company_scan_pipeline as csp
    from .store import db

    print("DEPRECATED: prefer asking Claude Code to run the company-scan skill "
          "across `hybond scan-candidates-list`'s output in-session -- no "
          "ANTHROPIC_API_KEY needed. This keyed path is kept working for "
          "scripted/cron use only.", file=sys.stderr)
    con = db.connect()
    db.init_db(con)
    limit = None if args.limit == 0 else args.limit
    summary = csp.run_scan_candidates(
        con, api_key=args.key, limit=limit, include_rescan=not args.no_rescan,
        log=lambda s: print(s, file=sys.stderr))
    con.close()
    if "error" in summary:
        print(summary["error"], file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                     ensure_ascii=False, indent=2))
    print(f"\n{summary['scanned_this_run']} scanned, {summary['errors']} error(s), "
          f"{summary['skipped_by_limit']} left for a later run (--limit), "
          f"{summary['candidates_found']} candidate(s) found, "
          f"{summary['already_scanned']} previously scanned.", file=sys.stderr)
    return 0


def cmd_scan_candidates_list(args) -> int:
    """No-key todo list for the company-scan skill's batch mode: same
    candidate+rescan-due list run_scan_candidates() acts on, printed as JSON
    with no scanning and no API key required."""
    from . import company_scan_pipeline as csp
    from .store import db

    con = db.connect()
    db.init_db(con)
    todo = csp.list_scan_todo(con, include_rescan=not args.no_rescan)
    con.close()
    print(json.dumps(todo, ensure_ascii=False, indent=2))
    print(f"\n{len(todo)} due for a scan (new candidates + rescan-due).", file=sys.stderr)
    return 0


def cmd_scan_company_decide(args) -> int:
    """Record the user's own follow-up call (promote/hold/avoid + why) on one
    already-stored company_scan_result row, by its `id`. Distinct from the
    scan's own issuer_quality/bond_suitability verdict -- that's the model's
    assessment; this is what the user actually decided to do about it."""
    from .store import db

    con = db.connect()
    db.init_db(con)
    db.init_company_scan(con)
    try:
        db.set_company_scan_decision(con, args.id, args.decision, args.reason)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        con.close()
        return 1
    con.commit()
    row = con.execute(
        "SELECT id, inn, issuer_name, decision, decision_reason, decision_at "
        "FROM company_scan_result WHERE id=?", (args.id,)).fetchone()
    con.close()
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_scan_company_store(args) -> int:
    """Persist one scan the company-scan Claude Code skill already produced
    (its own web search, no API call from this script) -- reads the
    markdown report and, if present, the parsed JSON addendum from files and
    writes a company_scan_result row via store_scan_result(), same schema
    and downstream dashboard rendering as the deprecated keyed path."""
    from . import company_scan_pipeline as csp
    from .store import db

    markdown = Path(args.markdown).read_text(encoding="utf-8")
    card = None
    if args.card:
        card_text = Path(args.card).read_text(encoding="utf-8")
        try:
            card = json.loads(card_text)
        except json.JSONDecodeError as e:
            print(f"--card file is not valid JSON: {e}", file=sys.stderr)
            return 1

    con = db.connect()
    db.init_db(con)
    result = csp.store_scan_result(con, query=args.query, markdown=markdown, card=card,
                                    model=args.model)
    con.close()
    print(json.dumps({k: v for k, v in result.items() if k != "card"},
                     ensure_ascii=False, indent=2))
    if card:
        print(f"\n{card.get('issuer_name', args.query)}: "
              f"issuer={card.get('issuer_quality', '?')} "
              f"bond={card.get('bond_suitability', '?')} "
              f"confidence={card.get('confidence', '?')} "
              f"coverage={card.get('data_coverage_pct', '?')}%", file=sys.stderr)
    else:
        print(f"\nNo JSON addendum given -- see raw_markdown in "
              f"company_scan_result id={result['id']}.", file=sys.stderr)
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
    pr.add_argument("--watchlist", help="optional watchlist.json fallback enrichment "
                                        "source (secid metadata); issuer scope always "
                                        "comes from the issuer table, not this file")
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
    ps.add_argument("--watchlist", help="optional watchlist.json fallback enrichment "
                                        "source; issuer scope always comes from the "
                                        "issuer table, not this file")
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
    pq.add_argument("--skip-noise", action="store_true",
                     help="bulk-mark all noise-pattern-matching filings as classified "
                          "(category=routine, severity=noise) without running them "
                          "through hy-event-classify -- prompts for confirmation first")
    pq.set_defaults(func=cmd_queue)

    pmq = sub.add_parser("market-notes-queue",
                          help="Show market_note rows awaiting classification")
    pmq.set_defaults(func=cmd_market_notes_queue)

    pac = sub.add_parser("auto-classify",
                          help="Classify unclassified filings/market_notes via local Ollama "
                               "(qwen3.5:latest) -- automated stand-in for hy-event-classify")
    pac.add_argument("--limit", type=int, default=None,
                      help="max filings+market_notes to process this run (default: no limit)")
    pac.add_argument("--dry-run", action="store_true",
                      help="classify and print stats but do not write to the DB")
    pac.add_argument("--priority-only", action="store_true",
                      help="only process rows classify_utils.queue_bucket() marks 'priority'")
    pac.add_argument("--source", choices=["filing", "market_note"], default=None,
                      help="restrict to one queue (default: both)")
    pac.add_argument("--pause", type=int, default=None,
                      help="burst mode: classify --limit items, close the DB "
                           "connection, sleep this many seconds, repeat until "
                           "the queue is empty. With --pause, --limit is the "
                           "per-burst size, not a total budget -- lets long "
                           "backlog runs release the DB between bursts instead "
                           "of holding one connection for the whole run.")
    pac.set_defaults(func=cmd_auto_classify)

    prq = sub.add_parser("review-queue",
                          help="Interactively review event rows Ollama flagged needs_review=1")
    prq.add_argument("--limit", type=int, default=None)
    prq.set_defaults(func=cmd_review_queue)

    pfpi = sub.add_parser("fill-price-impact",
                           help="Backfill event.price_before_bp/price_after_*_bp from "
                                "spread_snapshot (BONDEM-layer prep)")
    pfpi.set_defaults(func=cmd_fill_price_impact)
    pm = sub.add_parser("market-notes",
                        help="Ingest smart-lab.ru's general news RSS feed into market_note "
                             "(secondary business-news signal, distinct from filing/event)")
    pm.set_defaults(func=cmd_market_notes)
    pu = sub.add_parser("universe", help="Full MOEX corporate-bond universe screen + G-spread + mispricing")
    pu.add_argument("--refresh-emitent-map", action="store_true",
                     help="force a fresh paginated SECID->ИНН fetch instead of the cached "
                          "emitent_map table (normally refreshed weekly)")
    pu.set_defaults(func=cmd_universe)
    po = sub.add_parser("ofz", help="OFZ (government bond) universe: real MOEX data + carry/roll-down projection")
    po.set_defaults(func=cmd_ofz)
    pb = sub.add_parser("bonds", help="Slice 1: all outstanding MOEX series for one issuer by ИНН")
    pb.add_argument("inn")
    pb.set_defaults(func=cmd_bonds)
    pvs = sub.add_parser("vdo-screen",
                         help="Broad ВДО screen: read the vdo_scope flag already "
                              "computed by the daily `hybond universe` run "
                              "(rating band-or-unrated + spread + liquidity floor, "
                              "bank sub/perp excluded) -- no network calls")
    pvs.add_argument("--top", type=int, default=20,
                     help="how many top-by-AEY-risk-adj bonds to list (default 20)")
    pvs.set_defaults(func=cmd_vdo_screen)
    ppr = sub.add_parser("promote",
                         help="Phase 2: rank the ВДО-scope screen down to a deep-"
                              "monitoring candidate shortlist (rating floor -> "
                              "liquidity floor -> AEY-risk-adj ranking -> issuer "
                              "concentration cap). On-demand only -- not scheduled. "
                              "Thresholds are a starting point, not final policy.")
    ppr.add_argument("--rating-floor", default=None,
                     help="e.g. ruB- (default: ruB-)")
    ppr.add_argument("--n-before-cap", type=int, default=None,
                     help="how many bonds survive to the concentration-cap stage (default 32)")
    ppr.add_argument("--cap", type=int, default=None,
                     help="max bonds per issuer after ranking (default 2)")
    ppr.add_argument("--target", type=int, default=None,
                     help="final candidate list size after capping (default 25)")
    ppr.add_argument("--inn", default=None,
                     help="manually flag one issuer (by ИНН) for the promotion report -- "
                          "for issuers with no universe_snapshot row (e.g. found via "
                          "company-scan), not a watchlist change. Requires --name.")
    ppr.add_argument("--name", default=None,
                     help="issuer name to go with --inn")
    ppr.add_argument("--note", default=None,
                     help="optional free-text note to store with --inn/--name")
    ppr.add_argument("--confirm", metavar="INN", default=None,
                     help="promote a manual_candidate (flagged via --inn/--name) "
                          "into a real issuer row at the given --tier; removes "
                          "the ИНН from manual_candidate")
    ppr.add_argument("--tier", type=int, choices=(1, 2), default=2,
                     help="watchlist tier for --confirm (default: 2, passive "
                          "monitoring -- tier=1 active monitoring requires "
                          "explicitly passing --tier 1)")
    ppr.set_defaults(func=cmd_promote)
    pwd = sub.add_parser("watchlist-demote",
                          help="Move an existing tier=1 issuer to tier=2 "
                               "(passive monitoring), logged to watchlist_tier_log")
    pwd.add_argument("inn")
    pwd.add_argument("--reason", required=True, help="free-text reason for the demotion")
    pwd.set_defaults(func=cmd_watchlist_demote)
    pwp = sub.add_parser("watchlist-promote",
                          help="Move an existing tier=2 issuer to tier=1 "
                               "(active watchlist), logged to watchlist_tier_log")
    pwp.add_argument("inn")
    pwp.add_argument("--tier", type=int, choices=(1,), default=1,
                      help="target tier (only 1 is meaningful here -- use "
                           "watchlist-demote to go the other way)")
    pwp.add_argument("--reason", required=True, help="free-text reason for the promotion")
    pwp.add_argument("--force", action="store_true",
                      help="proceed past a BLOCKED pre-promotion audit result "
                           "(the override is recorded in the stored reason)")
    pwp.set_defaults(func=cmd_watchlist_promote)
    pdis = sub.add_parser("discover",
                          help="On-demand audit: MOEX-discovered bonds vs. watchlist.json "
                               "coverage, per issuer (manual tool, not part of daily_run.sh)")
    pdis.add_argument("watchlist", nargs="?", default="watchlist.json")
    pdis.set_defaults(func=cmd_discover)
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
                        help="Slice 4: fetch ГИР БО financials + compute pillars "
                             "(all tier=1+2 issuers, or a single issuer via --inn)")
    pf.add_argument("--key", help="parser-api key (or set PARSER_API_KEY)")
    pf.add_argument("--inn", help="run financials for a single issuer ИНН only, "
                                  "instead of the full tier=1+2 watchlist")
    pf.set_defaults(func=cmd_financials)
    psc = sub.add_parser("scan-company",
                         help="[DEPRECATED, needs a key] LLM company-scan for an issuer "
                              "not yet in hybond -- prefer the company-scan Claude Code "
                              "skill instead (skills/company-scan/SKILL.md), no key needed")
    psc.add_argument("query", help="ИНН or company name to screen")
    psc.add_argument("--key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    psc.set_defaults(func=cmd_scan_company)
    pscb = sub.add_parser("scan-candidates",
                          help="[DEPRECATED, needs a key] Batch company-scan -- prefer "
                               "`hybond scan-candidates-list` + the company-scan skill instead")
    pscb.add_argument("--key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    pscb.add_argument("--limit", type=int, default=10,
                      help="max NEW scans this run, guards against an accidental "
                           "full-batch run (default 10; pass 0 for unlimited)")
    pscb.add_argument("--no-rescan", action="store_true",
                      help="only scan brand-new candidates, skip the rescan-due list")
    pscb.set_defaults(func=cmd_scan_candidates)
    pscl = sub.add_parser("scan-candidates-list",
                          help="No-key todo list (new candidates + rescan-due) for the "
                               "company-scan skill's batch mode -- prints JSON, scans nothing")
    pscl.add_argument("--no-rescan", action="store_true",
                      help="only list brand-new candidates, skip the rescan-due list")
    pscl.set_defaults(func=cmd_scan_candidates_list)
    pscs = sub.add_parser("scan-company-store",
                          help="Persist one company-scan skill result (markdown report + "
                               "optional JSON addendum, both already produced by Claude Code) "
                               "into company_scan_result -- no API call, no key")
    pscs.add_argument("query", help="ИНН or company name that was screened")
    pscs.add_argument("--markdown", required=True,
                      help="path to a file with the full Step-16 markdown report")
    pscs.add_argument("--card",
                      help="path to a file with the parsed JSON rendering-addendum "
                           "(omit if the skill produced no JSON block)")
    pscs.add_argument("--model", default="claude-code-skill",
                      help="free-text provenance tag stored in the `model` column "
                           "(default: claude-code-skill)")
    pscs.set_defaults(func=cmd_scan_company_store)
    pscd = sub.add_parser("scan-company-decide",
                           help="Record the user's promote/hold/avoid call on one "
                                "company_scan_result row, with a reason")
    pscd.add_argument("id", type=int, help="company_scan_result.id to mark")
    pscd.add_argument("--decision", required=True, choices=sorted(("promote", "hold", "avoid")),
                       help="what the user decided to do about this scan result")
    pscd.add_argument("--reason", required=True, help="free-text reason for the decision")
    pscd.set_defaults(func=cmd_scan_company_decide)
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
