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

// ── position model ──────────────────────────────────────────────────────
// { secid, weight, bond|null, stored|null }. `bond` is the live model row;
// `stored` is the allocation row a preset carries, kept only for the name
// of a position that has dropped out of the snapshot.
function pfPositions() {
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
        + (noData ? ` · ${noData} без котировки` : '') },
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
    </div>`;
  }).join('');

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
  </section>`;
}

// ── Трекер ставки ЦБ ────────────────────────────────────────────────────
// Solid line: meetings that actually decided a rate (cbr.meetings, rate_after).
// Dashed: the forward path implied by cbr.rate_scenarios.base_annual_bp --
// the pipeline's own base scenario, not a curve fitted here. If there is no
// scenario the dashed half is simply absent.
function pfCbrChart() {
  const cbr = DATA.cbr || {};
  const hist = (cbr.meetings || []).filter(m => m.is_past && m.rate_after != null)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map(m => ({ date: m.date, rate: m.rate_after }));
  if (hist.length < 2) return null;

  const sc = cbr.rate_scenarios || {};
  const future = (cbr.meetings || []).filter(m => !m.is_past).sort((a, b) => a.date.localeCompare(b.date));
  const last = hist[hist.length - 1];
  const fwd = [];
  if (sc.base_annual_bp != null && future.length) {
    future.forEach(m => {
      const yrs = (Date.parse(m.date) - Date.parse(last.date)) / (365.25 * 864e5);
      fwd.push({ date: m.date, rate: last.rate + sc.base_annual_bp / 100 * yrs });
    });
  }

  const pts = hist.concat(fwd);
  const W = 420, H = 96, PADX = 10, PADT = 14, PADB = 22;
  const t0 = Date.parse(pts[0].date), t1 = Date.parse(pts[pts.length - 1].date);
  const rates = pts.map(p => p.rate);
  const lo = Math.min.apply(null, rates), hi = Math.max.apply(null, rates);
  const pad = Math.max(0.4, (hi - lo) * 0.25);
  const x = d => PADX + (t1 === t0 ? 0 : (Date.parse(d) - t0) / (t1 - t0)) * (W - 2 * PADX);
  const y = v => PADT + (1 - (v - (lo - pad)) / ((hi + pad) - (lo - pad))) * (H - PADT - PADB);
  const line = ps => ps.map((p, i) => `${i ? 'L' : 'M'}${x(p.date).toFixed(1)},${y(p.rate).toFixed(1)}`).join(' ');

  const k = [`<path d="${line(hist)}" fill="none" stroke="#c67139" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"></path>`];
  if (fwd.length) {
    k.push(`<path d="${line([last].concat(fwd))}" fill="none" stroke="#7a8a5e" stroke-width="2.4"
      stroke-dasharray="5 5" stroke-linecap="round"></path>`);
    const f = fwd[fwd.length - 1];
    k.push(`<text x="${(W - PADX).toFixed(1)}" y="${(y(f.rate) + 16).toFixed(1)}" class="pf-cbr-fwd" text-anchor="end">база ЦБ ${fmtPct(f.rate, 1)}</text>`);
  }
  k.push(`<circle cx="${x(last.date).toFixed(1)}" cy="${y(last.rate).toFixed(1)}" r="4.5" fill="#c67139" stroke="#f0fae1" stroke-width="2"></circle>`);
  k.push(`<text x="${x(last.date).toFixed(1)}" y="${(y(last.rate) - 10).toFixed(1)}" class="pf-cbr-now" text-anchor="middle">${fmtDayMonth(last.date)}</text>`);
  k.push(`<text x="${PADX}" y="${H - 6}" class="pf-cbr-tick">${fmtDate(hist[0].date)}</text>`);

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="pf-cbr-svg">${k.join('')}</svg>`;
}

function pfCbrHtml() {
  const cbr = DATA.cbr || {};
  const chart = pfCbrChart();
  const diff = (cbr.recent_diffs || [])[0];
  const sc = cbr.rate_scenarios || {};
  let note;
  if (diff) {
    note = diff.note;
  } else if (sc.base_annual_bp != null) {
    note = `Базовый сценарий конвейера: ${sc.base_annual_bp > 0 ? '+' : '−'}${Math.abs(sc.base_annual_bp)} б.п. за год.`;
  } else {
    note = 'Разбор последнего заседания в выгрузке отсутствует.';
  }
  if (sc.is_fallback) note += ' Сценарий взят из запасных значений, а не из пресс-релиза ЦБ.';

  return `<section class="pf-cbr">
    <div class="pf-cbr-kicker">Трекер ставки ЦБ</div>
    <div class="pf-cbr-head">
      <div class="pf-cbr-rate">${fmtPct(cbr.current_rate)}</div>
      <div class="pf-cbr-next">${cbr.next_meeting ? `следующее заседание ${fmtDate(cbr.next_meeting)}` : 'дата следующего заседания не выгружена'}</div>
    </div>
    ${chart || '<div class="pf-cbr-empty">Истории решений ЦБ в выгрузке недостаточно для графика.</div>'}
    <div class="pf-cbr-note">${escapeHtml(note)}</div>
  </section>`;
}

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
    t: ev.length
      ? `${evInns.size} ${plural(evInns.size, ['эмитент', 'эмитента', 'эмитентов'])} с серьёзным событием или флагом детектора: ${evNames.slice(0, 3).join(', ')}${evNames.length > 3 ? ' и др' : ''}.`
      : 'Ни у одного эмитента нет серьёзного события за 30 дней и флагов детектора на сегодня.' });

  // 5. liquidity -- against the screener's own low-liquidity floor
  const thin = positions.filter(p => p.bond && p.bond.vol != null && p.bond.vol < LOW_LIQ_VOLUME_RUB);
  const noVol = positions.filter(p => !p.bond || p.bond.vol == null);
  out.push({ g: thin.length ? '!' : '✓', tone: thin.length ? 'warn' : 'ok',
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

function pfChecksHtml(positions) {
  const items = pfChecks(positions).map(c =>
    `<div class="pf-check"><div class="pf-check-g is-${c.tone}">${escapeHtml(c.g)}</div><div class="pf-check-t">${escapeHtml(c.t)}</div></div>`).join('');
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

  if (!positions.length) { body.innerHTML = pfEmptyHtml(); return; }

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
        ${pfCbrHtml()}
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
