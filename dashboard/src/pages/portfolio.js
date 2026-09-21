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
  replaceOpen: null,
};

const PF_REPLACED = (() => {
  const KEY = 'kl.pf.replaced';
  let map = {};
  try { map = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { map = {}; }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(map)); } catch (e) {} }
  return {
    get(key, id) { return map[key] && map[key][id] || null; },
    set(key, oldId, newId) { if (!key) return; map[key] = Object.assign({}, map[key], { [oldId]: newId }); save(); },
  };
})();

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
  return bonds.map(b => {
    const replacement = PF_REPLACED.get(pfRemovalKey(), b.isin);
    return {
      secid: replacement || b.isin, weight: b.weight,
      bond: replacement ? BOND_BY_SECID.get(replacement) || null : BOND_BY_SECID.get(b.isin) || null, stored: b,
    };
  });
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

function pfReplacementHtml(p, positions) {
  if (!p.bond || !YIELD_GUARD.reasons(p.bond).length) return '';
  if (PF.replaceOpen !== p.secid) {
    return `<div class="pf-row-replace"><button type="button" class="pf-row-replace-btn" data-pf-replace="${escapeHtml(p.secid)}">Заменить ▾</button></div>`;
  }
  const alts = YIELD_GUARD.alternatives(p.bond, positions.map(x => x.secid)).slice(0, 3);
  return `<div class="pf-replace-pop"><div class="pf-replace-pop-head">Безопаснее · ниже порога ${fmtPct(YIELD_GUARD.threshold, 0)} · другой эмитент</div>${alts.length ? alts.map(x => `<div class="pf-replace-option"><span>${escapeHtml(x.t)} · ${escapeHtml(x.sectorLabel || x.sector || 'без отрасли')}</span><span>${fmtPct(x.aey, 1)} · ${escapeHtml(x.rating || 'без рейтинга')}</span><button type="button" class="pf-replace-btn" data-pf-replace-pick="${escapeHtml(p.secid)}" data-pf-replace-to="${escapeHtml(x.secid)}">Заменить</button></div>`).join('') : '<div class="pf-replace-option">Подходящей альтернативы в текущем снимке нет.</div>'}</div>`;
}

