"""MOEX CCI agency ratings: scale mapping, ISS parsing and fetch safety, the
agency_rating store, the resolver (ratings.py) and the daily refresh
(ratings_pipeline.py). Fully offline: ISS responses are real recorded
payloads under tests/fixtures/moex_cci/ (see its README).
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hybond import ratings, ratings_pipeline, universe_pipeline  # noqa: E402
from hybond.compute import default_probs, inflection  # noqa: E402
from hybond.ingest import moex_ratings as mr  # noqa: E402
from hybond.store import agency_ratings, db  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "moex_cci"
TODAY = date(2026, 9, 17)
NOW = datetime(2026, 9, 17, 10, 0)


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def rows(name):
    return mr.parse_payload(fixture(name))[0]


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "r.sqlite")
    db.init_db(c)
    db.init_rating(c)
    db.init_universe(c)
    db.init_emitent_map(c)
    agency_ratings.init(c)
    yield c
    c.close()


def store(con, name, emitent_id, inn, isin="", today=TODAY):
    agency_ratings.apply_fetch(con, emitent_id=emitent_id, isin=isin, inn=inn, rows=rows(name),
                               today=today, fetched_at=NOW.isoformat(), outcome="ok",
                               empty_confirm_days=3)


# ---- scale mapping ---------------------------------------------------------

@pytest.mark.parametrize("book,raw,grade", [
    (1, "BBB+(RU)", "ruBBB+"), (18, "ruBB-", "ruBB-"), (29, "CCC.ru", "ruCCC"),
    (32, "AA|ru|", "ruAA"), (1, "C(RU)", "ruC"), (29, "B.ru", "ruB"),
])
def test_national_scales_map_to_canonical_grades(book, raw, grade):
    assert mr.normalize_level(book, raw) == (grade, "live")
    assert default_probs.lookup(grade) is not None


@pytest.mark.parametrize("book,raw", [
    (1, "SD(RU)"), (1, "D(RU)"), (18, "ruRD"), (18, "ruD"), (29, "D.ru"),
    (32, "RD|ru|"), (32, "SD|ru|"), (32, "D|ru|"),
])
def test_default_family_maps_to_d_on_every_scale(book, raw):
    assert mr.normalize_level(book, raw) == ("D", "default")


def test_nra_en_dash_is_a_minus():
    assert mr.normalize_level(32, "BB–|ru|") == ("ruBB-", "live")


def test_withdrawn_and_suspended_have_no_grade():
    assert mr.normalize_level(18, "Отозван") == (None, "withdrawn")
    assert mr.normalize_level(1, "Приостановлен") == (None, "suspended")


def test_structured_finance_is_its_own_scale():
    assert mr.normalize_level(2, "AAA(ru.sf)") == ("ruAAA.sf", "live")
    assert mr.normalize_level(20, "ruBB+.sf") == ("ruBB+.sf", "live")
    assert mr.scale_kind(2) == mr.scale_kind(20) == "sf"
    assert mr.scale_kind(19) == "structural"


def test_standalone_assessments_and_unknown_text_are_never_grades():
    assert mr.scale_kind(11) == "osk"
    assert mr.normalize_level(11, "aaa(ru)") == (None, "unknown")
    assert mr.normalize_level(43, "bb+.ru") == (None, "unknown")
    assert mr.normalize_level(1, "AAAA(RU)") == (None, "unknown")
    assert mr.scale_kind(999, "ruA") == "other"


# ---- payload parsing -------------------------------------------------------

def test_parse_issuer_payload():
    parsed, cursor = mr.parse_payload(fixture("issuer_rzd"))
    assert [(r.agency, r.book_id, r.grade, r.rated_on) for r in parsed] == [
        ("АКРА", 1, "ruAAA", "2026-06-23"), ("Эксперт РА", 18, "ruAAA", "2026-06-30")]
    assert cursor == {"INDEX": 0, "TOTAL": 2, "PAGESIZE": 100}


def test_parse_issue_payload_has_no_cursor():
    parsed, cursor = mr.parse_payload(fixture("issue_x5_finance"))
    assert {r.agency for r in parsed} == {"АКРА", "Эксперт РА", "НКР"}
    assert cursor is None


def test_payload_without_rating_block_is_an_error_not_an_empty_list():
    with pytest.raises(ValueError):
        mr.parse_payload({"securities": {"columns": [], "data": []}})


# ---- client: spacing, backoff, denial detection -----------------------------

class Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def client_with(responses, sleeps=None):
    it = iter(responses)

    def get(url, params=None, timeout=None, headers=None):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    sleeps = sleeps if sleeps is not None else []
    return mr.MoexRatingsClient(get=get, sleep=sleeps.append, clock=lambda: 0.0)


def test_html_or_subscriber_only_body_is_denied_even_with_http_200():
    c = client_with([Resp(200, text="<html><body>blocked</body></html>"),
                     Resp(200, text="Информация доступна только подписчикам")])
    assert c.fetch(mr.issuer_url(1)).outcome == "denied"
    assert c.fetch(mr.issuer_url(2)).outcome == "denied"


@pytest.mark.parametrize("status", [403, 429])
def test_block_statuses_stop_immediately_without_retry(status):
    c = client_with([Resp(status, text="")])
    with pytest.raises(mr.RatingsBlocked):
        c.fetch(mr.issuer_url(1))
    assert c.calls == 1


def test_server_errors_back_off_then_succeed():
    sleeps = []
    c = client_with([Resp(502, text=""), requests.ConnectionError("reset"),
                     Resp(200, fixture("issuer_rzd"))], sleeps)
    res = c.fetch(mr.issuer_url(712))
    assert res.outcome == "ok" and len(res.rows) == 2
    assert list(mr.RETRY_BACKOFF_S) == [s for s in sleeps if s in mr.RETRY_BACKOFF_S]


def test_persistent_errors_end_as_error_outcome():
    c = client_with([Resp(503, text="")] * (len(mr.RETRY_BACKOFF_S) + 1))
    assert c.fetch(mr.issuer_url(1)).outcome == "error"


def test_calls_are_spaced():
    sleeps = []
    c = client_with([Resp(200, fixture("issuer_empty")), Resp(200, fixture("issuer_empty"))], sleeps)
    c.fetch(mr.issuer_url(1))
    c.fetch(mr.issuer_url(2))
    assert sleeps == [mr.REQUEST_SPACING_S]


def test_issuer_pagination_follows_the_cursor():
    page = fixture("issuer_rzd")
    page1 = json.loads(json.dumps(page))
    page1["cci_rating_companies"]["data"] = page["cci_rating_companies"]["data"][:1]
    page1["cci_rating_companies.cursor"]["data"] = [[0, 2, 1]]
    page2 = json.loads(json.dumps(page))
    page2["cci_rating_companies"]["data"] = page["cci_rating_companies"]["data"][1:]
    page2["cci_rating_companies.cursor"]["data"] = [[1, 2, 1]]
    c = client_with([Resp(200, page1), Resp(200, page2)])
    assert len(c.fetch(mr.issuer_url(712)).rows) == 2


# ---- store ------------------------------------------------------------------

def test_store_upserts_and_keeps_history_of_a_downgrade(con):
    store(con, "issuer_seligdar_split", 4579, "1402047184")
    older = fixture("issuer_seligdar_split")
    older["cci_rating_companies"]["data"][0][6] = "ruA+"
    older["cci_rating_companies"]["data"][0][4] = "2025-08-28 00:00:00"
    agency_ratings.apply_fetch(con, emitent_id=4579, isin="", inn="1402047184",
                               rows=mr.parse_payload(older)[0], today=date(2026, 9, 18),
                               fetched_at=NOW.isoformat(), outcome="ok", empty_confirm_days=3)
    change = agency_ratings.apply_fetch(
        con, emitent_id=4579, isin="", inn="1402047184", rows=rows("issuer_seligdar_split"),
        today=date(2026, 9, 19), fetched_at=NOW.isoformat(), outcome="ok", empty_confirm_days=3)
    assert ("Эксперт РА", 18, "ruA+", "ruA") in change["changed"]
    hist = con.execute("SELECT raw_level FROM agency_rating_history WHERE agency='Эксперт РА'").fetchall()
    assert {h["raw_level"] for h in hist} == {"ruA", "ruA+"}
    cur = agency_ratings.issuer_rows_by_inn(con, "1402047184")
    assert {r["raw_level"] for r in cur if r["agency"] == "Эксперт РА"} == {"ruA"}


def test_rating_dropped_from_a_populated_response_is_removed(con):
    store(con, "issuer_anterra_default", 12725, "7730176955")
    payload = fixture("issuer_anterra_default")
    payload["cci_rating_companies"]["data"] = payload["cci_rating_companies"]["data"][1:]
    agency_ratings.apply_fetch(con, emitent_id=12725, isin="", inn="7730176955",
                               rows=mr.parse_payload(payload)[0], today=TODAY,
                               fetched_at=NOW.isoformat(), outcome="ok", empty_confirm_days=3)
    assert [r["agency"] for r in agency_ratings.issuer_rows_by_inn(con, "7730176955")] == ["НРА"]


def test_empty_response_keeps_known_ratings_until_confirmed(con):
    store(con, "issuer_rzd", 712, "7708503727")
    for day, expected in ((17, 2), (18, 2), (19, 2), (20, 0)):
        agency_ratings.apply_fetch(con, emitent_id=712, isin="", inn="7708503727", rows=[],
                                   today=date(2026, 9, day), fetched_at=NOW.isoformat(),
                                   outcome="empty", empty_confirm_days=3)
        assert len(agency_ratings.issuer_rows_by_inn(con, "7708503727")) == expected, day


# ---- resolver ---------------------------------------------------------------

def test_split_ratings_resolve_to_the_most_conservative(con):
    store(con, "issuer_seligdar_split", 4579, "1402047184")
    res = ratings.resolve(con, inn="1402047184", as_of=TODAY)
    assert (res["grade"], res["agency"], res["split"], res["level"]) == ("ruA", "Эксперт РА", True, "issuer")
    assert [a["agency"] for a in res["agencies"] if a["used"]] == ["Эксперт РА"]


def test_issue_level_rating_beats_issuer_level(con):
    store(con, "issuer_x5_finance_withdrawn", 1259, "7715630469")
    store(con, "issue_x5_finance", 1259, "7715630469", isin="RU000A1075S4")
    bond = ratings.resolve(con, inn="7715630469", secid="RU000A1075S4", as_of=TODAY)
    assert (bond["grade"], bond["level"], bond["split"]) == ("ruAAA", "issue", False)
    issuer = ratings.resolve(con, inn="7715630469", as_of=TODAY)
    assert issuer["grade"] is None and issuer["withdrawn_on"] == "2024-08-12"


def test_withdrawn_agency_drops_out_but_live_one_remains(con):
    # МГКЛ: Эксперт РА withdrew 2026-09-14, НРА still rates it
    store(con, "issuer_mgkl_withdrawn", 11004, "7707600245")
    res = ratings.resolve(con, inn="7707600245", as_of=TODAY)
    assert (res["grade"], res["agency"], res["split"], res["withdrawn_on"]) == ("ruBB", "НРА", False, None)


def test_default_is_surfaced_and_never_gets_a_pd(con):
    store(con, "issuer_anterra_default", 12725, "7730176955")
    res = ratings.resolve(con, inn="7730176955", as_of=TODAY)
    assert (res["grade"], res["defaulted"]) == ("D", True)
    assert default_probs.lookup("D") is None
    assert default_probs.implied_fair_spread_bp("D") is None
    assert default_probs.rating_rank("D") > default_probs.rating_rank("ruC")
    assert universe_pipeline.is_in_vdo_rating_band("D")
    assert inflection.classify_yield_zone(0.25, 900, "D") == "negative_ev"


def test_standalone_assessments_do_not_create_a_split(con):
    store(con, "issuer_sber_osk", 484, "7707083893")
    res = ratings.resolve(con, inn="7707083893", as_of=TODAY)
    assert (res["grade"], res["split"]) == ("ruAAA", False)
    assert all(a["scale"] == "national" for a in res["agencies"])


def test_structured_finance_is_displayed_not_modelled(con):
    store(con, "issue_titan_sf", 11269, "7704370755", isin="RU000A102MB3")
    res = ratings.resolve(con, inn="7704370755", secid="RU000A102MB3", as_of=TODAY)
    assert res["grade"] is None and res["sf_grade"] == "ruAAA.sf"


def test_legacy_rating_is_the_fallback_when_moex_has_nothing(con):
    db.upsert_issuer(con, "7700000001", "ООО Тест")
    db.upsert_rating(con, "7700000001", "manual", "ruB", None, None, "2026-07-27")
    db.upsert_rating(con, "7700000001", "НКР", "ruBB-", None, None, "2026-05-01")
    res = ratings.resolve(con, inn="7700000001", as_of=TODAY)
    assert (res["grade"], res["agency"], res["source"]) == ("ruBB-", "НКР", "legacy")


def test_stale_legacy_rating_loses_to_newer_moex_action(con):
    # Центр-резерв: we held АКРА ruC, АКРА moved it to D on 2026-09-10
    db.upsert_issuer(con, "5612069520", "Центр-резерв")
    db.upsert_rating(con, "5612069520", "АКРА", "ruC", None, None, "2025-12-01")
    store(con, "issuer_centr_rezerv_default", 12366, "5612069520")
    res = ratings.resolve(con, inn="5612069520", as_of=TODAY)
    assert (res["grade"], res["source"]) == ("D", "moex")


def test_newer_legacy_row_for_the_same_agency_covers_moex_lag(con):
    # МГКЛ's НРА row on MOEX is 2025-11-21; a hand-entered newer НРА action wins
    db.upsert_issuer(con, "7707600245", "МГКЛ")
    db.upsert_rating(con, "7707600245", "НРА", "ruB", None, "downgrade", "2026-09-16")
    db.upsert_rating(con, "7707600245", "manual", "ruBB-", None, None, "2026-09-16")
    store(con, "issuer_mgkl_withdrawn", 11004, "7707600245")
    res = ratings.resolve(con, inn="7707600245", as_of=TODAY)
    assert (res["grade"], res["source"]) == ("ruB", "legacy")


def test_rating_not_reconfirmed_by_moex_is_flagged_stale(con):
    store(con, "issuer_rzd", 712, "7708503727", today=date(2026, 9, 10))
    assert ratings.resolve(con, inn="7708503727", as_of=date(2026, 9, 12))["stale"] is False
    assert ratings.resolve(con, inn="7708503727", as_of=date(2026, 9, 17))["stale"] is True


def test_resolver_tolerates_a_db_without_the_store(tmp_path):
    c = db.connect(tmp_path / "bare.sqlite")
    assert ratings.resolve(c, inn="1", secid="X")["grade"] is None
    c.close()


# ---- daily refresh ----------------------------------------------------------

BONDS = {  # secid -> (emitent_id, inn, issuer fixture)
    "RU000A106K43": (712, "7708503727", "issuer_rzd"),
    "RU000A1075S4": (1259, "7715630469", "issuer_x5_finance_withdrawn"),
    "RU000A0MGKL1": (11004, "7707600245", "issuer_mgkl_withdrawn"),
}


def seed_universe(con, snap="2026-09-16"):
    for secid, (eid, inn, _) in BONDS.items():
        db.insert_universe_snapshot(con, **{c: None for c in (
            "short_name,emitent_title,mat_date,offer_date,coupon_pct,list_level,qualified,"
            "last_pct,yield_moex,yield_to_offer,accrued_int,volume,num_trades,duration_moex,ytm,"
            "tenor_yrs,rf_rate,g_spread_bp,yield_source,rating,implied_fair_spread_bp,"
            "mispricing_bp,in_watchlist,signal_excluded,mod_dur,aey_base,aey_stress,aey_bull,"
            "aey_base_risk_adj").split(",")}, secid=secid, snap_date=snap, emitent_inn=inn)
    db.replace_emitent_map(con, {s: {"emitent_inn": inn, "emitent_title": None, "emitent_id": eid,
                                     "isin": s} for s, (eid, inn, _) in BONDS.items()},
                           "2026-09-14")
    con.commit()


class FakeISS:
    def __init__(self, override=None):
        self.urls = []
        self.override = override or {}

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.urls.append(url)
        if url in self.override:
            return self.override[url]
        for secid, (eid, _, name) in BONDS.items():
            if url == mr.issuer_url(eid):
                return Resp(200, fixture(name))
            if url == mr.issue_url(eid, secid):
                return Resp(200, fixture("issue_x5_finance" if eid == 1259 else "issuer_empty"))
        return Resp(200, fixture("issuer_empty"))


def refresh(con, iss, now=NOW, **kw):
    client = mr.MoexRatingsClient(get=iss, sleep=lambda s: None)
    return ratings_pipeline.run_refresh(con, client=client, today=now.date(), now=now,
                                        snapshot=False, refresh_emitent_map=False, **kw)


def test_refresh_skips_issue_calls_where_the_issuer_rating_resolves(con):
    seed_universe(con)
    iss = FakeISS()
    r = refresh(con, iss)
    assert (r["issuer_calls"], r["issue_calls"]) == (3, 1)
    assert mr.issue_url(1259, "RU000A1075S4") in iss.urls
    assert ratings.resolve(con, inn="7715630469", secid="RU000A1075S4", as_of=TODAY)["grade"] == "ruAAA"
    assert ratings.resolve(con, inn="7708503727", secid="RU000A106K43", as_of=TODAY)["grade"] == "ruAAA"


def test_refresh_is_incremental_within_the_ttl(con):
    seed_universe(con)
    refresh(con, FakeISS())
    again = FakeISS()
    r = refresh(con, again, now=datetime(2026, 9, 17, 16, 0))
    assert again.urls == [] and r["skipped_fresh"] == 4
    next_day = FakeISS()
    refresh(con, next_day, now=datetime(2026, 9, 18, 10, 0))
    assert len(next_day.urls) == 4


def test_refresh_budget_defers_the_rest_to_the_next_run(con):
    seed_universe(con)
    r = refresh(con, FakeISS(), max_calls=2)
    assert r["calls"] == 2 and r["deferred"] >= 1 and r["issue_calls"] == 0


def test_blocked_refresh_stops_logs_and_keeps_known_ratings(con):
    seed_universe(con)
    refresh(con, FakeISS())
    blocked = FakeISS({mr.issuer_url(eid): Resp(429, text="") for eid, _, _ in BONDS.values()})
    r = refresh(con, blocked, now=datetime(2026, 9, 18, 10, 0))
    assert r["blocked"] and len(blocked.urls) == 1
    codes = [row["code"] for row in con.execute("SELECT code FROM pipeline_health")]
    assert "MOEX_RATINGS_BLOCKED" in codes
    assert ratings.resolve(con, inn="7708503727", as_of=date(2026, 9, 18))["grade"] == "ruAAA"


def test_mass_emptiness_is_treated_as_denial_not_as_withdrawal(con, monkeypatch):
    monkeypatch.setattr(ratings_pipeline, "EMPTIED_MIN_COUNT", 2)
    seed_universe(con)
    refresh(con, FakeISS())
    empty = FakeISS({mr.issuer_url(eid): Resp(200, fixture("issuer_empty"))
                     for eid, _, _ in BONDS.values()})
    refresh(con, empty, now=datetime(2026, 9, 21, 10, 0))
    codes = [row["code"] for row in con.execute("SELECT code FROM pipeline_health")]
    assert "MOEX_RATINGS_EMPTY" in codes
    assert len(agency_ratings.issuer_rows_by_inn(con, "7708503727")) == 2
    assert ratings.resolve(con, inn="7708503727", as_of=date(2026, 9, 21))["stale"] is True


def test_new_default_is_logged(con):
    seed_universe(con)
    iss = FakeISS({mr.issuer_url(712): Resp(200, fixture("issuer_centr_rezerv_default"))})
    refresh(con, iss)
    detail = [row["detail"] for row in con.execute(
        "SELECT detail FROM pipeline_health WHERE code='MOEX_RATINGS_DEFAULT'")]
    assert detail and "D(RU)" in detail[0]


# ---- emitent map and export ---------------------------------------------------

def test_emitent_map_keeps_emitent_id_and_refreshes_an_old_cache(con, monkeypatch):
    con.execute("INSERT INTO emitent_map(secid, emitent_inn, emitent_title, fetched_on) "
                "VALUES('RU000A106K43', '7708503727', 'РЖД', '2026-09-16')")
    fetched = {"RU000A106K43": {"emitent_inn": "7708503727", "emitent_title": "РЖД",
                                "emitent_id": 712, "isin": "RU000A106K43"}}
    monkeypatch.setattr(universe_pipeline.sources, "fetch_emitent_map", lambda: fetched)
    out = universe_pipeline.load_emitent_map(con, TODAY, force_refresh=False)
    assert out["RU000A106K43"]["emitent_id"] == 712
    assert db.get_emitent_map(con)["RU000A106K43"]["emitent_id"] == 712


def test_missing_new_placements_are_looked_up_individually(con, monkeypatch):
    emap = {"RU000A106K43": {"emitent_inn": "7708503727", "emitent_id": 712}}
    calls = []

    def fake(secids):
        calls.append(list(secids))
        return {s: {"emitent_inn": "7750004150", "emitent_title": "ВЭБ.РФ", "emitent_id": 406,
                    "isin": s} for s in secids}
    monkeypatch.setattr(universe_pipeline.sources, "fetch_emitent_rows_for_secids", fake)
    assert universe_pipeline.fill_missing_emitents(con, emap, ["RU000A106K43", "RU000A10NEW1"], TODAY) == 1
    assert calls == [["RU000A10NEW1"]] and emap["RU000A10NEW1"]["emitent_id"] == 406
    assert universe_pipeline.fill_missing_emitents(con, {}, ["X"], TODAY) == 0


def test_export_carries_per_bond_rating_provenance(con):
    from export_dashboard_data import bond_rating_fields, issuer_ratings_all, ratings_meta
    store(con, "issuer_seligdar_split", 4579, "1402047184")
    res = ratings.resolve(con, inn="1402047184", as_of=TODAY)
    fields = bond_rating_fields(res)
    assert fields["rating_split"] is True and fields["rating_level"] == "issuer"
    assert {a["agency"] for a in fields["rating_agencies"]} == {"Эксперт РА", "НКР", "НРА"}
    assert set(fields["rating_agencies"][0]) == {"agency", "grade", "raw", "rated_on", "scale",
                                                 "status", "level", "source", "used"}
    assert all("watchlist_tier" not in k for k in fields)
    assert [r["rating"] for r in issuer_ratings_all(res) if r["is_current"]].count("ruA+") == 2
    meta = ratings_meta(con, [dict(rating="ruA", **fields), {"rating": None}])
    assert (meta["bonds"], meta["rated"], meta["split"]) == (2, 1, 1)


def test_watchlist_health_sees_a_moex_reported_downgrade(con):
    from hybond import watchlist_health
    db.upsert_issuer(con, "5612069520", "Центр-резерв")
    db.upsert_rating(con, "5612069520", "АКРА", "ruC", None, None, "2026-02-27")
    store(con, "issuer_centr_rezerv_default", 12366, "5612069520")
    assert watchlist_health.rating_drop_notches(con, "5612069520", "2026-09-17") == 1
