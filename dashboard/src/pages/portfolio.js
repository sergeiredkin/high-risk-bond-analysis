// Портфель -- the isPortfolio page of bond-lab-design-reference.html.
//
// Four sources sit behind one layout: the three preset portfolios the
// pipeline itself built (DATA.portfolios, portfolio.build_preset) and the
// basket the screener persists in localStorage (BASKET, src/bonds.js).
// "Собрать портфель" on Карта и скринер lands here with the basket already
// selected -- that handoff is the whole point of the basket.
//
// Every position is resolved back through BOND_BY_SECID so the whole page
// speaks one unit system, and every rate on it is a percent. This matters:
// DATA.portfolios stores aey_base, avg_aey, expected_loss and net_return as
// FRACTIONS (0.218), while universe_now -- and therefore src/bonds.js --
// carries the same quantities in PERCENT (21.82). Rendering the stored
// numbers directly would silently print 0.2% where 21.8% belongs, so this
// page reads exactly one field off the stored allocation: `weight`, which
// is a fraction of the portfolio and never shown as a rate.
//
// A preset position whose bond is absent from the latest MOEX snapshot
// (matured, or simply not traded that session) keeps its weight and shows
// "—" for every live number, with a count in the composition note. It is
// never back-filled from the stored snapshot -- see the "ничего не
// выдумываем" rule in core.js.
//
// Removing a position (captain's request, 2026-09-17): the "−" on a row, or
// «Убрать» on a failed check, takes a bond out of the portfolio on this page
// and every tile, share and check is recomputed over what is left. For a
// preset the removal is kept in this browser's localStorage under the preset
// TYPE, so it survives the half-hourly dashboard rebuild and a later rebuild
// of the preset itself; the pipeline's portfolio in the database is not
// changed (a static page cannot write to it). For the basket, removing is
// taking the bond out of the basket. Every removal is listed under the
// composition and can be undone.

const PF_PALETTE = ['#c67139', '#7a8a5e', '#b2622d', '#8fa073', '#8c491a',
                    '#aebf92', '#d67f48', '#728157', '#f6a06b', '#56633f'];
const PF_ROWS_SHOWN = 12;           // rows before "показать все"
const PF_PRESET_ORDER = ['conservative', 'balanced', 'aggressive'];

const PF_PRESETS = (DATA.portfolios || [])
  .slice()
  .sort((a, b) => PF_PRESET_ORDER.indexOf(a.preset_type) - PF_PRESET_ORDER.indexOf(b.preset_type));

const PF = {
  source: null,       // portfolio id, or 'basket'
  expanded: false,
};

// Positions the user took out: { 'preset:conservative': [secid, …], basket: [secid, …] }.
const PF_REMOVED = (() => {
  const KEY = 'kl.pf.removed';
  let map = {};
  try { map = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { map = {}; }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(map)); } catch (e) { /* storage unavailable: removal lasts this session */ } }
  return {
    list: key => (key && map[key] ? map[key].slice() : []),
    add(key, ids) { if (!key) return; map[key] = Array.from(new Set((map[key] || []).concat(ids))); save(); },
    restore(key, ids) {
      if (!key || !map[key]) return;
      map[key] = map[key].filter(id => !ids.includes(id));
      if (!map[key].length) delete map[key];
      save();
    },
  };
})();

// Keyed by preset TYPE, not portfolio id: a rebuilt preset gets a new id but is
// the same portfolio to the person who removed a bond from it.
function pfRemovalKey() {
  if (PF.source === 'basket') return 'basket';
  const cur = pfCurrent();
  return cur ? `preset:${cur.preset_type || cur.id}` : null;
}

// ── position model ──────────────────────────────────────────────────────
// { secid, weight, bond|null, stored|null }. `bond` is the live model row;
// `stored` is the allocation row a preset carries, kept only for the name
// of a position that has dropped out of the snapshot.
function pfAllPositions() {
  if (PF.source === 'basket') {
    const ids = BASKET.ids();
    const w = ids.length ? 1 / ids.length : 0;
    return ids.map(id => ({ secid: id, weight: w, bond: BOND_BY_SECID.get(id) || null, stored: null }));
  }
  const pf = PF_PRESETS.find(p => p.id === PF.source);
  const bonds = (pf && pf.allocation && pf.allocation.bonds) || [];
  return bonds.map(b => ({
    secid: b.isin, weight: b.weight,
    bond: BOND_BY_SECID.get(b.isin) || null, stored: b,
  }));
}

