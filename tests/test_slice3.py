"""Slice 3 tests. Run: PYTHONPATH=tests python -m pytest tests/test_slice3.py -q

Source is e-disclosure.azipi.ru (plain server-rendered HTML, no JS gate — see
edisclosure.py's module docstring for why e-disclosure.ru was replaced). The
PARSERS are still tested against saved-shape HTML fixtures rather than a live
fetch, so parsing correctness never depends on network access.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybond.scrape import edisclosure as ed  # noqa: E402
from hybond.scrape import smartlab as sl  # noqa: E402
from hybond.store import db  # noqa: E402


# Shaped like the real AZIPI homepage feed: a ".messages-subjects" list of
# ".item" blocks, each with a date, a link to /messages/<id>/, and an "info"
# block carrying the org name + ИНН. Two issuers plus one malformed item
# (no ИНН in its info block — should be skipped, not crash).
HOMEPAGE_FIXTURE_HTML = """
<div class="messages-subjects">
    <div class="item">
        <div class="date">
            27.07.2026 11:31:27
        </div>
        <div class="link">
            <a href="/messages/4570187/" alt="Совершение сделки">Совершение подконтрольной эмитенту организацией существенной сделки</a>
        </div>
        <div class="info">
            Организация: <b>ООО "Брусника. Строительство и девелопмент"</b><br />
            ИНН организации: <b>6685151087</b><br />
            Дата события: <b>24.07.2026</b>
        </div>
    </div>
    <div class="item">
        <div class="date">
            27.07.2026 10:00:00
        </div>
        <div class="link">
            <a href="/messages/4570001/" alt="Просрочка">Сведения о просрочке исполнения обязательства перед кредиторами</a>
        </div>
        <div class="info">
            Организация: <b>ООО "Пример эмитент"</b><br />
            ИНН организации: <b>7714699320</b><br />
            Дата события: <b>26.07.2026</b>
        </div>
    </div>
    <div class="item">
        <div class="date">
            27.07.2026 09:00:00
        </div>
        <div class="link">
            <a href="/messages/4569999/" alt="Служебное">Служебное сообщение без ИНН в блоке</a>
        </div>
        <div class="info">
            Организация: <b>Неизвестно</b><br />
        </div>
    </div>
</div>
"""

# Shaped like a per-issuer organization page's message-history table.
ORG_PAGE_FIXTURE_HTML = """
<table>
<tr>
    <td>24.07.2026</td>
    <td>27.07.2026 11:31:27</td>
    <td><a href="/messages/4570187/" alt="Совершение сделки">Совершение подконтрольной эмитенту организацией существенной сделки</a></td>
</tr>
<tr>
    <td>27.07.2026</td>
    <td>27.07.2026 11:14:36</td>
    <td><a href="/messages/4570170/" alt="Доходы">Выплаченные доходы по эмиссионным ценным бумагам эмитента</a></td>
</tr>
</table>
"""

# Shaped like the /search/ page (carries the session-scoped sessid the org
# search GET request needs) and the /search/index.php result page (carries
# the resolved organization/personal-pages link).
SEARCH_PAGE_FIXTURE_HTML = """
<form name="organization_search_form" action="/search/index.php">
<input type="hidden" name="sessid" id="sessid" value="deadbeefcafefeed00000000000beef" />
</form>
"""
SEARCH_RESULT_FIXTURE_HTML = """
<table>
<tr><td>6685151087</td><td>1186658052470</td><td>NA</td>
    <td><a href="/organization/personal-pages/4280069/">ООО "Брусника. Строительство и девелопмент"</a></td></tr>
