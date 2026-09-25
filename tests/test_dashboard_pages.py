"""Купон Лаб redesign stage 3 (2026-09-16): the Портфель and Данные pages,
and the retirement of the Watchlist route / dashboard-side watchlist_tier.

Two kinds of test live here:

* Source-shape tests over dashboard/template.html and dashboard/src/. The
  dashboard is vanilla JS with no build step and no JS test runner, so these
  assert the wiring and the invariants that were actually got wrong during
  the rebuild -- they are regression guards, not a substitute for opening
  the page.
* Export tests for the payload changes those pages needed, including the
  watchlist_tier removal.

The boundary this file defends: `issuer.watchlist_tier` stays in the
pipeline (it is how scrape/financials/universe/portfolio scope their issuer
set -- see src/hybond/pipeline.py and store/db.py), and is gone from
everything the dashboard is handed. See AGENTS.md.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hybond import health, moex_defaults, portfolio  # noqa: E402
from hybond.store import db  # noqa: E402
from build_dashboard import inline_includes  # noqa: E402
from export_dashboard_data import (  # noqa: E402
    export_all_active_portfolios, passive_issuers_export, pipeline_health_export,
)

DASHBOARD = ROOT / "dashboard"
TEMPLATE = DASHBOARD / "template.html"
SRC = DASHBOARD / "src"
ISSUER = "7701234567"


@pytest.fixture(scope="module")
def template_text():
    """The new dashboard with every INCLUDE inlined -- i.e. everything that
    actually ships, minus the embedded data blob."""
    return inline_includes(TEMPLATE.read_text(encoding="utf-8"), TEMPLATE.parent)


# ---- page wiring ----------------------------------------------------------

def test_both_stage3_pages_are_source_files():
    assert (SRC / "pages" / "portfolio.js").is_file()
    assert (SRC / "pages" / "data.js").is_file()


def test_pending_placeholder_is_gone():
    # pending.js rendered "Ещё не собрано · этап 3 из 3" for these two pages.
    assert not (SRC / "pages" / "pending.js").exists()


def test_template_includes_both_pages():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "__INCLUDE:src/pages/portfolio.js__" in text
    assert "__INCLUDE:src/pages/data.js__" in text
    assert "pending.js" not in text


def test_every_route_registers_a_page(template_text):
    # app.js routes on PAGE_IDS and calls PAGES[id].render(); a route with no
    # PAGES entry is a TypeError on navigation, not a blank page.
    assert "const PAGE_IDS = ['overview', 'terminal', 'rates', 'portfolio', 'data'];" in template_text
    for page in ("overview", "terminal", "rates", "portfolio", "data"):
        assert f"PAGES.{page} = " in template_text
        assert f'id="page-{page}"' in template_text


def test_pages_are_still_separate_source_files_not_one_template():
    # The modular layout is the constraint the redesign is built under.
    page_files = sorted(p.name for p in (SRC / "pages").glob("*.js"))
    assert page_files == ["bond-detail.js", "data.js", "overview-market-band.js",
                          "overview-rating-band.js", "overview.js", "portfolio.js",
                          "rates.js", "terminal-map.js", "terminal.js"]
    # template.html stays a shell: markup + includes, no page logic
    assert len(TEMPLATE.read_text(encoding="utf-8").splitlines()) < 150


def test_market_band_distinguishes_current_and_week_ago_spreads():
    text = (SRC / "pages" / "overview-market-band.js").read_text(encoding="utf-8")
    assert "DATA.universe_now" in text and "DATA.universe_week_ago" in text
    assert "медианный G-спред".lower() in text.lower()
    assert "неделю назад" in text


def test_d3_is_still_an_external_script_with_a_fallback(template_text):
    # Stage 2's constraint: D3 comes from the CDN and the page degrades when
    # it cannot load. Inlining it, or dropping the fallback, breaks both.
    assert '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/' in TEMPLATE.read_text(encoding="utf-8")
    assert "typeof d3" in template_text


# ---- Портфель -------------------------------------------------------------

def test_portfolio_consumes_the_basket_the_screener_persists(template_text):
    # The stage-2 handoff: one localStorage key, one BASKET object.
    assert "'kl.basket'" in template_text
    assert "BASKET.ids()" in (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    assert "if (PF.source === 'basket') return pfAllPositions();" in (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")


def test_build_portfolio_button_selects_the_basket_explicitly():
    # Regression: "Собрать портфель" used to be a bare goPage('portfolio'),
    # which only landed on the basket when Портфель had never been opened --
    # a user who had already looked at a preset got that preset back.
    terminal = (SRC / "pages" / "terminal.js").read_text(encoding="utf-8")
    assert "pfShowBasket(); goPage('portfolio');" in terminal
    pf = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    assert "function pfShowBasket()" in pf
    # ...and first render must not clobber an explicitly requested source
    assert "if (PF.source == null) PF.source = pfDefaultSource();" in pf


def test_portfolio_reads_only_weight_off_the_stored_allocation():
    """The units trap: DATA.portfolios stores aey_base/avg_aey/expected_loss/
    net_return as FRACTIONS while universe_now (and src/bonds.js) carries the
    same quantities in PERCENT. The page resolves every rate through
    BOND_BY_SECID and touches exactly one stored field, `weight`. Rendering a
    stored rate directly would print 0.2% where 21.8% belongs."""
    pf = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in pf.splitlines() if not ln.strip().startswith("//"))
    for fraction_field in ("aey_base", "avg_aey", "expected_loss", "net_return", "max_duration"):
        assert fraction_field not in code, (
            f"{fraction_field} is a fraction in DATA.portfolios; read the percent "
            "value off the bond model instead")
    assert "BOND_BY_SECID.get(b.isin)" in code


def test_portfolio_checks_use_exported_preset_constraints():
    # Thresholds come from the rules build_preset() actually enforced, not
    # from the design reference's mock 18% / 200 млн ₽ numbers.
    pf = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    assert "cur.constraints" in pf
    assert "max_per_issuer" in pf and "sector_cap" in pf and "min_rating" in pf


def test_portfolio_aey_tile_is_gated_on_the_pipeline_yield_zone():
    # A portfolio of distressed bonds averages a three-digit AEY; showing it
    # as the green good-news tile says the opposite of the model's verdict.
    pf = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    assert "yieldZone === 'negative_ev'" in pf
    assert "green: !red" in pf


def test_yield_guard_is_add_flow_only_and_uses_weekly_spread_join():
    bonds = (SRC / "bonds.js").read_text(encoding="utf-8")
    terminal = (SRC / "pages" / "terminal.js").read_text(encoding="utf-8")
    portfolio_page = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    # The map remains an unfiltered view; the guard is shared by basket/add
    # surfaces and gets its stability signal from the exported prior snapshot.
    assert "DATA.universe_week_ago" in bonds
    assert "const YIELD_GUARD" in bonds
    assert "BASKET.toggle(pick.dataset.pick)" in terminal
    assert "Доходность ≤ порога" in terminal
    assert "data-guard-threshold" in terminal
    assert "YIELD_GUARD.alternatives" in terminal
    assert "YIELD_GUARD.alternatives" in portfolio_page
    assert "PF_REPLACED" in portfolio_page


def test_yield_guard_keeps_map_unfiltered_by_default():
    terminal = (SRC / "pages" / "terminal.js").read_text(encoding="utf-8")
    # The opt-in chip is a predicate in TERM_CHIPS; no threshold predicate is
    # applied to termVisible() unless the user selects that chip.
    assert "filter: 'Все'" in terminal
    assert "['Все', () => true]" in terminal
    assert "['Доходность ≤ порога'" in terminal


def test_terminal_keeps_only_split_layout():
    terminal = (SRC / "pages" / "terminal.js").read_text(encoding="utf-8")
    terminal_map = (SRC / "pages" / "terminal-map.js").read_text(encoding="utf-8")
    css = (SRC / "app.css").read_text(encoding="utf-8")
    assert "Раскладка A · сплит" not in terminal  # layout switcher is gone
    assert "Раскладка B · полоса" not in terminal
    assert "data-layout-btn" not in terminal
    assert 'data-layout="B"' not in css
    assert "S.layout" not in terminal_map


def test_screener_removes_deprecated_sector_chips():
    terminal = (SRC / "pages" / "terminal.js").read_text(encoding="utf-8")
    for label in ("Лизинг", "Недвижимость", "Диверсифицированный холдинг"):
        assert f"['{label}'" not in terminal


def test_map_brush_proposes_candidates_only_after_confirmation():
    terminal = (SRC / "pages" / "terminal.js").read_text(encoding="utf-8")
    open_flow = terminal.split("function termOpenProposal", 1)[1].split("function termRenderProposal", 1)[0]
    assert 'id="termProposeBtn" disabled' in terminal
    assert "Выделите область → предложение" in terminal
    assert "termProposalRows" in open_flow and "new Set(rows.map(b => b.secid))" in open_flow
    assert "BASKET.toggle" not in open_flow
    assert "slice(0, 30)" in terminal
    assert "!target.held.has(b.secid)" in terminal
    assert "Подтвердить и открыть черновик" in terminal
    assert "proposalClose.matches('button')" in terminal
    assert "Существующие позиции не заменяются" in terminal
    assert "if (ids.length) { pfShowBasket(); goPage('portfolio'); }" in terminal


def test_portfolio_scenario_chart_renders_percentage_ticks():
    pf = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    assert "const ticks = [0, 1, 2, 3, 4]" in pf
    assert 'stroke-opacity=".10"' in pf
    assert 'fill-opacity=".58">${fmtNum(v, 1)}%</text>' in pf


# ---- Данные ---------------------------------------------------------------

def test_data_page_covers_the_whole_event_taxonomy():
    """An unmapped category renders its raw English key into a Russian UI."""
    from hybond.classify_ollama import VALID_CATEGORIES
    text = (SRC / "pages" / "data.js").read_text(encoding="utf-8")
    body = text.split("const D_CATEGORY = {", 1)[1].split("};", 1)[0]
    mapped = {ln.split(":", 1)[0].strip() for ln in body.splitlines() if ":" in ln}
    assert VALID_CATEGORIES <= mapped, VALID_CATEGORIES - mapped


def test_data_page_default_filter_is_a_real_filter_label():
    """D.filter is compared against the chip labels directly, so a default
    that is not one of them leaves every chip unhighlighted."""
    text = (SRC / "pages" / "data.js").read_text(encoding="utf-8")
    default = text.split("const D = { filter: '", 1)[1].split("'", 1)[0]
    labels = text.split("const D_FILTERS = [", 1)[1].split("];", 1)[0]
    assert f"['{default}'," in labels


def test_data_page_invents_no_confidence_score():
    # The reference shows "уверенность 0.9" per event; classified events
    # carry a category and a severity, never a confidence.
    text = (SRC / "pages" / "data.js").read_text(encoding="utf-8")
    assert "уверенность" not in text


# ---- Watchlist route / watchlist_tier retirement --------------------------

def _new_dashboard_sources():
    return [TEMPLATE] + sorted(SRC.rglob("*.js")) + sorted(SRC.rglob("*.css"))


def test_watchlist_tier_is_absent_from_the_dashboard_source():
    offenders = [p.relative_to(ROOT) for p in _new_dashboard_sources()
                 if "watchlist_tier" in p.read_text(encoding="utf-8")]
    assert offenders == []


def _strip_comments(text):
    """Drop // line comments -- terminal.js explains the retirement in prose
    and must stay free to name the thing it retired."""
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


def test_watchlist_route_is_absent_from_the_dashboard_source():
    """Retired into the screener: "Наблюдение" is a per-bond flag and a chip,
    not a page. dashboard/legacy/ is deliberately excluded -- it is kept
    buildable as the fallback for the live cutover and still has the old
    route (see scripts/build_dashboard.py)."""
    offenders = [p.relative_to(ROOT) for p in _new_dashboard_sources()
                 if "Вотчлист" in _strip_comments(p.read_text(encoding="utf-8"))]
    assert offenders == []
    template = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-page="watchlist"' not in template
    # the nav is the route list; five buttons (Ставка и ОФЗ is the fifth,
    # plan rev 2, 2026-09-17), none of them a watchlist
    assert template.count("topnav-btn") == 5


def test_ticker_watching_survives_as_a_per_bond_flag(template_text):
    # The replacement for the route: in_watchlist -> bond.watch -> a chip.
    assert "watch: b.in_watchlist === true" in template_text
    assert "['Наблюдение', b => b.watch]" in template_text


def test_legacy_dashboard_is_still_buildable():
    """Kept on purpose until the live cutover has been stable; it is the way
    back if the cutover goes wrong."""
    import build_dashboard
    legacy = DASHBOARD / "legacy" / "template.html"
    assert legacy.is_file()
    assert any(src == legacy for src, _ in build_dashboard.BUILDS)


def test_passive_issuers_export_carries_no_tier(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_rating(con)
    db.init_universe(con)
    db.add_issuer_at_tier(con, ISSUER, "Пассивный эмитент", watchlist_tier=2)
    con.commit()
    rows = passive_issuers_export(con, db.list_issuers_by_tier(con, 2), {}, None)
    con.close()
    assert rows and "watchlist_tier" not in rows[0]


def test_moex_default_matches_carry_no_tier(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.add_issuer_at_tier(con, ISSUER, "Эмитент с дефолтом", watchlist_tier=1)
    db.init_moex_default(con)
    con.execute(
        "INSERT INTO moex_default(inn, issuer_name, isin, obligation_type, status, planned_date) "
        "VALUES (?,?,?,?,?,?)",
        (ISSUER, "Эмитент с дефолтом", "RU000A100001", "Купон", "Технический дефолт", "2026-07-03"))
    con.commit()
    rows = moex_defaults.get_watchlist_defaults(con)
    con.close()
    assert rows and "watchlist_tier" not in rows[0]
    # the join still spans both tiers -- only the field is gone
    assert rows[0]["issuer_name"] == "Эмитент с дефолтом"


def test_pipeline_still_scopes_issuers_by_watchlist_tier(tmp_path):
    """The other half of the boundary: removing the column would break the
    data path, so the pipeline's own scoping must keep working."""
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.add_issuer_at_tier(con, "1", "Tier1 Co", watchlist_tier=1)
    db.add_issuer_at_tier(con, "2", "Tier2 Co", watchlist_tier=2)
    con.commit()
    assert [r["issuer_id"] for r in db.list_issuers_by_tier(con, 1)] == ["1"]
    assert sorted(r["issuer_id"] for r in db.list_issuers_by_tier(con, (1, 2))) == ["1", "2"]
    con.close()