// The positions every tile, share and check is computed over: the preset
// minus what the user removed (the basket's removals already left the basket).
function pfPositions() {
  const removed = PF_REMOVED.list(pfRemovalKey());
  return pfAllPositions().filter(p => !removed.includes(p.secid));
}

// The removed positions, for the restore list.
function pfRemovedPositions() {
  const key = pfRemovalKey();
  const removed = PF_REMOVED.list(key);
  if (PF.source === 'basket') {
    return removed.filter(id => !BASKET.has(id))
      .map(id => ({ secid: id, weight: null, bond: BOND_BY_SECID.get(id) || null, stored: null }));
  }
  return pfAllPositions().filter(p => removed.includes(p.secid));
}

function pfCurrent() { return PF_PRESETS.find(p => p.id === PF.source) || null; }

function pfName(pos) {
  if (pos.bond) return pos.bond.t;
  return (pos.stored && pos.stored.short_name) || pos.secid;
}

// Weighted mean over the positions that actually have the value. Weights are
// renormalised to the covered subset, so a portfolio where a third of the
// positions have no price reports the average of the two thirds that do --
// and pfCoverage() below says how much that is.
function pfWeightedMean(positions, get) {
  let num = 0, den = 0;
  positions.forEach(p => {
    const v = p.bond ? get(p.bond) : null;
    if (v != null && isFinite(v) && p.weight) { num += v * p.weight; den += p.weight; }
  });
  return den > 0 ? num / den : null;
}

function pfCoverage(positions, get) {
  return positions.filter(p => p.bond && get(p.bond) != null && isFinite(get(p.bond))).length;
}

// ── stat tiles ──────────────────────────────────────────────────────────
function pfStats(positions) {
  // aey is (1 − PD)·AEY − PD·LGD, rated bonds only; aeyBase is before credit
  // losses. Both already percent. Their difference IS the expected loss the
  // pipeline priced -- no separate loss model here.
  const aey = pfWeightedMean(positions, b => b.aey);
  const base = pfWeightedMean(positions, b => b.aeyBase);
  const nAey = pfCoverage(positions, b => b.aey);
  const dur = pfWeightedMean(positions, b => b.dur);
  const nDur = pfCoverage(positions, b => b.dur);
  const scores = positions.map(p => p.bond && p.bond.score).filter(v => v != null);
  const inns = new Set(positions.map(p => p.bond && p.bond.inn).filter(Boolean));
  const sectors = new Set(positions.map(p => p.bond && p.bond.sector).filter(Boolean));
  const noData = positions.filter(p => !p.bond).length;
  const removedN = pfRemovedPositions().length;

  const lossNote = aey != null && base != null
    ? `AEY до потерь ${fmtPct(base, 2)} минус ожидаемые потери ${fmtNum(base - aey, 2)} п.п.`
    : 'ни одна позиция не имеет рейтинга — кредитные потери не моделируются';

  // The AEY tile goes green only when the pipeline's own yield zone says the
  // portfolio is clean. Same rule as the screener's AEY column: a portfolio
  // of distressed bonds can average a three-digit AEY, and rendering that as
  // the good-news tile would say the opposite of what the model concluded.
  const red = positions.filter(p => p.bond && p.bond.yieldZone === 'negative_ev').length;
  const zoneNote = red
    ? ` · ${red} ${plural(red, ['позиция', 'позиции', 'позиций'])} в красной зоне — см. проверку ниже`
    : '';

  return [
    { k: 'AEY портфеля', v: fmtPct(aey, 2), green: !red,
      note: aey == null ? lossNote : `${lossNote} · по ${nAey} из ${positions.length}${zoneNote}` },
    { k: 'Дюрация', v: dur == null ? DASH : `${fmtNum(dur, 2)} г.`,
      note: dur == null ? 'дюрация не выгружена ни по одной позиции'
        : `снижение ставки на 1 п.п. даёт около +${fmtNum(dur, 1)}% к цене · по ${nDur} из ${positions.length}` },
    { k: 'Минимальный балл', v: scores.length ? `${fmtNum(Math.min.apply(null, scores), 1)} / 10` : DASH,
      note: scores.length
        ? `слабейшая опора по портфелю · балл есть у ${scores.length} из ${positions.length}`
        : 'балл РСБУ считается только для эмитентов под мониторингом' },
    { k: 'Бумаг', v: fmtNum(positions.length, 0),
      note: `${inns.size} ${plural(inns.size, ['эмитент', 'эмитента', 'эмитентов'])} · ${sectors.size} ${plural(sectors.size, ['отрасль', 'отрасли', 'отраслей'])}`
        + (noData ? ` · ${noData} без котировки` : '')
        + (removedN ? ` · ${removedN} ${plural(removedN, ['убрана', 'убраны', 'убрано'])} вами` : '') },
  ];
}

