// Карта и скринер -- the isTerminal page of bond-lab-design-reference.html.
//
// One screener, not two. The old dashboard had a Рынок screener and a
// Вотчлист screener side by side, each with its own filters, columns and
// selection; this page replaces both. "Наблюдение" is now a flag on a bond
// (in_watchlist) and therefore just another chip in this screener's filter
// row -- there is no second copy of the screener behind a second tab.
//
// Layout, copy, spacing and colours follow the reference. What is NOT from
// the reference is anything numeric: its `bonds` array is mock data, and its
// thresholds (±60 б.п. fair band, 500 млн ₽ liquidity floor, AEY > 18%) sit
// outside the real distribution. Real thresholds live in src/bonds.js.
//
// State lives here; the map (src/pages/terminal-map.js) is a view over it
// and calls back through the `ctl` object at the bottom of this file.

const SCREENER_PAGE = 250;          // rows revealed at a time (universe is ~1200 bonds)
const SIMILAR_MIN_SCORE = 6;        // "Балл ≥ 6" chip

const TERM_CHIPS = [
  // [label, predicate]. Counts on the real universe are in the comment so a
  // chip that stops matching anything is easy to spot.
  ['Все', () => true],                                                        // 1191
  ['Наблюдение', b => b.watch],                                               // 312
  ['Недооценённые', b => gapBand(b) === 'above'],                             // 225
  // yield_zone is the pipeline's own verdict on whether a high yield is an
  // opportunity or distress (compute's yield-inflection table), so it is the
  // chip that separates the two -- not an AEY threshold, which cannot.
  ['Зелёная зона', b => b.yieldZone === 'green'],                              // 180
  [`Балл ≥ ${SIMILAR_MIN_SCORE}`, b => b.score != null && b.score >= SIMILAR_MIN_SCORE], // 51
  ['Ликвидные', b => b.vol != null && b.vol >= LIQUID_VOLUME_RUB],            // 83
  ['Лизинг', b => b.sector === 'leasing'],                                    // 47
  ['Недвижимость', b => b.sector === 'real_estate'],                          // 43
  ['Диверсифицированный холдинг', b => b.sector === 'holding_diversified'],   // 40
];

// [label, sort key, alignment]. Reference colDef, with its mock-only
// columns mapped onto real fields.
const TERM_COLS = [
  ['Бумага', 't', 'left'],
  ['Срок', 'm', 'right'],
  ['YTM', 'ytm', 'right'],
  ['AEY', 'aey', 'right'],
  ['G-спред', 'gs', 'right'],
  ['Δ к fair', 'gap', 'right'],
  ['Рейтинг', 'rating', 'center'],
  ['Балл', 'score', 'right'],
  ['Флаг', null, 'right'],
];

const TERM_SORT_KEYS = {
  t: b => b.t, m: b => b.m, ytm: b => b.ytm, aey: b => b.aey,
  gs: b => b.gs, gap: b => b.gap, score: b => b.score,
  rating: b => { const i = (DATA.rating_order || []).indexOf(b.rating); return i < 0 ? null : -i; },
};

const TERM_AXES = [
  ['spread', 'G-спред / дюрация'],
  ['aey', 'AEY / кредитный балл'],
  ['curve', 'Кривая ОФЗ'],
];

const TERM_LAYOUT_NOTE = {
  A: 'A: карта и таблица делят экран пополам. Рамка на карте и строка в таблице всегда рядом — удобно сравнивать глазами, но таблица узкая.',
  B: 'B: карта сжата в полосу-фильтр над таблицей. Выделение задаётся быстро, зато на таблицу уходит вся ширина — читаются все колонки сразу.',
};

const TERM_LEGEND = [
  ['#c67139', 'спред выше справедливого'],
  ['#7a8a5e', 'в пределах модели'],
  ['#8a8478', 'спред ниже справедливого'],
  ['#c0b6a5', 'без рейтинга — справедливый спред не считается'],
];