# ---- export changes the pages needed --------------------------------------

def test_active_portfolios_export_carries_its_preset_constraints(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_portfolio(con)
    db.init_issuer_bond(con)
    db.init_universe(con)
    db.upsert_issuer(con, ISSUER, "Эмитент", sector="leasing")
    db.add_issuer_bond(con, ISSUER, "RU000A100001", source="manual")
    pid = db.create_portfolio(con, "Консервативный", "conservative", status="active")
    db.add_portfolio_bond(con, pid, "RU000A100001", weight=1.0)
    con.commit()
    rows = export_all_active_portfolios(con)
    con.close()
    assert rows, "a portfolio with a bond must export"
    assert rows[0]["preset_type"] == "conservative"
    assert rows[0]["constraints"] == portfolio.PRESETS["conservative"]
    # the constraints the Портфель page checks against must all be present
    for key in ("max_per_issuer", "sector_cap", "min_rating"):
        assert key in rows[0]["constraints"]


def test_unknown_preset_type_exports_no_constraints():
    # A hand-built portfolio has no preset rules; the page must get None
    # rather than another preset's thresholds.
    assert portfolio.PRESETS.get("hand-rolled") is None


def test_health_export_keeps_errors_when_warnings_are_newer(tmp_path):
    """Regression: get_unresolved() orders by recorded_at and caps at 20, so
    a burst of warnings buried every older error and the Данные page listed
    warnings under a header that said "4 ошибки"."""
    con = db.connect(tmp_path / "t.sqlite")
    db.init_db(con)
    db.init_universe(con)          # pipeline_health_export checks snapshot staleness
    db.init_pipeline_health(con)
    health.log_health(con, "pipeline", "ingest", "error", "MOEX_COUNT_DROP", "25% drop")
    for i in range(30):
        health.log_health(con, "pipeline", "compute", "warn", "GSPREAD_NEGATIVE", f"bond {i}")
    con.commit()
    result = pipeline_health_export(con, latest_spread=None)
    con.close()
    codes = {row["code"] for row in result["unresolved"]}
    assert "MOEX_COUNT_DROP" in codes, "the error was buried by newer warnings"
    assert "GSPREAD_NEGATIVE" in codes
    # newest first, and no duplicate ids from the two queries
    ids = [row["id"] for row in result["unresolved"]]
    assert len(ids) == len(set(ids))
    recorded = [row["recorded_at"] for row in result["unresolved"]]
    assert recorded == sorted(recorded, reverse=True)


def test_every_exported_sector_has_a_russian_label():
    """A sector with no label printed its raw English key into the screener
    chip and the Портфель concentration check."""
    from export_dashboard_data import SECTOR_COLORS, SECTOR_LABELS_RU
    unlabelled = {s for s in SECTOR_COLORS if s not in SECTOR_LABELS_RU} - {"other", "mfo"}
    assert unlabelled == set(), unlabelled


# ---- the built artefact ---------------------------------------------------

@pytest.mark.skipif(not (DASHBOARD / "index.html").is_file(), reason="dashboard not built")
def test_built_dashboard_has_no_watchlist_tier_anywhere():
    """Belt and braces over the shipped file: source AND embedded payload."""
    text = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    assert "watchlist_tier" not in text


@pytest.mark.skipif(not (DASHBOARD / "index.html").is_file(), reason="dashboard not built")
def test_built_dashboard_embeds_the_pages_it_claims_to():
    text = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    assert "__INCLUDE" not in text          # every marker was substituted
    assert "PAGES.portfolio = {" in text
    assert "PAGES.data = {" in text
    assert "PAGES.rates = {" in text
    assert "Ещё не собрано" not in text     # no placeholder page left


# ---- Ставка и ОФЗ (plan rev 2, approved 2026-09-17) -------------------------

def _code_lines(path):
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_rates_page_is_wired_between_the_screener_and_the_portfolio():
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "__INCLUDE:src/pages/rates.js__" in template
    nav = [line.split('data-page="')[1].split('"')[0] for line in template.splitlines()
           if 'class="topnav-btn"' in line]
    assert nav == ["overview", "terminal", "rates", "portfolio", "data"]
    # rates.js uses the drawer helpers from bond-detail.js, so it must load after it
    assert template.index("src/pages/bond-detail.js") < template.index("src/pages/rates.js")


def test_key_rate_readout_links_to_the_rates_page():
    """The «Ключевая ЦБ» readout is a page link, the way the health chip is."""
    import re
    template = TEMPLATE.read_text(encoding="utf-8")
    tag = re.search(r'<button[^>]*id="cbrChip"[^>]*>', template).group(0)
    assert 'data-page="rates"' in tag
    health = re.search(r'<button[^>]*id="healthChip"[^>]*>', template).group(0)
    assert 'data-page="data"' in health


def test_rates_page_introduces_no_new_css_component():
    """Visually identical to the other pages (captain's D1): every class the
    page renders already exists in the design system or app.css."""
    import re
    css = "".join((SRC / name).read_text(encoding="utf-8") for name in ("design-system.css", "base.css", "app.css"))
    js = (SRC / "pages" / "rates.js").read_text(encoding="utf-8")
    classes = set()
    for attr in re.findall(r'class="([^"$]*)"', js):
        classes.update(c for c in attr.split() if c)
    missing = sorted(c for c in classes if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css))
    assert classes and missing == [], missing