// ── composition ─────────────────────────────────────────────────────────
function pfCompositionHtml(positions) {
  const cur = pfCurrent();
  const total = positions.reduce((a, p) => a + (p.weight || 0), 0) || 1;
  const sorted = positions.slice().sort((a, b) => (b.weight || 0) - (a.weight || 0));
  const shown = PF.expanded ? sorted : sorted.slice(0, PF_ROWS_SHOWN);
  const noData = positions.filter(p => !p.bond).length;

  const title = PF.source === 'basket' ? 'Портфель из корзины' : `${cur ? cur.name : ''} портфель`;
  let note;
  if (PF.source === 'basket') {
    note = `Собран из ${positions.length} ${plural(positions.length, ['бумаги', 'бумаг', 'бумаг'])}, отобранных в скринере. `
      + 'Веса равные: это выборка, а не оптимизированный портфель — проверки ниже считаются по этим равным долям.';
  } else if (cur) {
    const sized = cur.sizing_mode === 'equal' ? 'равные доли' : escapeHtml(cur.sizing_mode);
    note = `Собран конвейером ${fmtDate(cur.created_at)} по правилам пресета «${cur.name}» (${sized}). `
      + 'Доли — из выгрузки; доходность, дюрация и балл пересчитаны по последнему снимку МосБиржи.';
  } else {
    note = '';
  }
  if (noData) {
    note += ` ${noData} ${plural(noData, ['позиция', 'позиции', 'позиций'])} нет в последнем снимке — `
      + 'доля сохранена, живые числа показаны как отсутствующие.';
  }

  const bars = sorted.map((p, i) =>
    `<div style="width:${(p.weight / total * 100).toFixed(3)}%;background:${PF_PALETTE[i % PF_PALETTE.length]}"></div>`).join('');

  const rows = shown.map((p, i) => {
    const b = p.bond;
    const sub = b
      ? [b.n, b.m == null ? null : `${fmtNum(b.m, 1)} г.`, b.rating || 'без рейтинга'].filter(Boolean).join(' · ')
      : 'нет в последнем снимке МосБиржи';
    return `<div class="pf-row" data-pf-secid="${escapeHtml(p.secid)}"${b ? '' : ' data-pf-dead="1"'}>
      <div class="pf-row-swatch" style="background:${PF_PALETTE[i % PF_PALETTE.length]}"></div>
      <div class="pf-row-main">
        <div class="pf-row-t">${escapeHtml(pfName(p))}</div>
        <div class="pf-row-sub">${escapeHtml(sub)}</div>
      </div>
      <div class="pf-row-right">
        <div class="pf-row-w">${fmtNum(p.weight / total * 100, 1)}%</div>
        <div class="pf-row-aey">${b && b.aey != null ? `AEY ${fmtPct(b.aey, 1)}` : DASH}</div>
      </div>
      <button type="button" class="pick" data-pf-remove="${escapeHtml(p.secid)}" title="Убрать из портфеля" aria-label="Убрать ${escapeHtml(pfName(p))} из портфеля">−</button>
    </div>`;
  }).join('');

  const removed = pfRemovedPositions();
  const removedHtml = removed.length ? `
    <div class="pf-card-note">Убрано вами: ${fmtNum(removed.length, 0)} ${plural(removed.length, ['позиция', 'позиции', 'позиций'])}. Доли, доходность и проверки выше пересчитаны без ${plural(removed.length, ['неё', 'них', 'них'])}. ${PF.source === 'basket'
      ? 'Бумаги убраны из корзины.'
      : 'Хранится в этом браузере и переживает пересборку дашборда; портфель конвейера в базе не меняется.'} «+» возвращает позицию.</div>
    <div class="pf-rows">${removed.map(p => {
      const b = p.bond;
      return `<div class="pf-row" data-pf-dead="1">
        <div class="pf-row-swatch" style="background:var(--color-neutral-400)"></div>
        <div class="pf-row-main">
          <div class="pf-row-t">${escapeHtml(pfName(p))}</div>
          <div class="pf-row-sub">${escapeHtml(b ? [b.n, b.rating || 'без рейтинга', b.yieldZone === 'negative_ev' ? 'красная зона' : null].filter(Boolean).join(' · ') : 'нет в последнем снимке МосБиржи')}</div>
        </div>
        <div class="pf-row-right"><div class="pf-row-aey">убрана</div></div>
        <button type="button" class="pick" data-pf-restore="${escapeHtml(p.secid)}" title="Вернуть в портфель" aria-label="Вернуть ${escapeHtml(pfName(p))} в портфель">+</button>
      </div>`;
    }).join('')}</div>
    ${removed.length > 1 ? `<button type="button" class="pf-more" data-pf-restore="${escapeHtml(removed.map(p => p.secid).join(','))}">Вернуть все ${fmtNum(removed.length, 0)}</button>` : ''}` : '';

  const rest = sorted.length - shown.length;
  const more = rest > 0 || PF.expanded
    ? `<button type="button" class="pf-more" id="pfMore">${PF.expanded ? 'Свернуть список' : `Показать все ${fmtNum(sorted.length, 0)}`}</button>`
    : '';

  return `<section class="pf-card">
    <div class="pf-card-title">${escapeHtml(title)}</div>
    <div class="pf-card-note">${escapeHtml(note)}</div>
    <div class="pf-bar">${bars}</div>
    <div class="pf-rows">${rows}</div>
    ${more}
    ${removedHtml}
  </section>`;
}