const S = {
  layout: 'A',
  axis: 'spread',
  filter: 'Все',
  custom: null,     // ad-hoc chip from "Показать похожие на карте": { label, test }
  sort: 'aey',
  dir: -1,
  brush: null,      // unzoomed plot coords, owned by the map
  sel: null,        // secid of the bond whose drawer is open
  shown: SCREENER_PAGE,
  zoomed: false,
  focus: null,      // layout A only: 'map' | 'screener' enlarged, null = even split
};

const TERM_FOCUS_MS = 700;      // fallback if transitionend never fires (reduced motion, stacked layout)

let el = null;                 // page container
let visibleCache = null;       // invalidated by anything that changes the row set

function termFilterFn() {
  if (S.custom && S.filter === S.custom.label) return S.custom.test;
  const chip = TERM_CHIPS.find(c => c[0] === S.filter) || TERM_CHIPS[0];
  return chip[1];
}

// "Показать похожие на карте": the nearest real peer group the data can
// define -- sector, else rating class, else the same issuer. Never a made-up
// similarity score.
function termSimilar(b) {
  if (b.sector) return { label: b.sectorLabel || b.sector, test: x => x.sector === b.sector };
  if (b.ratingClass) return { label: `Рейтинг ${b.ratingClass}`, test: x => x.ratingClass === b.ratingClass };
  return { label: b.n, test: x => x.inn === b.inn };
}

// Bonds passing the chip AND the map's frame. The map is the other half of
// the filter, which is why it is one function and not two.
function termVisible() {
  if (visibleCache) return visibleCache;
  const pass = termFilterFn();
  visibleCache = BONDS.filter(b => pass(b) && TerminalMap.inBrush(b));
  const key = TERM_SORT_KEYS[S.sort] || TERM_SORT_KEYS.aey;
  // nulls always sink, whichever way the column is sorted -- "no value" is
  // not "worst value", it just cannot be ranked.
  visibleCache.sort((a, b) => {
    const x = key(a), y = key(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === 'string') return x.localeCompare(y, 'ru') * S.dir;
    return (x > y ? 1 : x < y ? -1 : 0) * S.dir;
  });
  return visibleCache;
}

function termInvalidate() { visibleCache = null; }

// ── rendering ───────────────────────────────────────────────────────────
function termShellHtml() {
  const seg = on => `class="seg-btn${on ? ' is-on' : ''}"`;
  return `<div class="page-terminal">

    <div class="term-toolbar">
      <div class="seg-group">
        <button type="button" data-layout-btn="A" ${seg(S.layout === 'A')}>Раскладка A · сплит</button>
        <button type="button" data-layout-btn="B" ${seg(S.layout === 'B')}>Раскладка B · полоса</button>
      </div>
      <div class="term-layout-note" id="termLayoutNote"></div>
      <div class="term-axis-pills" id="termAxisPills">
        ${TERM_AXES.map(([k, label]) =>
          `<button type="button" class="axis-pill" data-axis="${k}">${escapeHtml(label)}</button>`).join('')}
      </div>
    </div>

    <div class="term-split" id="termSplit">

      <section class="term-card term-map-card">
        <div class="term-card-head">
          <div class="term-card-title" id="termMapTitle"></div>
          <div class="term-card-hint">Протяните рамку по карте — скринер справа отфильтруется на лету. Клик по точке открывает разбор бумаги. Колесо — зум, Shift+перетаскивание — сдвиг.</div>
          <button type="button" class="chip-clear" id="termClearBrush" hidden>Сбросить выделение</button>
          <button type="button" class="chip-clear" id="termResetZoom" hidden>Сбросить зум</button>
          <button type="button" class="chip-clear" data-focus-btn="map"></button>
        </div>
        <div class="map-root" id="termMapRoot"></div>
        <div class="map-legend">
          ${TERM_LEGEND.map(([c, t]) =>
            `<div class="map-legend-item"><span class="map-legend-dot" style="background:${c}"></span><span>${escapeHtml(t)}</span></div>`).join('')}
          <div class="map-legend-item"><span>размер точки — оборот за сессию</span></div>
        </div>
        <div class="map-note" id="termMapNote"></div>
      </section>

      <section class="term-card term-screener-card">
        <div class="term-card-head term-screener-head">
          <div class="term-card-title">Скринер</div>
          <div class="term-card-hint" id="termScreenerCount"></div>
          <button type="button" class="chip-clear" data-focus-btn="screener"></button>
        </div>
        <div class="term-chips" id="termChips"></div>
        <div class="screener-scroll" id="termScroll">
          <table class="screener">
            <thead><tr id="termHead"></tr></thead>
            <tbody id="termBody"></tbody>
          </table>
          <div class="screener-more" id="termMore" hidden></div>
          <div class="screener-empty" id="termEmpty" hidden>Под выделение ничего не попало. Растяните рамку на карте шире или сбросьте выделение.</div>
        </div>
      </section>
    </div>

    <div class="basket-bar" id="termBasket" hidden></div>
  </div>`;
}

