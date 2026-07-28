"""Slice 2 tests. Run: PYTHONPATH=tests python -m pytest tests/test_slice2.py -q"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.diff import engine  # noqa: E402
from hybond.store import db  # noqa: E402
from hybond.factlist.assemble import build_factlist, SCHEMA_VERSION  # noqa: E402
from hybond import grade  # noqa: E402


def _hist(spreads):
    """Build newest-first spread history rows from an oldest-first list."""
    rows = []
    d = date(2026, 6, 1)
    for i, s in enumerate(spreads):
        rows.append({"snap_date": (d + timedelta(days=i)).isoformat(),
                     "ytm": 0.2, "g_spread_bp": s, "mod_dur": 1.5})
    return list(reversed(rows))  # newest-first, as the store returns


# --- fixed detector works cold (no history) --------------------------------

def test_fixed_fires_cold():
    flags = engine.detect_spread_move("S1", "I1", today_spread_bp=700,
                                      prior_spread_bp=600, hist_desc=[])
    dets = {f.detector for f in flags}
    assert "fixed" in dets           # 100bp move clears 50bp floor
    assert "stat" not in dets        # no history -> stat silent
    assert "hybrid" not in dets


def test_fixed_ignores_small_move():
    flags = engine.detect_spread_move("S1", "I1", 610, 600, hist_desc=[])
    assert flags == []               # 10bp < 50bp floor


def test_no_flag_on_first_appearance():
    flags = engine.detect_spread_move("S1", "I1", 700, None, hist_desc=[])
    assert flags == []               # no prior -> no delta


# --- statistical detector needs history, then self-calibrates --------------

def test_stat_silent_until_enough_history():
    # only 3 changes of history -> below MIN_HISTORY_FOR_STAT
    hist = _hist([600, 605, 603, 604])
    flags = engine.detect_spread_move("S1", "I1", 700, 604, hist)
    assert all(f.detector != "stat" for f in flags)
    assert not engine.stat_is_live(hist)


def test_stat_fires_on_unusual_move_for_quiet_issuer():
    # a very stable issuer (tiny daily changes) then a big jump
    hist = _hist([600, 601, 600, 602, 601, 600, 601, 602, 601, 600])
    assert engine.stat_is_live(hist)
    flags = engine.detect_spread_move("S1", "I1", today_spread_bp=660,
                                      prior_spread_bp=600, hist_desc=hist)
    dets = {f.detector for f in flags}
    assert "stat" in dets            # 60bp jump is many σ for this quiet name
    assert "hybrid" in dets          # and clears the 25bp floor


def test_hybrid_suppresses_tiny_but_unusual():
    # an ultra-stable issuer where even a 10bp move is >2σ, but 10bp < floor
    hist = _hist([600, 600.5, 600, 600.5, 600, 600.5, 600, 600.5, 600, 600.5])
    flags = engine.detect_spread_move("S1", "I1", today_spread_bp=612,
                                      prior_spread_bp=600.5, hist_desc=hist)
    dets = {f.detector for f in flags}
    # stat may fire (unusual), but hybrid must NOT (11.5bp < 25bp floor)
    assert "hybrid" not in dets


def test_volume_silence_flags_after_trading():
    f = engine.detect_volume_silence("S1", "I1", today_volume=0,
                                     today_trades=0, prior_traded=True)
    assert f and f[0].signal == "volume_zero"
    # but not if it never traded
    assert engine.detect_volume_silence("S1", "I1", 0, 0, prior_traded=False) == []


def test_new_filing_flag():
    f = engine.detect_new_filing("S1", "I1", ["Отчётность за 2025 задержана"])
    assert f and f[0].signal == "new_filing"
    assert engine.detect_new_filing("S1", "I1", []) == []


# --- factlist assembly + schema stability ----------------------------------

def test_factlist_shape(tmp_path):
    con = db.connect(tmp_path / "f.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "I1", "ТестЭмитент", "leasing")
    db.upsert_bond(con, secid="S1", isin="RU000S1", issuer_id="I1",
                   short_name="Тест1", mat_date="2028-07-25", face_value=1000.0,
                   face_unit="SUR", coupon_pct=18.0, list_level=3, qualified=1,
                   updated="2026-07-25")
    # two days of spread so a delta exists
    db.insert_spread_snapshot(con, secid="S1", snap_date="2026-07-24", ytm=0.19,
                              tenor_yrs=2.0, rf_rate=0.14, g_spread_bp=500.0,
                              mod_dur=1.6, convexity=3.1)
    db.insert_spread_snapshot(con, secid="S1", snap_date="2026-07-25", ytm=0.20,
                              tenor_yrs=1.99, rf_rate=0.14, g_spread_bp=600.0,
                              mod_dur=1.59, convexity=3.0)
    db.insert_price_snapshot(con, secid="S1", snap_date="2026-07-25", last_pct=92.0,
                             yield_moex=20.0, duration=580, volume=1_000_000,
                             num_trades=42, systime="2026-07-25 18:00:00")
    con.commit()
    fl = build_factlist(con, "I1", "2026-07-25",
                        flags=[{"detector": "fixed", "signal": "spread_move",
                                "metric": 100.0, "detail": "ΔG-spread +100bp"}],
                        stat_warming_up=True)
    j = fl.to_json()
    assert j["schema_version"] == SCHEMA_VERSION
    assert j["issuer_name"] == "ТестЭмитент"
    b = j["bonds"][0]
    assert b["g_spread_bp"] == 600.0
    assert b["g_spread_delta_bp"] == 100.0     # 600 - 500
    # reserved placeholders present and stable for future slices
    assert j["six_pillars"] == {}
    assert j["events_since_last"] == []
    assert j["thesis_status"] is None
    con.close()


# --- grader stub reports precision with its denominator --------------------

def test_grader_precision_with_denominator(tmp_path):
    con = db.connect(tmp_path / "g.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "I1", "E", None)
    db.upsert_bond(con, secid="S1", isin=None, issuer_id="I1", short_name=None,
                   mat_date=None, face_value=1000.0, face_unit="SUR",
                   coupon_pct=18.0, list_level=3, qualified=0, updated="2026-07-25")
    f1 = db.insert_flag(con, snap_date="2026-07-25", secid="S1", issuer_id="I1",
                        detector="fixed", signal="spread_move", metric=100.0,
                        detail="x", driving=1)
    f2 = db.insert_flag(con, snap_date="2026-07-26", secid="S1", issuer_id="I1",
                        detector="fixed", signal="spread_move", metric=80.0,
                        detail="y", driving=1)
    con.commit()
    # one true, one false
    grade.grade_flag(con, f1, "issuer delayed report 3d later", is_true=True)
    grade.grade_flag(con, f2, "reverted next day, noise", is_true=False)
    s = grade.summarise(con)["fixed"]
    assert s["true_positives"] == 1
    assert s["false_positives"] == 1
    assert s["precision"] == 0.5
    assert s["precision_denominator"] == 2   # thin, and shown as such
    con.close()
