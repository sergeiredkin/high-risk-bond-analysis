"""Tests for issuer_bond (Watchlist Tier System follow-up, 2026-08-20):
the persistent issuer_id -> secid mapping that replaces watchlist.json as
the daily pipeline's SECID source, plus list_issuers_by_tier()'s extension
to accept multiple tiers."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.store import db  # noqa: E402


def _con(tmp_path, name="t.sqlite"):
    con = db.connect(tmp_path / name)
    db.init_db(con)
    db.init_universe(con)
    db.init_issuer_bond(con)
    return con


# ---- schema -----------------------------------------------------------------

def test_issuer_bond_table_created(tmp_path):
    con = _con(tmp_path)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(issuer_bond)")}
    assert {"id", "issuer_id", "secid", "added_at", "source", "delisted"} <= cols
    con.close()


def test_add_issuer_bond_rejects_invalid_source(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    con.commit()
    with pytest.raises(ValueError):
        db.add_issuer_bond(con, "1", "SECID1", source="not_a_real_source")
    con.close()


def test_add_issuer_bond_idempotent_does_not_clobber_delisted(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    db.add_issuer_bond(con, "1", "SECID1", source="watchlist_json")
    con.commit()
    con.execute("UPDATE issuer_bond SET delisted=1 WHERE issuer_id='1' AND secid='SECID1'")
    con.commit()
    # Re-adding (as sync_issuer_bond does on every run) must not reset delisted.
    db.add_issuer_bond(con, "1", "SECID1", source="watchlist_json")
    con.commit()
    row = con.execute("SELECT delisted FROM issuer_bond WHERE issuer_id='1' AND secid='SECID1'").fetchone()
    assert row["delisted"] == 1
    con.close()


# ---- sync_issuer_bond: the Гарант-Инвест case --------------------------------

def test_sync_issuer_bond_unions_watchlist_universe_and_bond(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    con.commit()
    # universe_snapshot contributes SECID_U
    con.execute("INSERT INTO universe_snapshot(secid,snap_date,emitent_inn) "
                "VALUES('SECID_U','2026-08-20','1')")
    # bond table contributes SECID_B
    db.upsert_bond(con, secid="SECID_B", isin="RU1", issuer_id="1", short_name="B",
                    mat_date="2028-01-01", face_value=1000.0, face_unit="SUR",
                    coupon_pct=15.0, list_level=3, qualified=1, updated="2026-08-20")
    con.commit()
    watchlist = {"1": {"name": "Co", "secids": ["SECID_W"]}}

    db.sync_issuer_bond(con, watchlist)
    con.commit()
    secids = {r["secid"] for r in con.execute("SELECT secid FROM issuer_bond WHERE issuer_id='1'")}
    assert secids == {"SECID_W", "SECID_U", "SECID_B"}
    con.close()


def test_sync_issuer_bond_preserves_watchlist_only_secid_when_delisted(tmp_path):
    """The Гарант-Инвест case: a secid known only via watchlist.json (fully
    delisted, absent from both universe_snapshot and bond) must survive a
    sync even after watchlist.json is no longer passed in a later run."""
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "7726637843", "Гарант-Инвест", watchlist_tier=2)
    con.commit()
    db.sync_issuer_bond(con, {"7726637843": {"name": "Гарант-Инвест",
                                              "secids": ["RU000A106862", "RU000A108G88"]}})
    con.commit()
    # Simulate MOEX confirming these are gone (real live signal).
    db.refresh_delisted_flags(con, live_secids=set())
    con.commit()

    # A later run with watchlist.json absent (graceful fallback -- {}) must
    # not lose the already-recorded secids.
    db.sync_issuer_bond(con, {})
    con.commit()
    secids = db.list_bonds_for_issuer(con, "7726637843")
    assert set(secids) == {"RU000A106862", "RU000A108G88"}
    con.close()


def test_sync_issuer_bond_graceful_with_no_watchlist(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    con.commit()
    db.sync_issuer_bond(con, None)  # must not raise
    db.sync_issuer_bond(con)  # default arg path
    con.close()


# ---- refresh_delisted_flags ---------------------------------------------------

def test_refresh_delisted_flags_marks_missing_secids(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    con.commit()
    db.add_issuer_bond(con, "1", "STILL_LIVE", source="bond")
    db.add_issuer_bond(con, "1", "NOW_GONE", source="bond")
    con.commit()
    db.refresh_delisted_flags(con, live_secids={"STILL_LIVE"})
    con.commit()
    rows = {r["secid"]: r["delisted"] for r in
            con.execute("SELECT secid, delisted FROM issuer_bond WHERE issuer_id='1'")}
    assert rows == {"STILL_LIVE": 0, "NOW_GONE": 1}
    con.close()


def test_refresh_delisted_flags_relists_symmetrically(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Co", watchlist_tier=1)
    con.commit()
    db.add_issuer_bond(con, "1", "SECID1", source="bond")
    con.commit()
    db.refresh_delisted_flags(con, live_secids=set())  # goes delisted
    con.commit()
    assert con.execute("SELECT delisted FROM issuer_bond WHERE secid='SECID1'").fetchone()["delisted"] == 1
    db.refresh_delisted_flags(con, live_secids={"SECID1"})  # reappears
    con.commit()
    assert con.execute("SELECT delisted FROM issuer_bond WHERE secid='SECID1'").fetchone()["delisted"] == 0
    con.close()


# ---- list_bonds_for_issuer: tier=2 keeps delisted bonds in scope -------------

def test_list_bonds_for_issuer_tier1_excludes_delisted(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    con.commit()
    db.add_issuer_bond(con, "1", "LIVE", source="bond")
    db.add_issuer_bond(con, "1", "DELISTED", source="bond")
    con.commit()
    con.execute("UPDATE issuer_bond SET delisted=1 WHERE secid='DELISTED'")
    con.commit()
    assert db.list_bonds_for_issuer(con, "1") == ["LIVE"]
    con.close()


def test_list_bonds_for_issuer_tier2_keeps_delisted(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "2", "Confirmed Default Co", watchlist_tier=2)
    con.commit()
    db.add_issuer_bond(con, "2", "LIVE", source="bond")
    db.add_issuer_bond(con, "2", "DELISTED", source="bond")
    con.commit()
    con.execute("UPDATE issuer_bond SET delisted=1 WHERE secid='DELISTED'")
    con.commit()
    assert set(db.list_bonds_for_issuer(con, "2")) == {"LIVE", "DELISTED"}
    con.close()


# ---- list_issuers_by_tier: multi-tier + scope exclusion ----------------------

def test_list_issuers_by_tier_accepts_tuple(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Tier1", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2", watchlist_tier=2)
    con.commit()
    ids = {r["issuer_id"] for r in db.list_issuers_by_tier(con, (1, 2))}
    assert ids == {"1", "2"}
    con.close()


def test_list_issuers_by_tier_single_int_still_works(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Tier1", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2", watchlist_tier=2)
    con.commit()
    ids = {r["issuer_id"] for r in db.list_issuers_by_tier(con, 1)}
    assert ids == {"1"}
    con.close()


def test_list_issuers_by_tier_excludes_tier_3(tmp_path):
    con = _con(tmp_path)
    db.add_issuer_at_tier(con, "1", "Tier1", watchlist_tier=1)
    con.execute("INSERT INTO issuer(issuer_id,name,watchlist_tier) VALUES('3','Tier3 Co',3)")
    con.commit()
    ids = {r["issuer_id"] for r in db.list_issuers_by_tier(con, (1, 2))}
    assert ids == {"1"}
    con.close()


def test_watchlist_tier_column_is_not_null(tmp_path):
    """issuer.watchlist_tier is NOT NULL DEFAULT 2 -- a NULL tier can't
    exist in the first place, so list_issuers_by_tier() never needs to
    special-case it."""
    con = _con(tmp_path)
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        con.execute("INSERT INTO issuer(issuer_id,name,watchlist_tier) VALUES('4','Co',NULL)")
    con.close()