function termRenderChips() {
  const chips = TERM_CHIPS.map(c => c[0]);
  const custom = S.custom && !chips.includes(S.custom.label) ? S.custom.label : null;
  document.getElementById('termChips').innerHTML =
    (custom ? `<button type="button" class="chip is-custom${S.filter === custom ? ' is-on' : ''}" data-chip="${escapeHtml(custom)}">${escapeHtml(custom)}</button>` : '')
    + chips.map(c => `<button type="button" class="chip${S.filter === c ? ' is-on' : ''}" data-chip="${escapeHtml(c)}">${escapeHtml(c)}</button>`).join('');
}

function termRenderHead() {
  document.getElementById('termHead').innerHTML = TERM_COLS.map(([label, key, align]) => {
    const on = key && S.sort === key;
    const arrow = on ? (S.dir < 0 ? ' ↓' : ' ↑') : '';
    const flag = label === 'Флаг' ? ' c-flag' : '';
    return `<th class="al-${align}${flag}${key ? ' is-sortable' : ''}${on ? ' is-sorted' : ''}"${key ? ` data-sort="${key}"` : ''}>${escapeHtml(label)}${arrow}</th>`;
  }).join('');
}

function termRowHtml(b) {
  const band = gapBand(b);
  const gapCls = band === 'above' ? 'v-up' : band === 'below' ? 'v-down' : band === 'fair' ? 'v-fair' : '';
  // AEY is coloured by the pipeline's own yield zone, not by a magnitude
  // threshold: a 300% AEY on a negative-EV bond must not read as the best row.
  const aeyCls = b.aey == null ? '' : b.yieldZone === 'green' ? 'v-fair' : b.yieldZone === 'negative_ev' ? 'v-down' : 'v-caution';
  const rc = b.ratingClass;
  const ratCls = rc == null ? 'rat-none' : ['AAA', 'AA', 'A'].includes(rc) ? 'rat-high' : ['BBB', 'BB'].includes(rc) ? 'rat-mid' : 'rat-low';
  const picked = BASKET.has(b.secid);
  const sc = b.score;
  return `<tr data-secid="${escapeHtml(b.secid)}"${S.sel === b.secid ? ' class="is-sel"' : ''}>
    <td class="al-left c-name">
      <div class="c-name-row">
        <button type="button" class="pick${picked ? ' is-on' : ''}" data-pick="${escapeHtml(b.secid)}" aria-label="${picked ? 'Убрать из корзины' : 'Добавить в корзину'}">${picked ? '✓' : '+'}</button>
        <div class="c-name-text">
          <div class="c-ticker">${escapeHtml(b.t)}</div>
          <div class="c-issuer">${escapeHtml(b.n)}</div>
        </div>
      </div>
    </td>
    <td class="al-right dim">${b.m == null ? DASH : fmtNum(b.m, 1) + ' г.'}</td>
    <td class="al-right dim">${fmtPct(b.ytm, 1)}</td>
    <td class="al-right c-aey ${aeyCls}">${fmtPct(b.aey, 1)}</td>
    <td class="al-right dim">${b.gs == null ? DASH : fmtBpSigned(b.gs)}</td>
    <td class="al-right ${gapCls}">${b.gap == null ? DASH : fmtBpSigned(b.gap)}</td>
    <td class="al-center"><span class="rat ${ratCls}">${b.rating ? escapeHtml(b.rating) : 'без рейтинга'}</span></td>
    <td class="al-right">${sc == null ? `<span class="dim">${DASH}</span>` : `<div class="score">
        <div class="score-track"><div class="score-fill${sc >= 7 ? ' is-good' : sc >= 4 ? ' is-mid' : ' is-bad'}" style="width:${sc * 10}%"></div></div>
        <span class="score-num">${fmtNum(sc, 1)}</span>
      </div>`}</td>
    <td class="al-right c-flag">${escapeHtml(b.flags[0] || '')}</td>
  </tr>`;
}