function pfGuardControlsHtml() {
  if (PF.source !== 'basket') return '';
  return `<div class="pf-guard-inline"><div><b>Порог добавления</b><span>Настройка действует в этом браузере и помогает проверять новые позиции.</span></div><label><output>${fmtPct(YIELD_GUARD.threshold, 0)}</output><input class="yg-range" type="range" min="10" max="60" step="5" value="${YIELD_GUARD.threshold}" data-pf-guard-threshold></label><label class="yg-check"><input type="checkbox" data-pf-guard-stable ${YIELD_GUARD.requireStable ? 'checked' : ''}> стабильный G-спред</label></div>`;
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
      ${pfReplacementHtml(p, positions)}
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
    ${pfGuardControlsHtml()}
    <div class="pf-card-note">${escapeHtml(note)}</div>
    <div class="pf-bar">${bars}</div>
    <div class="pf-rows">${rows}</div>
    ${more}
    ${removedHtml}
  </section>`;
}

// ── Портфель через 3 года: AEY под сценариями ЦБ ────────────────────────
// The rate tracker that used to live here moved to «Ставка и ОФЗ» (captain,
// 2026-09-18): it duplicated that page's own «Траектория по сценариям»
// panel (rates.js), which draws the same published path across every CBR
// scenario, not just one. This replaces that slot with the follow-up the
// captain asked for next: a rate-scenario portfolio-impact section
// (scout plan: firstmate/data/portfolio-rate-plan-1789716252/report.md),
// approved 2026-09-18. Two blocks, both backed by DATA.rates_premium
// (scripts/export_dashboard_data.py's rates_premium_export -- the SAME
// formula «Премия к ОФЗ» on «Ставка и ОФЗ» already uses live):
//   A. one-time repricing tiles, one per CBR scenario, at today's holdings;
//   B. the same repricing evaluated at each of the next few years' own
//      published scenario level (aey_path) -- a sensitivity across years,
//      NOT a compounding value path: there is no cash-flow/roll-down model
//      for corporate bonds (unlike ofz_pipeline for OFZ), so this doesn't
//      assume the portfolio still holds today's exact bonds in year 2 or 3.
//      Paired with `rolloff` -- how much of the portfolio's own bond COUNT
//      actually survives that long -- so the chart can't be read as more
//      than it is.
const PF_SCEN_ORDER = ['base', 'bull', 'stress', 'risky'];
const PF_SCEN_LABEL = { base: 'Базовый', bull: 'Дезинфл.', stress: 'Проинфл.', risky: 'Рисковый' };
const PF_SCEN_COLOR = { base: '#c67139', bull: '#7a8a5e', stress: '#8c491a', risky: '#56633f' };

function pfRatePremium() {
  const cur = pfCurrent();
  if (!cur) return null;
  return ((DATA.rates_premium && DATA.rates_premium.portfolios) || []).find(p => p.id === cur.id) || null;
}

function pfRateTilesHtml(rp) {
  const tiles = PF_SCEN_ORDER.map(k => {
    const s = rp.by_scenario[k];
    if (!s || s.pf_aey_pct == null) {
      return `<div class="stat"><div class="stat-k">${escapeHtml(PF_SCEN_LABEL[k])}</div><div class="stat-v">${DASH}</div><div class="stat-note">нет данных по этому сценарию</div></div>`;
    }
    return `<div class="stat${k === 'base' ? ' is-green' : ''}">
      <div class="stat-k">${escapeHtml(PF_SCEN_LABEL[k])}</div>
      <div class="stat-v">${fmtPct(s.pf_aey_pct, 2)}</div>
      <div class="stat-note">AEY после потерь · дюр. ${rp.avg_mod_dur != null ? fmtNum(rp.avg_mod_dur, 2) + ' г.' : DASH}</div>
    </div>`;
  }).join('');
  return `<section class="pf-card">
    <div class="pf-card-title">Разовая переоценка по сценариям ЦБ</div>
    <div class="pf-card-note">Сегодняшний состав, репрайс на сдвиг ставки каждого сценария — та же формула, что «Премия к ОФЗ» на «Ставка и ОФЗ». Разовый эффект, не путь на несколько лет — график ниже.</div>
    <div class="stat-grid">${tiles}</div>
  </section>`;
}

function pfRatePathSvg(rp) {
  const years = rp.aey_path_years || [];
  if (years.length < 2) return null;
  const W = 440, H = 170, PX = 10, PT = 14, PB = 22, LBL = 96;
  const x = i => PX + i / (years.length - 1) * (W - 2 * PX - LBL);
  const vals = [];
  PF_SCEN_ORDER.forEach(k => years.forEach(yy => {
    const v = rp.aey_path[k] && rp.aey_path[k][String(yy)];
    if (v != null) vals.push(v);
  }));
  if (!vals.length) return null;
  const lo = Math.floor(Math.min.apply(null, vals) / 2) * 2 - 2, hi = Math.ceil(Math.max.apply(null, vals) / 2) * 2 + 2;
  const y = v => PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB);
  const parts = [];
  const ticks = [0, 1, 2, 3, 4].map(i => lo + (hi - lo) * i / 4);
  ticks.forEach(v => {
    const yy = y(v);
    parts.push(`<line x1="${PX}" x2="${(W - LBL).toFixed(1)}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="#201e1d" stroke-opacity=".10"></line>`);
    parts.push(`<text x="0" y="${(yy + 3).toFixed(1)}" font-size="9.5" fill="#201e1d" fill-opacity=".58">${fmtNum(v, 1)}%</text>`);
  });
  const lastYear = years[years.length - 1];
  const labelRows = [];
  PF_SCEN_ORDER.forEach(k => {
    const pts = years.map((yy, i) => {
      const v = rp.aey_path[k] && rp.aey_path[k][String(yy)];
      return v == null ? null : [i, v];
    }).filter(Boolean);
    if (pts.length < 2) return;
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
    parts.push(`<path d="${d}" fill="none" stroke="${PF_SCEN_COLOR[k]}" stroke-width="${k === 'base' ? 2.4 : 1.8}" stroke-linecap="round" stroke-linejoin="round"></path>`);
    parts.push(pts.map(p => `<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="2.6" fill="${PF_SCEN_COLOR[k]}"></circle>`).join(''));
    const lastV = rp.aey_path[k] && rp.aey_path[k][String(lastYear)];
    if (lastV != null) labelRows.push({ k, v: lastV });
  });
  labelRows.sort((a, b) => a.v - b.v);
  let prevY = -999;
  labelRows.forEach(({ k, v }) => {
    let yy = y(v) + 3;
    if (yy - prevY < 11) yy = prevY + 11;
    prevY = yy;
    parts.push(`<text x="${(W - LBL + 6).toFixed(1)}" y="${yy.toFixed(1)}" font-size="9.5" fill="${PF_SCEN_COLOR[k]}" font-weight="600">${escapeHtml(PF_SCEN_LABEL[k])} ${fmtNum(v, 1)}%</text>`);
  });
  years.forEach((yy, i) => parts.push(`<text x="${x(i).toFixed(1)}" y="${H - 6}" font-size="9.5" text-anchor="middle" fill="#201e1d" fill-opacity=".5">${yy}</text>`));
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="pf-cbr-svg" role="img" aria-label="AEY портфеля по годам и сценариям">${parts.join('')}</svg>`;
}

function pfRatePathHtml(rp) {
  const svg = pfRatePathSvg(rp);
  const ro = rp.rolloff || {};
  const years = rp.aey_path_years || [];
  const rolloffPct = ro.n_bonds ? Math.round((ro.within_1y + ro.within_1_3y) / ro.n_bonds * 100) : null;
  const note = svg
    ? `Каждая точка — переоценка сегодняшних ${ro.n_bonds != null ? fmtNum(ro.n_bonds, 0) : ''} бумаг под уровень ставки сценария в этом году (та же формула, что в тайлах выше, просто на разных годах). Это не прогноз стоимости портфеля и не сложный процент${rolloffPct != null ? ` — ${rolloffPct}% бумаг по счёту гасится или уходит в оферту до ${years[years.length - 1]} года, и доходность этих денег зависит от того, во что они переинвестируются, не от сегодняшней дюрации` : ''}.`
    : 'Сценариев ЦБ на несколько лет вперёд в выгрузке пока нет — появятся после hybond expectations.';
  return `<section class="pf-cbr">
    <div class="pf-cbr-kicker">AEY портфеля по годам и сценариям ЦБ</div>
    ${svg || '<div class="pf-cbr-empty">Ставки по годам в выгрузке нет.</div>'}
    <div class="pf-cbr-note">${escapeHtml(note)}</div>
  </section>`;
}

function pfRateSectionHtml() {
  const rp = pfRatePremium();
  if (!rp) {
    return `<section class="pf-card">
      <div class="pf-card-title">Разовая переоценка по сценариям ЦБ</div>
      <div class="pf-card-note">Считается только для пресетов конвейера — у корзины нет сохранённого состава, который экспортер может пересчитать по сценариям.</div>
    </section>`;
  }
  return pfRateTilesHtml(rp) + pfRatePathHtml(rp);
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

  // 4b. rating moves on record (src/hybond/ratings_pipeline.py change log),
  // same 30-day window as the screener's marker. Counted by issuer, like events.
  const moved = positions.filter(p => p.bond && p.bond.ratingMove === 'down');
  const movedNames = Array.from(new Set(moved.map(p => {
    const c = ratingChangesRecent(p.bond.ratingChanges, (DATA.universe_now || {}).snap_date).find(ratingChangeIsDown);
    return c ? `${p.bond.n} (${ratingChangeText(c)})` : p.bond.n;
  })));
  const defaulted = positions.filter(p => p.bond && p.bond.ratingDefaulted);
  out.push({ g: moved.length || defaulted.length ? '!' : '✓', tone: moved.length || defaulted.length ? 'warn' : 'ok',
    t: moved.length || defaulted.length
      ? `${defaulted.length ? `${defaulted.length} ${plural(defaulted.length, ['позиция', 'позиции', 'позиций'])} с дефолтным рейтингом. ` : ''}${moved.length ? `Рейтинг понижен, отозван или в дефолте за ${EVENT_LOOKBACK_DAYS} дней: ${movedNames.slice(0, 3).join('; ')}${movedNames.length > 3 ? ' и др' : ''}.` : ''}`
      : `За ${EVENT_LOOKBACK_DAYS} дней рейтинги позиций не понижались и не отзывались.` });

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
        ${pfRateSectionHtml()}
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

  const replace = e.target.closest('[data-pf-replace]');
  if (replace) { e.stopPropagation(); PF.replaceOpen = PF.replaceOpen === replace.dataset.pfReplace ? null : replace.dataset.pfReplace; pfRender(); return; }
  const replacePick = e.target.closest('[data-pf-replace-pick]');
  if (replacePick) {
    e.stopPropagation();
    const oldId = replacePick.dataset.pfReplacePick, newId = replacePick.dataset.pfReplaceTo;
    if (PF.source === 'basket') {
      if (BASKET.has(oldId)) BASKET.toggle(oldId);
      if (!BASKET.has(newId)) BASKET.toggle(newId);
    } else PF_REPLACED.set(pfRemovalKey(), oldId, newId);
    PF.replaceOpen = null;
    pfRender();
    return;
  }

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
    el.addEventListener('input', e => {
      if (e.target.matches('[data-pf-guard-threshold]')) YIELD_GUARD.setThreshold(e.target.value);
    });
    el.addEventListener('change', e => {
      if (e.target.matches('[data-pf-guard-stable]')) YIELD_GUARD.setRequireStable(e.target.checked);
    });
    YIELD_GUARD.onChange(() => pfRender());
    // The screener can empty or refill the basket while this page is built;
    // re-render so "Из корзины (N)" and the basket view never go stale.
    BASKET.onChange(() => {
      if (PF.source === 'basket' || !BASKET.ids().length) pfRender();
      else document.getElementById('pfSources').innerHTML = pfSourceButtons();
    });
    pfRender();
  },
};
