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
  ['Доходность ≤ порога', b => b.aey != null && b.aey <= YIELD_GUARD.threshold],
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

const TERM_LAYOUT_NOTE = 'Карта и таблица занимают весь экран. Рамка на карте и строки в таблице всегда рядом — удобно сравнивать глазами.';

const TERM_LEGEND = [
  ['#c67139', 'спред выше справедливого'],
  ['#7a8a5e', 'в пределах модели'],
  ['#8a8478', 'спред ниже справедливого'],
  ['#c0b6a5', 'без рейтинга — справедливый спред не считается'],
];

const S = {
  axis: 'spread',
  filter: 'Все',
  custom: null,     // ad-hoc chip from "Показать похожие на карте": { label, test }
  sort: 'aey',
  dir: -1,
  brush: null,      // unzoomed plot coords, owned by the map
  sel: null,        // secid of the bond whose drawer is open
  shown: SCREENER_PAGE,
  zoomed: false,
  focus: null,      // 'map' | 'screener' enlarged, null = even split
  proposal: null,    // open, unconfirmed map-selection proposal
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

// ── map-selection proposal ──────────────────────────────────────────────
// This is deliberately a browser-side proposal, not a portfolio rebuild. It
// ranks only the current brush, excludes the chosen target's existing bonds,
// and changes BASKET only after the user confirms the checked rows.
function termProposalTargets() {
  const targets = [{ id: 'basket', name: 'Новая корзина', held: new Set(BASKET.ids()) }];
  (DATA.portfolios || []).forEach(p => {
    const held = ((p.allocation && p.allocation.bonds) || []).map(b => b.isin);
    targets.push({ id: String(p.id), name: p.name || `Портфель ${p.id}`, held: new Set(held) });
  });
  return targets;
}

function termProposalTarget(id) {
  return termProposalTargets().find(t => t.id === id) || termProposalTargets()[0];
}

function termRankPoints(rows, get, higher = true) {
  const sorted = rows.slice().sort((a, b) => {
    const x = get(a), y = get(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (higher ? y - x : x - y);
  });
  const out = new Map();
  sorted.forEach((row, i) => out.set(row.secid, (sorted.length - i) / sorted.length));
  return out;
}

function termProposalRows(targetId) {
  const target = termProposalTarget(targetId);
  const pool = BONDS.filter(b => S.brush && TerminalMap.inBrush(b)
    && !target.held.has(b.secid)
    && b.yieldZone !== 'negative_ev');
  const spread = termRankPoints(pool, b => b.gs);
  const quality = termRankPoints(pool, b => b.score);
  const ratingOrder = DATA.rating_order || [];
  const rating = termRankPoints(pool, b => {
    const i = b.rating ? ratingOrder.indexOf(b.rating) : -1;
    return i < 0 ? null : -i;
  });
  const liquidity = termRankPoints(pool, b => b.vol);
  const zone = b => b.yieldZone === 'green' ? 1 : b.yieldZone === 'caution' ? .5 : 0;
  const rows = pool.map(b => Object.assign({}, b, {
    proposalScore: .35 * (spread.get(b.secid) || 0)
      + .30 * (quality.get(b.secid) || 0)
      + .20 * (rating.get(b.secid) || 0)
      + .10 * (liquidity.get(b.secid) || 0)
      + .05 * zone(b),
  }));
  return rows.sort((a, b) => b.proposalScore - a.proposalScore || (b.gs == null ? -Infinity : b.gs) - (a.gs == null ? -Infinity : a.gs)).slice(0, 30);
}

function termProposalWeakest(targetId) {
  const target = termProposalTarget(targetId);
  if (target.id === 'basket') return [];
  const order = DATA.rating_order || [];
  return BONDS.filter(b => target.held.has(b.secid)).sort((a, b) => {
    const zone = x => x.yieldZone === 'negative_ev' ? 0 : x.yieldZone === 'caution' ? 1 : 2;
    const ar = a.rating ? order.indexOf(a.rating) : 999, br = b.rating ? order.indexOf(b.rating) : 999;
    return zone(a) - zone(b) || (a.score == null ? -1 : b.score == null ? 1 : a.score - b.score)
      || br - ar || (a.gap || 0) - (b.gap || 0);
  });
}

function termOpenProposal() {
  if (!S.brush) return;
  const target = termProposalTargets()[0];
  const rows = termProposalRows(target.id);
  S.proposal = { target: target.id, rows, selected: new Set(rows.map(b => b.secid)) };
  termRenderProposal();
}

function termRenderProposal() {
  const root = document.getElementById('termProposalRoot');
  if (!root) return;
  if (!S.proposal) { root.innerHTML = ''; return; }
  const target = termProposalTarget(S.proposal.target);
  const rows = S.proposal.rows;
  const weakest = termProposalWeakest(target.id);
  const selected = S.proposal.selected;
  root.innerHTML = `<div class="term-proposal-backdrop" data-proposal-close>
    <section class="term-proposal" role="dialog" aria-modal="true" aria-labelledby="termProposalTitle">
      <div class="term-proposal-head">
        <div><div class="term-proposal-kicker">Предложение по выделению</div><h2 id="termProposalTitle">Кандидаты в портфель</h2></div>
        <button type="button" class="detail-close" data-proposal-close aria-label="Закрыть">×</button>
      </div>
      <p class="term-proposal-note">Выбрано ${fmtNum(rows.length, 0)} бумаг. Ранжирование: G-спред, качество бизнеса и фундаментальные показатели. Ничего не меняется без подтверждения.</p>
      <div class="term-proposal-targets"><span>С чем сравнить:</span>${termProposalTargets().map(t => `<button type="button" class="pf-pill${t.id === target.id ? ' is-on' : ''}" data-proposal-target="${escapeHtml(t.id)}">${escapeHtml(t.name)}${t.id === 'basket' ? ` (${fmtNum(t.held.size, 0)})` : ''}</button>`).join('')}</div>
      ${target.id !== 'basket' ? `<div class="term-proposal-warning">Существующие позиции не заменяются. Ниже показаны только новые бумаги из выделения; подпись «вместо» — ориентир для ручного сравнения.</div>` : ''}
      <div class="term-proposal-list">
        ${rows.length ? rows.map((b, i) => {
          const replacement = weakest[i];
          return `<label class="term-proposal-row"><input type="checkbox" data-proposal-pick="${escapeHtml(b.secid)}"${selected.has(b.secid) ? ' checked' : ''}><span class="term-proposal-main"><b>${escapeHtml(b.t)}</b><small>${escapeHtml(b.n)}${replacement ? ` · вместо ${escapeHtml(replacement.t)}` : ''}</small></span><span class="term-proposal-metrics"><b>${b.gs == null ? DASH : fmtBpSigned(b.gs)}</b><small>G-спред · AEY ${fmtPct(b.aey, 1)} · балл ${b.score == null ? DASH : fmtNum(b.score, 1)}</small></span></label>`;
        }).join('') : '<div class="term-proposal-empty">В выделении нет новых подходящих кандидатов для этого портфеля.</div>'}
      </div>
      <div class="term-proposal-actions"><span>${fmtNum(selected.size, 0)} выбрано</span><button type="button" class="pill-outline" data-proposal-close>Отмена</button><button type="button" class="pill-primary" data-proposal-confirm${selected.size ? '' : ' disabled'}>Подтвердить и добавить в корзину</button></div>
    </section>
  </div>`;
}

// ── rendering ───────────────────────────────────────────────────────────
function termShellHtml() {
  return `<div class="page-terminal">

    <div class="term-toolbar">
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
          <button type="button" class="chip-clear" id="termProposeBtn" hidden>Предложить в портфель</button>
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
    <div id="termProposalRoot"></div>
  </div>`;
}

function termRenderChips() {
  const chips = TERM_CHIPS.map(c => c[0]);
  const custom = S.custom && !chips.includes(S.custom.label) ? S.custom.label : null;
  document.getElementById('termChips').innerHTML =
    (custom ? `<button type="button" class="chip is-custom${S.filter === custom ? ' is-on' : ''}" data-chip="${escapeHtml(custom)}">${escapeHtml(custom)}</button>` : '')
    + chips.map(c => {
      const label = c === 'Доходность ≤ порога' ? `Доходность ≤ ${fmtPct(YIELD_GUARD.threshold, 0)}` : c;
      return `<button type="button" class="chip${S.filter === c ? ' is-on' : ''}" data-chip="${escapeHtml(c)}">${escapeHtml(label)}</button>`;
    }).join('');
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
    <td class="al-center"><span class="rat ${ratCls}${b.ratingStale ? ' is-stale' : ''}" title="${escapeHtml(ratingTitle(b))}">${escapeHtml(ratingLabel(b))}${b.ratingSplit ? '<span class="rat-split" aria-label="агентства расходятся">≠</span>' : ''}${b.ratingMove ? `<span class="rat-move is-${b.ratingMove}" aria-label="${b.ratingMove === 'down' ? 'рейтинг понижен за 30 дней' : 'рейтинг повышен за 30 дней'}">${b.ratingMove === 'down' ? '↓' : '↑'}</span>` : ''}</span></td>
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
  const risky = bs.filter(b => !YIELD_GUARD.isSafe(b));
  const guardSummary = risky.length
    ? `${risky.length} ${plural(risky.length, ['бумага', 'бумаги', 'бумаг'])} нуждается в проверке`
    : 'Все бумаги проходят текущий фильтр безопасности';
  const alt = risky.length ? YIELD_GUARD.alternatives(risky[0], ids).slice(0, 3) : [];
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
    <div class="basket-guard${risky.length ? ' is-warn' : ' is-ok'}">
      <div class="basket-guard-line"><span class="basket-guard-dot"></span><b>${escapeHtml(guardSummary)}</b><button type="button" class="basket-guard-toggle" data-guard-expand="1">${risky.length ? 'Проверить' : 'Настроить'}</button></div>
      <div class="basket-guard-panel"${risky.length ? ' hidden' : ''}>
        <div class="basket-guard-controls"><label>Порог риска <output>${fmtPct(YIELD_GUARD.threshold, 0)}</output><input class="yg-range" type="range" min="10" max="60" step="5" value="${YIELD_GUARD.threshold}" data-guard-threshold></label><label class="yg-check"><input type="checkbox" data-guard-stable ${YIELD_GUARD.requireStable ? 'checked' : ''}> учитывать стабильность G-спреда</label></div>
        ${risky.length ? `<div class="basket-guard-reason">${escapeHtml(YIELD_GUARD.reasons(risky[0]).join(' · '))}</div><div class="basket-alternatives"><div class="basket-alt-title">Безопаснее сейчас · ${escapeHtml(risky[0].t)}</div>${alt.length ? alt.map(x => `<button type="button" class="basket-alt" data-open-secid="${escapeHtml(x.secid)}"><span>${escapeHtml(x.t)}</span><span>${fmtPct(x.aey, 1)} · ${escapeHtml(x.rating || 'без рейтинга')}</span></button>`).join('') : '<div class="basket-alt-empty">Подходящей альтернативы в текущем снимке нет.</div>'}</div>` : ''}
      </div>
    </div>
    <div class="basket-actions">
      <button type="button" class="basket-clear" id="termBasketClear">Очистить</button>
      <button type="button" class="basket-build" id="termBasketBuild">Собрать портфель</button>
    </div>`;
}

function termRenderChrome() {
  document.getElementById('termLayoutNote').textContent = TERM_LAYOUT_NOTE;
  document.querySelectorAll('[data-axis]').forEach(b => b.classList.toggle('is-on', b.dataset.axis === S.axis));
  termRenderChips();
  const split = document.getElementById('termSplit');
  if (S.focus) split.dataset.focus = S.focus; else delete split.dataset.focus;
  // Enlarge controls: the side-by-side layout only. The enlarged card's own
  // button returns to the even split; the other card's button swaps.
  document.querySelectorAll('[data-focus-btn]').forEach(btn => {
    const which = btn.dataset.focusBtn;
    btn.hidden = false;
    btn.textContent = S.focus === which ? '⇆ Вернуть сплит' : which === 'map' ? '⤢ Развернуть карту' : '⤢ Развернуть таблицу';
    btn.setAttribute('aria-pressed', S.focus === which ? 'true' : 'false');
  });
  document.getElementById('termMapTitle').textContent = TerminalMap.title(S.axis);
  document.getElementById('termClearBrush').hidden = !S.brush;
  document.getElementById('termResetZoom').hidden = !S.zoomed;
  const propose = document.getElementById('termProposeBtn');
  propose.hidden = !S.brush;
  propose.textContent = S.brush ? 'Предложить в портфель' : '';

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
  termRenderProposal();
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

  if (t.closest('[data-proposal-close]')) {
    S.proposal = null;
    termRenderProposal();
    return;
  }
  const proposalTarget = t.closest('[data-proposal-target]');
  if (proposalTarget && S.proposal) {
    const target = proposalTarget.dataset.proposalTarget;
    const rows = termProposalRows(target);
    S.proposal = { target, rows, selected: new Set(rows.map(b => b.secid)) };
    termRenderProposal();
    return;
  }
  const proposalPick = t.closest('[data-proposal-pick]');
  if (proposalPick && S.proposal) {
    const id = proposalPick.dataset.proposalPick;
    if (S.proposal.selected.has(id)) S.proposal.selected.delete(id); else S.proposal.selected.add(id);
    termRenderProposal();
    return;
  }
  if (t.closest('[data-proposal-confirm]') && S.proposal) {
    const ids = Array.from(S.proposal.selected);
    ids.forEach(id => { if (!BASKET.has(id)) BASKET.toggle(id); });
    S.proposal = null;
    termRenderProposal();
    if (ids.length) { pfShowBasket(); goPage('portfolio'); }
    return;
  }
  if (t.closest('#termProposeBtn')) { termOpenProposal(); return; }

  const focus = t.closest('[data-focus-btn]');
  if (focus) { termSetFocus(S.focus === focus.dataset.focusBtn ? null : focus.dataset.focusBtn); return; }

  const ax = t.closest('[data-axis]');
  if (ax) { S.axis = ax.dataset.axis; S.brush = null; S.zoomed = false; TerminalMap.rebuild(true); termRefresh(); return; }

  const chip = t.closest('[data-chip]');
  if (chip) { S.filter = chip.dataset.chip; S.shown = SCREENER_PAGE; termRefresh(); return; }

  const guardToggle = t.closest('[data-guard-expand]');
  if (guardToggle) {
    const panel = document.querySelector('#termBasket .basket-guard-panel');
    if (panel) panel.hidden = !panel.hidden;
    return;
  }
  const openBond = t.closest('[data-open-secid]');
  if (openBond) { termOpenBond(openBond.dataset.openSecid); return; }

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
    el.addEventListener('input', e => {
      if (e.target.matches('[data-guard-threshold]')) YIELD_GUARD.setThreshold(e.target.value);
    });
    el.addEventListener('change', e => {
      if (e.target.matches('[data-guard-stable]')) YIELD_GUARD.setRequireStable(e.target.checked);
    });
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
    YIELD_GUARD.onChange(() => { termRenderChips(); termRenderBasket(); });
    termRefresh();
  },
};