function termRenderRows() {
  const vis = termVisible();
  const rows = vis.slice(0, S.shown);
  document.getElementById('termBody').innerHTML = rows.map(termRowHtml).join('');
  const more = document.getElementById('termMore');
  const rest = vis.length - rows.length;
  more.hidden = rest <= 0;
  if (rest > 0) more.innerHTML = `<button type="button" class="pill-outline" id="termMoreBtn">Показать ещё ${fmtNum(Math.min(rest, SCREENER_PAGE), 0)} · осталось ${fmtNum(rest, 0)}</button>`;
  document.getElementById('termEmpty').hidden = vis.length !== 0;

  const scope = S.brush ? ' · по выделению' : '';
  document.getElementById('termScreenerCount').textContent =
    `${fmtNum(vis.length, 0)} из ${fmtNum(BONDS.length, 0)} ${plural(BONDS.length, ['бумаги', 'бумаг', 'бумаг'])}${scope}`;
}

function termRenderBasket() {
  const bar = document.getElementById('termBasket');
  const ids = BASKET.ids();
  bar.hidden = ids.length === 0;
  if (!ids.length) return;
  const bs = ids.map(id => BOND_BY_SECID.get(id)).filter(Boolean);
  const avg = f => { const v = bs.map(f).filter(x => x != null && isFinite(x)); return v.length ? v.reduce((a, c) => a + c, 0) / v.length : null; };
  const mins = bs.map(b => b.score).filter(x => x != null);
  bar.innerHTML = `
    <div class="basket-main">
      <div class="basket-kicker">Отобрано в корзину</div>
      <div class="basket-names">${escapeHtml(bs.map(b => b.t).join(' · '))}</div>
    </div>
    <div class="basket-stats">
      <div><span>AEY</span> <b>${fmtPct(avg(b => b.aey), 1)}</b></div>
      <div><span>Дюрация</span> <b>${avg(b => b.dur) == null ? DASH : fmtNum(avg(b => b.dur), 1) + ' г.'}</b></div>
      <div><span>Мин. балл</span> <b>${mins.length ? fmtNum(Math.min.apply(null, mins), 1) : DASH}</b></div>
    </div>
    <div class="basket-actions">
      <button type="button" class="basket-clear" id="termBasketClear">Очистить</button>
      <button type="button" class="basket-build" id="termBasketBuild">Собрать портфель</button>
    </div>`;
}

