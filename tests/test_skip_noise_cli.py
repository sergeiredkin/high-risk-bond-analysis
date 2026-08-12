"""Regression test for a real bug: cmd_queue's --skip-noise originally built
its target set from classify_queue_view()'s view["noise"], which is capped
at [:50] for DISPLAY purposes -- so with more than 50 noise-matching
filings queued, only the first 50 got marked classified, silently leaving
the rest. --skip-noise must recompute the full noise set directly from
unclassified_filings(), not reuse the capped display list.
"""
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond import __main__ as cli  # noqa: E402
from hybond.store import db  # noqa: E402

ISSUER = "1234567890"
N_NOISE = 55  # > the 50-item display cap classify_queue_view() applies


def test_skip_noise_marks_every_match_not_just_first_50(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "skip_noise.sqlite")
    con = db.connect()
    db.init_db(con)
    db.init_filing(con)
    db.upsert_issuer(con, ISSUER, "Test Issuer", None)
    for i in range(N_NOISE):
        db.insert_filing(con, body_hash=f"noise-{i}", issuer_id=ISSUER, edisclosure_id=ISSUER,
                          published=f"2026-07-{(i % 28) + 1:02d}",
                          title=f"Выплаченные доходы по эмиссионным ценным бумагам эмитента #{i}",
                          url="http://x", fetched_on="2026-07-01")
    # One priority and one normal filing too, to confirm they're untouched.
    db.insert_filing(con, body_hash="priority-1", issuer_id=ISSUER, edisclosure_id=ISSUER,
                      published="2026-07-01", title="Присвоение рейтинга эмитенту понижено",
                      url="http://x", fetched_on="2026-07-01")
    db.insert_filing(con, body_hash="normal-1", issuer_id=ISSUER, edisclosure_id=ISSUER,
                      published="2026-07-01", title="Решения совета директоров",
                      url="http://x", fetched_on="2026-07-01")
    con.commit()
    con.close()

    monkeypatch.setattr("builtins.input", lambda _: "y")
    rc = cli.cmd_queue(Namespace(skip_noise=True))
    assert rc == 0

    con = db.connect()
    remaining = db.unclassified_filings(con)
    assert len(remaining) == 2  # only priority-1 and normal-1 left
    remaining_hashes = {r["body_hash"] for r in remaining}
    assert remaining_hashes == {"priority-1", "normal-1"}

    noise_events = con.execute(
        "SELECT COUNT(*) c FROM event WHERE category='routine' AND severity='noise'"
    ).fetchone()["c"]
    assert noise_events == N_NOISE
    con.close()

    out = capsys.readouterr().out
    assert f"{N_NOISE} filing(s) match a noise pattern" in out
    assert f"Marked {N_NOISE} filing(s)" in out