</table>
"""


def test_parser_extracts_messages():
    filings = ed.parse_homepage_feed(HOMEPAGE_FIXTURE_HTML)
    assert len(filings) == 2                    # third item (no ИНН) skipped
    incs = {f.issuer_id for f in filings}
    assert incs == {"6685151087", "7714699320"}
    titles = [f.title for f in filings]
    assert any("подконтрольной" in t for t in titles)
    assert any("просрочке" in t for t in titles)
    # dates parsed to ISO (from the feed's own "date" column, not "Дата события")
    assert all(f.published == "2026-07-27" for f in filings)
    # urls absolutised
    assert filings[0].url == "https://e-disclosure.azipi.ru/messages/4570187/"


def test_parser_empty_on_unrecognised_layout():
    # a page with no message items -> [] (caller can alert), never a crash
    assert ed.parse_homepage_feed("<html><body>gate</body></html>") == []


def test_org_page_parser_extracts_history():
    filings = ed.parse_org_page_feed(ORG_PAGE_FIXTURE_HTML, "6685151087")
    assert len(filings) == 2
    assert all(f.issuer_id == "6685151087" for f in filings)
    assert filings[1].url == "https://e-disclosure.azipi.ru/messages/4570170/"
    assert filings[1].published == "2026-07-27"


def test_search_org_by_inn_resolves_org_url():
    class FakeBackend:
        def __init__(self):
            self.calls = []
        def get(self, url):
            self.calls.append(url)
            if url.endswith("/search/"):
                return SEARCH_PAGE_FIXTURE_HTML
            return SEARCH_RESULT_FIXTURE_HTML
    backend = FakeBackend()
    url = ed.search_org_by_inn("6685151087", backend)
    assert url == "https://e-disclosure.azipi.ru/organization/personal-pages/4280069/"
    # sessid from the search page was carried into the GET query
    assert "deadbeefcafefeed00000000000beef" in backend.calls[1]


def test_search_org_by_inn_none_when_not_found():
    class FakeBackend:
        def get(self, url):
            if url.endswith("/search/"):
                return SEARCH_PAGE_FIXTURE_HTML
            return "<html>no results</html>"
    assert ed.search_org_by_inn("0000000000", FakeBackend()) is None


def test_body_hash_stable_and_unique():
    f1 = ed.Filing.make("I1", "I1", "2026-07-24", "Купонная выплата", "u1")
    f2 = ed.Filing.make("I1", "I1", "2026-07-24", "Купонная выплата", "u1")
    f3 = ed.Filing.make("I1", "I1", "2026-07-24", "Другое сообщение", "u2")
    assert f1.body_hash == f2.body_hash          # same content -> same id
    assert f1.body_hash != f3.body_hash


def test_dedupe_gate():
    filings = ed.parse_homepage_feed(HOMEPAGE_FIXTURE_HTML)
    seen = {filings[0].body_hash}                 # pretend we've seen the first
    fresh = ed.new_filings_since(filings, seen)
    assert len(fresh) == 1
    assert fresh[0].body_hash == filings[1].body_hash


def test_http_backend_raises_on_non_200():
    class FakeResp:
        status_code = 403
        text = "Forbidden"
    class FakeRequests:
        def get(self, *a, **k):
            return FakeResp()
    b = ed.HttpBackend()
    b._requests = FakeRequests()
    try:
        b.get(f"{ed.BASE}/")
        assert False, "should have raised on non-200"
    except RuntimeError as e:
        assert "403" in str(e)


def test_message_body_stripped():
    class FakeBackend:
        def get(self, url):
            return "<html><script>x()</script><body><b>Купон</b> выплачен 45 руб.</body></html>"
    text = ed.fetch_message_body("http://x/y", FakeBackend(), snapshot=False)
    assert "Купон выплачен 45 руб." in text
    assert "<b>" not in text and "script" not in text


def test_filing_store_and_dedupe_memory(tmp_path):
    con = db.connect(tmp_path / "s3.sqlite")
    db.init_db(con)
    db.init_filing(con)
    db.upsert_issuer(con, "I1", "Эмитент", None)
    f = ed.Filing.make("I1", "I1", "2026-07-24", "Просрочка обязательства", "u1")
    db.insert_filing(con, body_hash=f.body_hash, issuer_id="I1", edisclosure_id="I1",
                     published=f.published, title=f.title, url=f.url,
                     fetched_on="2026-07-25")
    con.commit()
    # seen set now contains it
    assert f.body_hash in db.seen_filing_hashes(con, "I1")
    # re-insert is ignored (INSERT OR IGNORE), no duplicate
    db.insert_filing(con, body_hash=f.body_hash, issuer_id="I1", edisclosure_id="I1",
                     published=f.published, title=f.title, url=f.url,
                     fetched_on="2026-07-26")
    con.commit()
    n = con.execute("SELECT COUNT(*) c FROM filing").fetchone()["c"]
    assert n == 1
    # unclassified queue shows it, then mark_classified removes it
    assert len(db.unclassified_filings(con)) == 1
    db.mark_classified(con, f.body_hash)
    con.commit()
    assert len(db.unclassified_filings(con)) == 0
    con.close()


def test_event_insert_and_query(tmp_path):
    con = db.connect(tmp_path / "e.sqlite")
    db.init_db(con)
    db.upsert_issuer(con, "I1", "Эмитент", None)
    eid = db.insert_event(con, issuer_id="I1", occurred="2026-07-24",
                          category="reporting_delay", severity="high",
                          summary="Задержка годовой отчётности",
                          source_url="u1")
    con.commit()
    assert eid == 1
    rows = db.events_since(con, "I1", "2026-07-01")
    assert len(rows) == 1 and rows[0]["category"] == "reporting_delay"
    con.close()


def test_api_hook_is_off_by_design():
    from hybond.skills import classify_api
    # model policy resolves without a call
    assert classify_api.choose_model(issuer_flagged=True) == "claude-opus-4-8"
    assert "haiku" in classify_api.choose_model(issuer_flagged=False)
    # but actually classifying raises NotImplementedError (automation off)
    try:
        classify_api.classify_filing("text", {}, False)
        assert False, "should be a stub"
    except NotImplementedError:
        pass


# --- smart-lab.ru: secondary source, supplements AZIPI ----------------------

# Shaped like the real smart-lab.ru/bonds/extra/ feed: a "today" item (HH:MM,
# no target-issuer match — should be skipped), a title-match item ("Самолет"
# in the title, dated DD/MM), and an author-match item (the issuer's own IR
# account posting, matched via the /profile/ link text rather than the title).
SMARTLAB_FIXTURE_HTML = """
<ul class="temp_headers temp_headers--have-numbers">
    <li class="bluid_111111">
        <b>&nbsp;<u></u></b>
            14:16
        <i><a href="/profile/uniservice/">Юнисервис Капитал</a></i>
        <a href="/blog/1111111.php" title="Обзор рынка ВДО">Обзор рынка ВДО за неделю</a>
    </li>
    <li class="bluid_222222">
        <b>&nbsp;<u>+17</u></b>
            24/07
        <i><a href="/profile/romanranniy/">Роман Ранний</a></i>
        <a href="/blog/1332539.php" title="ставка купона">Самолет установил ставку 25-36-го купонов по облигациям серии БО-П15 на уровне 17% годовых</a>
    </li>
    <li class="bluid_333333">
        <b>1&nbsp;<u></u></b>
            22/07
        <i><a href="/profile/CarMoney/">CarMoney</a></i>
        <a href="/blog/1331492.php" title="Уважаемые инвесторы">🔔 Уважаемые инвесторы!</a>
    </li>