function termRenderChrome() {
  document.getElementById('termLayoutNote').textContent = TERM_LAYOUT_NOTE[S.layout];
  document.querySelectorAll('[data-layout-btn]').forEach(b => b.classList.toggle('is-on', b.dataset.layoutBtn === S.layout));
  document.querySelectorAll('[data-axis]').forEach(b => b.classList.toggle('is-on', b.dataset.axis === S.axis));
  termRenderChips();
  const split = document.getElementById('termSplit');
  split.dataset.layout = S.layout;
  if (S.focus) split.dataset.focus = S.focus; else delete split.dataset.focus;
  // Enlarge controls: the side-by-side layout only. The enlarged card's own
  // button returns to the even split; the other card's button swaps.
  document.querySelectorAll('[data-focus-btn]').forEach(btn => {
    const which = btn.dataset.focusBtn;
    btn.hidden = S.layout !== 'A';
    btn.textContent = S.focus === which ? '⇆ Вернуть сплит' : which === 'map' ? '⤢ Развернуть карту' : '⤢ Развернуть таблицу';
    btn.setAttribute('aria-pressed', S.focus === which ? 'true' : 'false');
  });
  document.getElementById('termMapTitle').textContent = TerminalMap.title(S.axis);
  document.getElementById('termClearBrush').hidden = !S.brush;
  document.getElementById('termResetZoom').hidden = !S.zoomed;

  const off = TerminalMap.offAxisCount();
  const plotted = TerminalMap.plottedCount();
  const parts = [`На карте ${fmtNum(plotted, 0)} из ${fmtNum(BONDS.length, 0)} ${plural(BONDS.length, ['бумаги', 'бумаг', 'бумаг'])}`];
  if (off) parts.push(`${fmtNum(off, 0)} за пределами осей (экстремальная доходность или дюрация длиннее 8 лет) — они остаются в скринере`);
  if (S.axis === 'aey') parts.push('балл есть только у эмитентов под мониторингом, остальные на этой оси не показываются');
  document.getElementById('termMapNote').textContent = parts.join(' · ') + '.';
}

// Everything except the map's own geometry.
function termRefresh({ rows = true, chrome = true } = {}) {
  termInvalidate();
  if (chrome) termRenderChrome();
  if (rows) { termRenderHead(); termRenderRows(); }
  termRenderBasket();
  TerminalMap.refresh();
}

// ── controller handed to the map ────────────────────────────────────────
const ctl = {
  state: S,
  visibleIds() { return termVisible().map(b => b.secid); },
  setBrush(br) {
    S.brush = br;
    S.shown = SCREENER_PAGE;
    termRefresh();
  },
  onBrushDrag() {
    // live filtering while the frame is being dragged
    S.shown = SCREENER_PAGE;
    termInvalidate();
    termRenderRows();
    TerminalMap.refresh();
  },
  onZoom(zoomed) {
    if (zoomed === S.zoomed) return;
    S.zoomed = zoomed;
    document.getElementById('termResetZoom').hidden = !zoomed;
  },
  // the map re-laid itself out (container width changed); the frame was
  // rescaled with it, so only the readouts need refreshing
  onResize() { termRefresh(); },
  onOpen(secid) { termOpenBond(secid); },
  onTogglePick(secid) { BASKET.toggle(secid); },
};

// ── bond drawer ─────────────────────────────────────────────────────────
function termOpenBond(secid) {
  const b = BOND_BY_SECID.get(secid);
  if (!b) return;
  S.sel = secid;
  openDetail(Object.assign(bondDetail(b), {
    onClose() { S.sel = null; termRefresh({ chrome: false }); },
  }));
  termRefresh({ chrome: false });
}

// Enlarge the map or the table (captain's request, 2026-09-17): the split's
// columns animate (app.css, the design's .22,1,.36,1 ease), the map's SVG
// keeps its height while they move, and the map is redrawn at its new width
// exactly once when the transition ends, easing into its new height -- the
// frame keeps the same bonds selected (TerminalMap rescales it).
let termFocusTimer = 0;
function termSetFocus(next) {
  if (S.focus === next) return;
  const split = document.getElementById('termSplit');
  S.focus = next;
  TerminalMap.holdResize();
  let finished = false;
  const done = () => {
    if (finished) return;
    finished = true;
    split.removeEventListener('transitionend', onEnd);
    clearTimeout(termFocusTimer);
    TerminalMap.releaseResize();
    termRefresh({ rows: false });   // the row set is unchanged; only the map moved
  };
  const onEnd = ev => { if (ev.target === split && ev.propertyName === 'grid-template-columns') done(); };
  split.addEventListener('transitionend', onEnd);
  clearTimeout(termFocusTimer);
  termFocusTimer = setTimeout(done, TERM_FOCUS_MS);
  termRenderChrome();
}