// The rate tracker that used to live here moved to «Ставка и ОФЗ» (captain,
// 2026-09-18): it duplicated that page's own «Траектория по сценариям»
// panel (rates.js), which draws the same published path across every CBR
// scenario, not just one. pf-aside is single-panel until a rate-scenario
// portfolio-impact projection (3-year horizon) replaces this slot -- follow-up
// scope, not built here.

// ── Проверка портфеля ───────────────────────────────────────────────────
// Checks a preset portfolio against the very constraints build_preset()
// used (DATA.portfolios[].constraints -> portfolio.PRESETS). The basket was
// not built by those rules, so it is checked against what can be stated
// without them: concentration, the pipeline's yield zone, events, liquidity
// and data coverage. Nothing here invents a threshold.
function pfChecks(positions) {
  const cur = pfCurrent();
  const cons = (cur && cur.constraints) || null;
  const total = positions.reduce((a, p) => a + (p.weight || 0), 0) || 1;
  const out = [];

  const byKey = (key, label) => {
    const m = new Map();
    positions.forEach(p => {
      const k = p.bond ? p.bond[key] : null;
      if (k == null) return;
      m.set(k, (m.get(k) || 0) + (p.weight || 0) / total);
    });
    let top = null;
    m.forEach((w, k) => { if (!top || w > top.w) top = { k, w }; });
    return top ? { top, label } : null;
  };

  // 1. issuer concentration
  const iss = byKey('inn');
  if (iss) {
    const name = (positions.find(p => p.bond && p.bond.inn === iss.top.k) || {}).bond.n;
    const cap = cons && cons.max_per_issuer;
    const bad = cap != null && iss.top.w > cap + 1e-9;
    out.push({ g: cap == null ? 'i' : bad ? '!' : '✓', tone: cap == null ? 'neutral' : bad ? 'warn' : 'ok',
      t: cap != null
        ? `Максимальная доля эмитента — ${fmtPct(iss.top.w * 100, 1)} (${name}) при лимите пресета ${fmtPct(cap * 100, 0)}.${bad ? ' Лимит нарушен.' : ' Нарушений нет.'}`
        : `Максимальная доля эмитента — ${fmtPct(iss.top.w * 100, 1)} (${name}). Корзина собрана вручную, лимита по эмитенту у неё нет.` });
  }

  // 2. sector concentration
  const sec = byKey('sector');
  if (sec) {
    const b = positions.find(p => p.bond && p.bond.sector === sec.top.k).bond;
    const cap = cons && cons.sector_cap;
    const bad = cap != null && sec.top.w > cap + 1e-9;
    out.push({ g: cap == null ? 'i' : bad ? '!' : '✓', tone: cap == null ? 'neutral' : bad ? 'warn' : 'ok',
      t: cap != null
        ? `Максимальная доля отрасли — ${fmtPct(sec.top.w * 100, 1)} (${b.sectorLabel || b.sector}) при лимите ${fmtPct(cap * 100, 0)}.${bad ? ' Лимит нарушен.' : ' Нарушений нет.'}`
        : `Максимальная доля отрасли — ${fmtPct(sec.top.w * 100, 1)} (${b.sectorLabel || b.sector}).` });
  }

  // 3. yield zone -- the pipeline's own verdict, the one check that reads
  //    a high yield as distress rather than as return
  const red = positions.filter(p => p.bond && p.bond.yieldZone === 'negative_ev');
  const caution = positions.filter(p => p.bond && p.bond.yieldZone === 'caution');
  out.push({ g: red.length ? '!' : caution.length ? 'i' : '✓', tone: red.length ? 'warn' : caution.length ? 'neutral' : 'ok',
    ids: red.map(p => p.secid),
    t: red.length
      ? `${red.length} ${plural(red.length, ['позиция', 'позиции', 'позиций'])} в красной зоне доходности (${red.slice(0, 3).map(p => p.bond.t).join(', ')}) — модель считает ожидаемую доходность отрицательной.`
      : caution.length
        ? `Красной зоны нет. ${caution.length} ${plural(caution.length, ['позиция', 'позиции', 'позиций'])} в жёлтой — доходность выше порога перегиба для своего рейтинга.`
        : 'Ни одной позиции в жёлтой или красной зоне доходности.' });

  // 4. events on record. Counted by ISSUER, not by position: two series of
  // the same issuer carry the same event and must not read as two problems.
  const ev = positions.filter(p => p.bond && (p.bond.recentSerious.length || p.bond.flagsToday.length));
  const evInns = new Set(ev.map(p => p.bond.inn));
  const evNames = Array.from(new Set(ev.map(p => p.bond.n)));
  out.push({ g: ev.length ? '!' : '✓', tone: ev.length ? 'warn' : 'ok',
    ids: ev.map(p => p.secid),
    t: ev.length
      ? `${evInns.size} ${plural(evInns.size, ['эмитент', 'эмитента', 'эмитентов'])} с серьёзным событием или флагом детектора: ${evNames.slice(0, 3).join(', ')}${evNames.length > 3 ? ' и др' : ''}.`
      : 'Ни у одного эмитента нет серьёзного события за 30 дней и флагов детектора на сегодня.' });

  // 5. liquidity -- against the screener's own low-liquidity floor
  const thin = positions.filter(p => p.bond && p.bond.vol != null && p.bond.vol < LOW_LIQ_VOLUME_RUB);
  const noVol = positions.filter(p => !p.bond || p.bond.vol == null);
  out.push({ g: thin.length ? '!' : '✓', tone: thin.length ? 'warn' : 'ok',
    ids: thin.map(p => p.secid),
    t: (thin.length
      ? `${thin.length} ${plural(thin.length, ['позиция', 'позиции', 'позиций'])} с оборотом ниже ${fmtRub(LOW_LIQ_VOLUME_RUB)} за сессию — выход займёт несколько дней.`
      : `Все позиции с котировкой прошли порог оборота ${fmtRub(LOW_LIQ_VOLUME_RUB)} за сессию.`)
      + (noVol.length ? ` По ${noVol.length} оборот не выгружен.` : '') });

  // 6. rating floor (preset only)
  if (cons && cons.min_rating) {
    const order = DATA.rating_order || [];
    const floor = order.indexOf(cons.min_rating);
    const below = floor < 0 ? [] : positions.filter(p => {
      const i = p.bond && p.bond.rating ? order.indexOf(p.bond.rating) : -1;
      return i >= 0 && i > floor;
    });
    const unrated = positions.filter(p => p.bond && !p.bond.rating).length;
    out.push({ g: below.length ? '!' : '✓', tone: below.length ? 'warn' : 'ok',
      ids: below.map(p => p.secid),
      t: (below.length
        ? `${below.length} ${plural(below.length, ['позиция', 'позиции', 'позиций'])} ниже рейтингового пола пресета ${cons.min_rating}: ${below.slice(0, 3).map(p => p.bond.t).join(', ')}.`
        : `Ни одна позиция не опускается ниже рейтингового пола пресета ${cons.min_rating}.`)
        + (unrated ? ` ${unrated} без рейтинга — пол к ним неприменим.` : '') });
  }

  // 7. data coverage -- always last, always stated
  const live = positions.filter(p => p.bond).length;
  out.push({ g: live === positions.length ? '✓' : 'i', tone: 'neutral',
    t: live === positions.length
      ? `Все ${positions.length} ${plural(positions.length, ['позиция', 'позиции', 'позиций'])} есть в снимке МосБиржи от ${fmtDate((DATA.universe_now || {}).snap_date)}. Ни одно поле не заполнено оценкой.`
      : `${live} из ${positions.length} позиций есть в снимке от ${fmtDate((DATA.universe_now || {}).snap_date)}; остальные показаны без живых чисел, а не оценкой.` });

  return out;
}