def test_dashboard_fetches_nothing_at_runtime():
    # D3 of the plan: the old «Обновить котировки (MOEX, сейчас)» browser
    # fetch is not carried over; everything comes from the embedded export.
    for path in sorted(SRC.rglob("*.js")):
        code = _code_lines(path)
        assert "fetch(" not in code and "XMLHttpRequest" not in code, path.name


def test_no_scenario_delta_is_extrapolated_as_a_slope():
    """Regression, plan finding S1: the rate tracker (originally on Портфель,
    moved to «Ставка и ОФЗ» 2026-09-18) drew rate_scenarios.base_annual_bp (a
    level difference) as a slope per year and printed «база ЦБ 13,0%» for
    18.12.2026, a number the CBR never published."""
    for path in sorted(SRC.rglob("*.js")):
        assert "_annual_bp" not in _code_lines(path), path.name
    rt = (SRC / "pages" / "rates.js").read_text(encoding="utf-8")
    assert "DATA.expectations" in rt and "f.rest" in rt


def test_rates_page_taper_matches_the_model():
    """The OFZ curve's dashed shift must use the very taper the returns were
    computed with, not a copy that can drift."""
    from hybond.compute import ofz_carry
    js = (SRC / "pages" / "rates.js").read_text(encoding="utf-8")
    line = next(ln for ln in js.splitlines() if ln.startswith("const RT_SHIFT"))
    for h, frac in ofz_carry.GRADUATED_SHIFT.items():
        assert f"{h}: {frac:.2f}" in line