// ── wiring ──────────────────────────────────────────────────────────────
function termOnClick(e) {
  const t = e.target;

  // NB: [data-layout-btn], not [data-layout] -- #termSplit carries
  // data-layout for CSS and would swallow every click inside the split.
  const lay = t.closest('[data-layout-btn]');
  if (lay) { S.layout = lay.dataset.layoutBtn; S.brush = null; S.focus = null; termRenderChrome(); TerminalMap.rebuild(false); termRefresh(); return; }

  const focus = t.closest('[data-focus-btn]');
  if (focus) { termSetFocus(S.focus === focus.dataset.focusBtn ? null : focus.dataset.focusBtn); return; }

  const ax = t.closest('[data-axis]');
  if (ax) { S.axis = ax.dataset.axis; S.brush = null; S.zoomed = false; TerminalMap.rebuild(true); termRefresh(); return; }

  const chip = t.closest('[data-chip]');
  if (chip) { S.filter = chip.dataset.chip; S.shown = SCREENER_PAGE; termRefresh(); return; }

  if (t.closest('#termClearBrush')) { S.brush = null; S.shown = SCREENER_PAGE; TerminalMap.refresh(); termRefresh(); return; }
  if (t.closest('#termResetZoom')) { TerminalMap.resetZoom(); return; }
  if (t.closest('#termMoreBtn')) { S.shown += SCREENER_PAGE; termRenderRows(); return; }

  const th = t.closest('th[data-sort]');
  if (th) {
    const k = th.dataset.sort;
    S.dir = S.sort === k ? -S.dir : (k === 't' ? 1 : -1);
    S.sort = k;
    S.shown = SCREENER_PAGE;
    termInvalidate(); termRenderHead(); termRenderRows();
    return;
  }

  const pick = t.closest('[data-pick]');
  if (pick) { e.stopPropagation(); BASKET.toggle(pick.dataset.pick); return; }

  if (t.closest('#termBasketClear')) { BASKET.clear(); return; }
  if (t.closest('#termBasketBuild')) { pfShowBasket(); goPage('portfolio'); return; }

  const detailAct = t.closest('[data-detail-action]');
  if (detailAct) {
    const b = BOND_BY_SECID.get(detailAct.dataset.secid);
    if (!b) return;
    if (detailAct.dataset.detailAction === 'pick') BASKET.toggle(b.secid);
    else {
      S.custom = termSimilar(b);
      S.filter = S.custom.label;
      S.brush = null;
      S.shown = SCREENER_PAGE;
      closeDetail();   // onClose clears S.sel and refreshes
      termRefresh();
    }
    return;
  }

  const row = t.closest('tr[data-secid]');
  if (row) termOpenBond(row.dataset.secid);
}

PAGES.terminal = {
  render(container) {
    el = container;
    el.innerHTML = termShellHtml();
    el.addEventListener('click', termOnClick);
    // the drawer lives in the app shell, outside this page's container
    document.getElementById('detail').addEventListener('click', termOnClick);
    TerminalMap.mount(document.getElementById('termMapRoot'), ctl);
    BASKET.onChange(() => {
      termRenderBasket();
      el.querySelectorAll('[data-pick]').forEach(btn => {
        const on = BASKET.has(btn.dataset.pick);
        btn.classList.toggle('is-on', on);
        btn.textContent = on ? '✓' : '+';
      });
      const cta = document.getElementById('termDetailPick');
      if (cta) cta.textContent = BASKET.has(cta.dataset.secid) ? 'Убрать из корзины' : 'Добавить в корзину';
      TerminalMap.refresh();
    });
    termRefresh();
  },
};