// A failed check that names positions offers to take exactly those out.
function pfChecksHtml(positions) {
  const items = pfChecks(positions).map(c => {
    const act = c.ids && c.ids.length
      ? ` <button type="button" class="chip-clear" data-pf-remove="${escapeHtml(c.ids.join(','))}">Убрать ${fmtNum(c.ids.length, 0)} из портфеля</button>`
      : '';
    return `<div class="pf-check"><div class="pf-check-g is-${c.tone}">${escapeHtml(c.g)}</div><div class="pf-check-t">${escapeHtml(c.t)}${act}</div></div>`;
  }).join('');
  return `<section class="pf-checks">
    <div class="pf-checks-kicker">Проверка портфеля</div>
    ${items}
  </section>`;
}

// ── page ────────────────────────────────────────────────────────────────
function pfSourceButtons() {
  const n = BASKET.ids().length;
  const btns = PF_PRESETS.map(p =>
    `<button type="button" class="pf-pill${PF.source === p.id ? ' is-on' : ''}" data-pf-source="${p.id}">${escapeHtml(p.name)}</button>`);
  btns.push(`<button type="button" class="pf-pill${PF.source === 'basket' ? ' is-on' : ''}${n ? '' : ' is-empty'}" data-pf-source="basket">Из корзины (${n})</button>`);
  return btns.join('');
}