def test_rates_page_reads_thresholds_from_the_export():
    js = _code_lines(SRC / "pages" / "rates.js")
    assert "RT_OFZ.max_curve_gap_bp" in js                   # ofz_pipeline.MAX_CURVE_GAP_BP
    assert "match_max_diff_years" in js                      # the premium's duration-match limit
    assert "pd_rating" not in js and "aey_base\b" not in js  # premium comes computed, not re-derived


# ---- captain's review requests, 2026-09-17 ----------------------------------

def test_portfolio_positions_can_be_removed_and_restored():
    """Remove a bond that no longer meets his criteria, from a row or from a
    failed check; every tile, share and check is computed over what is left;
    the removal survives a dashboard rebuild (localStorage) and a rebuilt
    preset (keyed by preset type, not id); and it can be undone."""
    pf = (SRC / "pages" / "portfolio.js").read_text(encoding="utf-8")
    code = _strip_comments(pf)
    assert "'kl.pf.removed'" in code
    assert "`preset:${cur.preset_type || cur.id}`" in code
    # every computation goes through the filtered position list
    assert "return pfAllPositions().filter(p => !removed.includes(p.secid));" in code
    assert "const positions = pfPositions();" in code
    # the controls: per row, per failed check, and the way back
    assert 'data-pf-remove="${escapeHtml(p.secid)}"' in code
    assert 'data-pf-remove="${escapeHtml(c.ids.join(\',\'))}"' in code
    assert 'data-pf-restore="${escapeHtml(p.secid)}"' in code
    assert "PF_REMOVED.restore(pfRemovalKey(), ids)" in code
    # still only one stored field read off the allocation (units trap)
    assert "aey_base" not in code