</ul>
"""


def test_smartlab_parser_matches_title_and_author():
    from datetime import date
    filings = sl.parse_feed(SMARTLAB_FIXTURE_HTML, today=date(2026, 7, 27))
    assert len(filings) == 2                    # first item (no match) skipped
    by_issuer = {f.issuer_id: f for f in filings}
    assert set(by_issuer) == {"9731004688", "7730634468"}  # Самолёт, CarMoney
    # title match: date came from the feed's DD/MM (no year) -> current year
    assert by_issuer["9731004688"].published == "2026-07-24"
    assert "купон" in by_issuer["9731004688"].title
    # author-only match: title itself has no issuer name in it
    assert by_issuer["7730634468"].title == "🔔 Уважаемые инвесторы!"
    # both tagged with the secondary-source marker
    assert all(f.edisclosure_id == sl.SOURCE_MARKER for f in filings)


def test_smartlab_today_time_uses_today_date():
    from datetime import date
    html = """
    <li class="bluid_444444">
        <b>&nbsp;<u></u></b>
            09:00
        <i><a href="/profile/CarMoney/">CarMoney</a></i>
        <a href="/blog/9999999.php" title="x">Важное объявление</a>
    </li>
    """
    filings = sl.parse_feed(html, today=date(2026, 7, 27))
    assert len(filings) == 1
    assert filings[0].published == "2026-07-27"


def test_smartlab_parser_empty_on_no_matches():
    html = """
    <li class="bluid_555555">
        <b>&nbsp;<u></u></b>
            14:16
        <i><a href="/profile/someone/">Someone</a></i>
        <a href="/blog/5555555.php" title="x">Пост не про наши компании</a>
    </li>
    """
    assert sl.parse_feed(html) == []


def test_smartlab_interfax_issuers_inn_corrections():
    # Regression guard, 2026-07-30: 8 of 11 INTERFAX_ISSUERS entries carried
    # an INN that didn't match watchlist.json's current issuer_id for the
    # same company -- a name match would "succeed" in parse_feed but the
    # resulting filing.issuer_id would reference a row that doesn't exist in
    # the `issuer` table, so it would never surface in that issuer's real
    # event feed. Pins the corrected values so a future edit can't silently
    # regress one of them back to a stale/wrong INN.
    assert sl.INTERFAX_ISSUERS["Сегежа"] == "9703024202"
    assert sl.INTERFAX_ISSUERS["АФК Система"] == "7703104630"
    assert sl.INTERFAX_ISSUERS["Монополия"] == "7810766685"
    assert sl.INTERFAX_ISSUERS["CarMoney"] == "7730634468"
    assert sl.INTERFAX_ISSUERS["КарМани"] == "7730634468"
    assert sl.INTERFAX_ISSUERS["Интерлизинг"] == "7802131219"
    assert sl.INTERFAX_ISSUERS["Балтийский лизинг"] == "7826705374"
    assert sl.INTERFAX_ISSUERS["Эталон"] == "7705619586"
    assert sl.INTERFAX_ISSUERS["Гарант-Инвест"] == "7726637843"
    # ГлоракС and Самолёт were already correct -- confirm they're untouched.
    assert sl.INTERFAX_ISSUERS["ГлоракС"] == "9729387137"
    assert sl.INTERFAX_ISSUERS["Самолёт"] == "9731004688"
    # the old, now-deleted МФК Займер issuer had its own (also wrong) entry;
    # it's gone from watchlist.json entirely, so the key should be too.
    assert "Займер" not in sl.INTERFAX_ISSUERS


def test_smartlab_segezha_post_resolves_to_correct_issuer():
    from datetime import date
    html = """
    <li class="bluid_777777">
        <b>&nbsp;<u>+5</u></b>
            24/07
        <i><a href="/profile/someauthor/">Аналитик</a></i>
        <a href="/blog/9999999.php" title="обзор">Сегежа объявила о новых условиях по облигациям серии 3P6R</a>
    </li>
    """
    filings = sl.parse_feed(html, today=date(2026, 7, 30))
    assert len(filings) == 1
    assert filings[0].issuer_id == "9703024202"  # Сегежа Групп's real issuer_id


def test_smartlab_filings_flow_through_same_dedupe_and_store(tmp_path):
    from datetime import date
    filings = sl.parse_feed(SMARTLAB_FIXTURE_HTML, today=date(2026, 7, 27))
    con = db.connect(tmp_path / "sl.sqlite")
    db.init_db(con)
    db.init_filing(con)
    db.upsert_issuer(con, "9731004688", "ГК Самолёт", None)
    f = [x for x in filings if x.issuer_id == "9731004688"][0]
    db.insert_filing(con, body_hash=f.body_hash, issuer_id=f.issuer_id,
                     edisclosure_id=f.edisclosure_id, published=f.published,
                     title=f.title, url=f.url, fetched_on="2026-07-27")
    con.commit()
    row = con.execute("SELECT edisclosure_id FROM filing WHERE body_hash=?",
                      (f.body_hash,)).fetchone()
    assert row["edisclosure_id"] == "smartlab"  # the secondary-source tag
    con.close()