// The source the page opens on the FIRST time it is shown: the basket when
// the screener has already filled it, otherwise the first preset. After
// that PF.source is sticky and only changes on an explicit selection.
function pfDefaultSource() {
  if (BASKET.ids().length) return 'basket';
  return PF_PRESETS.length ? PF_PRESETS[0].id : 'basket';
}

// "Собрать портфель" on Карта и скринер. The screener calls this instead of
// bare goPage('portfolio') because the default above is only consulted on
// first render: a user who had already opened Портфель would otherwise fill
// the basket, press the button, and land back on whichever preset they last
// looked at -- which is precisely the handoff this page exists to serve.
function pfShowBasket() {
  PF.source = 'basket';
  PF.expanded = false;
  if (document.getElementById('pfBody')) pfRender();
}

function pfEmptyHtml() {
  return `<section class="pf-card">
    <div class="pf-card-title">В корзине пока пусто</div>
    <div class="pf-card-note">Откройте «Карта и скринер», отметьте бумаги кнопкой «+» в таблице или на карте и нажмите «Собрать портфель» — выборка появится здесь. Корзина хранится в этом браузере.</div>
    <div class="pf-empty-actions"><button type="button" class="pill-primary" data-page="terminal">Перейти к скринеру</button></div>
  </section>`;
}