def test_map_and_screener_can_be_enlarged_with_an_animated_split():
    terminal = _strip_comments((SRC / "pages" / "terminal.js").read_text(encoding="utf-8"))
    assert 'data-focus-btn="map"' in terminal and 'data-focus-btn="screener"' in terminal
    assert "⇆ Вернуть сплит" in terminal                          # the way back to the even split
    assert "TerminalMap.holdResize();" in terminal and "TerminalMap.releaseResize();" in terminal
    css = (SRC / "app.css").read_text(encoding="utf-8")
    assert "transition: grid-template-columns .5s cubic-bezier(.22,1,.36,1)" in css   # the design's own ease
    assert '.term-split[data-focus="map"]' in css
    assert '.term-split[data-focus="screener"]' in css
    assert 'data-layout="B"' not in css
    assert "height: calc(100dvh - 58px)" in css
    assert ".term-map-card .map-root" in css and ".term-screener-card .screener-scroll" in css
    assert "grid-template-rows: minmax(420px, 1fr) minmax(420px, 1fr)" in css
    assert "[data-focus-btn] { display: inline-block; }" in css
    assert "prefers-reduced-motion" in css
    tmap = (SRC / "pages" / "terminal-map.js").read_text(encoding="utf-8")
    assert "if (resizeHeld) return;" in tmap                     # no per-frame redraw during the move