function pfRender() {
  const positions = pfPositions();
  const body = document.getElementById('pfBody');
  const head = document.getElementById('pfSources');
  if (head) head.innerHTML = pfSourceButtons();
  if (!body) return;

  if (!positions.length) {
    // everything removed: keep the restore list reachable instead of an empty page
    body.innerHTML = pfRemovedPositions().length
      ? `<div class="pf-split">${pfCompositionHtml(positions)}</div>` : pfEmptyHtml();
    return;
  }

  body.innerHTML = `
    <div class="stat-grid">
      ${pfStats(positions).map(s => `<div class="stat${s.green ? ' is-green' : ''}">
        <div class="stat-k">${escapeHtml(s.k)}</div>
        <div class="stat-v">${escapeHtml(s.v)}</div>
        <div class="stat-note">${escapeHtml(s.note)}</div>
      </div>`).join('')}
    </div>
    <div class="pf-split">
      ${pfCompositionHtml(positions)}
      <div class="pf-aside">
        ${pfChecksHtml(positions)}
      </div>
    </div>`;
}

function pfOnClick(e) {
  const src = e.target.closest('[data-pf-source]');
  if (src) {
    const v = src.dataset.pfSource;
    PF.source = v === 'basket' ? 'basket' : Number(v);
    PF.expanded = false;
    pfRender();
    return;
  }
  if (e.target.closest('#pfMore')) { PF.expanded = !PF.expanded; pfRender(); return; }

  const rm = e.target.closest('[data-pf-remove]');
  if (rm) {
    e.stopPropagation();
    const ids = rm.dataset.pfRemove.split(',');
    PF_REMOVED.add(pfRemovalKey(), ids);
    if (PF.source === 'basket') ids.forEach(id => { if (BASKET.has(id)) BASKET.toggle(id); });  // onChange re-renders
    pfRender();
    return;
  }
  const back = e.target.closest('[data-pf-restore]');
  if (back) {
    e.stopPropagation();
    const ids = back.dataset.pfRestore.split(',');
    PF_REMOVED.restore(pfRemovalKey(), ids);
    if (PF.source === 'basket') ids.forEach(id => { if (!BASKET.has(id) && BOND_BY_SECID.has(id)) BASKET.toggle(id); });
    pfRender();
    return;
  }

  const row = e.target.closest('[data-pf-secid]');
  if (row && !row.dataset.pfDead) {
    const b = BOND_BY_SECID.get(row.dataset.pfSecid);
    if (b) openDetail(bondDetail(b));
  }
}

PAGES.portfolio = {
  render(el) {
    if (PF.source == null) PF.source = pfDefaultSource();
    el.innerHTML = `<div class="page-wide">
      <div class="page-title-row">
        <div class="page-title">Портфель</div>
        <div class="page-title-spacer"></div>
        <div class="pf-sources" id="pfSources"></div>
      </div>
      <div id="pfBody"></div>
    </div>`;
    el.addEventListener('click', pfOnClick);
    // The screener can empty or refill the basket while this page is built;
    // re-render so "Из корзины (N)" and the basket view never go stale.
    BASKET.onChange(() => {
      if (PF.source === 'basket' || !BASKET.ids().length) pfRender();
      else document.getElementById('pfSources').innerHTML = pfSourceButtons();
    });
    pfRender();
  },
};
