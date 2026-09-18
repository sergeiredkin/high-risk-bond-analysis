// Разбор бумаги -- the detailOpen drawer of bond-lab-design-reference.html,
// opened from a map point or a screener row. Section order, radii and copy
// follow the reference; every number comes from dashboard_data.json.
//
// Where the reference's mock data has no real counterpart the section says
// so instead of showing a plausible number:
//   "IRR с выживаемостью"  -> доходность при выживании, from the pipeline's
//        own aey_risk_decomposition (rated bonds only).
//   "шесть опор"           -> the four pillars compute/pillars.py actually
//        scores (leverage, liquidity, refinancing, operations); the other
//        six_pillars fields are raw ratios, not scores.
//   "три агентства"        -> the bond's own rating_agencies (MOEX ЦКИ via
//        src/hybond/ratings.py), however many agencies rate it.
// An unrated bond has no model PD, so the credit-adjusted blocks are
// replaced by one line explaining why, never by an estimate.

const DET_INK = '#201e1d';
const DET_ACC = '#c67139';
const DET_ACC2 = '#7a8a5e';
const DET_GREY = '#8a8478';
const SURV_YEARS = 5;

function detCard(title, bodyHtml, extra) {
  return `<section class="det-card${extra && extra.cls ? ' ' + extra.cls : ''}">
    <div class="det-card-head"><div class="det-card-title">${escapeHtml(title)}</div>${extra && extra.aside ? `<div class="det-card-aside">${extra.aside}</div>` : ''}</div>
    ${bodyHtml}
  </section>`;
}

function detTile(kicker, value, cls) {
  return `<div class="det-tile${cls ? ' ' + cls : ''}"><div class="det-tile-k">${escapeHtml(kicker)}</div><div class="det-tile-v">${value}</div></div>`;
}

// ── "Из чего собран AEY" ────────────────────────────────────────────────
// Three bars, exactly the reference's waterfall, over the pipeline's own
// decomposition: EV = Rs − PD × (Rs − Rd).
function detWaterfall(d) {
  const W = 460, LX = 150, RG = 76, PW = W - LX - RG;
  const rs = d.return_if_survive_pct, rd = d.return_if_default_pct;
  const pd = d.p_default_pct / 100, ev = d.expected_value_pct;
  const loss = rs - ev;
  const max = Math.max(rs, ev, 1);
  const xs = v => Math.max(0, v) / max * PW;
  const rows = [
    ['Доходность при выживании', rs, DET_ACC, LX, xs(rs)],
    ['− потери от дефолта', -loss, DET_GREY, LX + xs(Math.max(0, ev)), xs(loss)],
    ['AEY с учётом дефолта', ev, DET_ACC2, LX, xs(ev)],
  ];
  const bars = rows.map(([label, v, fill, x, w], i) => {
    const y = i * 32 + 4;
    return `<rect x="${x.toFixed(1)}" y="${y}" width="${Math.max(1, w).toFixed(1)}" height="20" rx="7" fill="${fill}"></rect>
      <text x="${LX - 10}" y="${y + 14.5}" class="wf-label" text-anchor="end">${escapeHtml(label)}</text>
      <text x="${W}" y="${y + 14.5}" class="wf-value" text-anchor="end">${v < 0 ? '−' : ''}${fmtPct(Math.abs(v), 1)}</text>`;
  }).join('');
  return `<svg viewBox="0 0 ${W} 100" width="100%" class="det-svg">${bars}</svg>`;
}

// ── "Выживаемость по годам" ─────────────────────────────────────────────
// S(k) = (1 − PD)^k at the rating's annual PD from DATA.pd_by_rating. The
// assumption (flat PD, no rating migration) is printed under the chart --
// it is the pipeline's own model, not an extrapolation invented here.
function detSurvival(b) {
  const pd = b.pdRating;
  if (pd == null || !isFinite(pd)) return null;
  const n = Math.max(2, Math.min(SURV_YEARS, Math.ceil(b.m || SURV_YEARS)));
  const surv = [];
  let s = 1;
  for (let k = 0; k < n; k++) { s *= (1 - pd / 100); surv.push(s * 100); }
  const W = 460, H = 124;
  const lo = Math.min(90, Math.floor(Math.min.apply(null, surv) / 10) * 10);
  const x = i => 58 + (n === 1 ? 0.5 : i / (n - 1)) * (W - 78);
  const y = v => 16 + (1 - (v - lo) / (100 - lo)) * (H - 44);
  const k = [];
  for (let v = 100; v >= lo; v -= Math.max(2, Math.round((100 - lo) / 4))) {
    k.push(`<line x1="36" x2="${W - 8}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="${DET_INK}" stroke-opacity="0.07"></line>
      <text x="32" y="${(y(v) + 3.5).toFixed(1)}" class="det-tick" text-anchor="end">${fmtNum(v, 0)}%</text>`);
  }
  let d = `M${x(0).toFixed(1)},${y(surv[0]).toFixed(1)}`;
  for (let i = 1; i < n; i++) {
    const cx = (x(i - 1) + x(i)) / 2;
    d += ` C${cx.toFixed(1)},${y(surv[i - 1]).toFixed(1)} ${cx.toFixed(1)},${y(surv[i]).toFixed(1)} ${x(i).toFixed(1)},${y(surv[i]).toFixed(1)}`;
  }
  k.push(`<path d="${d} L${x(n - 1).toFixed(1)},${H - 24} L${x(0).toFixed(1)},${H - 24} Z" fill="${DET_ACC2}" fill-opacity="0.14"></path>`);
  k.push(`<path d="${d}" fill="none" stroke="${DET_ACC2}" stroke-width="2.6" stroke-linecap="round"></path>`);
  surv.forEach((v, i) => {
    k.push(`<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="3.6" fill="#f5ead8" stroke="${DET_ACC2}" stroke-width="2.2"></circle>
      <text x="${x(i).toFixed(1)}" y="${H - 8}" class="det-tick" text-anchor="middle">${i + 1} г.</text>
      <text x="${x(i).toFixed(1)}" y="${(y(v) - 9).toFixed(1)}" class="det-point" text-anchor="middle">${fmtNum(v, 1)}%</text>`);
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="det-svg">${k.join('')}</svg>`;
}

// ── sections ────────────────────────────────────────────────────────────
function detAeySection(b) {
  const d = b.decomposition;
  if (!d) {
    return detCard('Из чего собран AEY', `<div class="det-note">У бумаги нет рейтинга, поэтому модельной вероятности дефолта для неё нет и кредитная поправка не считается. YTM показан как есть: ${fmtPct(b.ytm, 2)}${b.aeyBase != null ? `, AEY до поправки на кредит — ${fmtPct(b.aeyBase, 2)}` : ''}. Пустое поле здесь означает отсутствие данных, а не ноль риска.</div>`);
  }
  const meta = DATA.pd_table_meta || {};
  const note = `Годовая вероятность дефолта ${fmtPct(b.pdRating, 2)} по рейтингу ${escapeHtml(b.rating || '')} при возврате ${fmtPct((meta.recovery_rate || 0) * 100, 1)} номинала даёт ${fmtPct(d.p_default_pct, 1)} шанс потерять ${fmtPct(Math.abs(d.return_if_default_pct), 1)} на горизонте бумаги. Именно это вычитается из доходности при выживании — сравнивать бумаги имеет смысл только после вычета.`;
  return detCard('Из чего собран AEY', detWaterfall(d) + `<div class="det-note">${note}</div>`);
}

function detSurvivalSection(b) {
  const svg = detSurvival(b);
  if (!svg) return '';
  const meta = DATA.pd_table_meta || {};
  return detCard('Выживаемость по годам', svg + `<div class="det-note">Постоянная годовая вероятность дефолта ${fmtPct(b.pdRating, 2)} из таблицы ${escapeHtml(meta.source || 'PD')} от ${fmtDate(meta.dated)}, без миграции рейтинга. Это допущение модели, а не прогноз по конкретному эмитенту.</div>`, {
    aside: `<span class="det-card-aside-v">${b.aey != null ? 'AEY ' + fmtPct(b.aey, 2) : ''}</span>`,
  });
}

function detPillarsSection(b) {
  const iss = b.issuer;
  const sp = iss && iss.six_pillars;
  if (!sp) {
    const why = b.passiveIssuer
      ? 'Эмитент есть в базе, но не стоит под активным мониторингом, поэтому отчётность по РСБУ для него не разбиралась.'
      : 'Эмитента нет в базе мониторинга, отчётность по РСБУ для него не разбиралась.';
    return detCard('Скоринг РСБУ', `<div class="det-note">${escapeHtml(why)} Балл не показан, потому что его не существует, а не потому что он низкий.</div>`);
  }
  const rows = PILLARS.map(([key, name, rawFmt]) => {
    const v = sp[key];
    if (v == null) return '';
    const s = v / 10;
    const cls = s >= 7 ? 'is-good' : s >= 4 ? 'is-mid' : 'is-bad';
    const raw = rawFmt(sp);
    return `<div class="det-pillar">
      <div class="det-pillar-head">
        <div class="det-pillar-name">${escapeHtml(name)}</div>
        <div class="det-pillar-raw">${raw ? escapeHtml(raw) : ''}</div>
        <div class="det-pillar-v ${cls}">${fmtNum(s, 1)}</div>
      </div>
      <div class="det-pillar-track"><div class="det-pillar-fill ${cls}" style="width:${s * 10}%"></div></div>
    </div>`;
  }).join('');
  const total = b.score;
  const cls = total == null ? '' : total >= 7 ? 'is-good' : total >= 4 ? 'is-mid' : 'is-bad';
  return detCard('Скоринг РСБУ', `<div class="det-pillars">${rows}</div>
    <div class="det-note">Правила зашиты в код и покрыты тестами: одинаковая отчётность всегда даёт одинаковый балл. Отраслевые пороги отличаются для банков, лизинга и девелопмента. Отчётность от ${escapeHtml(fmtDate(iss.pillars_date))}.</div>`, {
    aside: `<span class="det-card-aside-v ${cls}">${total == null ? DASH : fmtNum(total, 1) + ' / 10'}</span>`,
  });
}

// Every recorded change touching this bond (issuer-level changes touch all of
// the issuer's bonds), newest first: the day it was seen, and the agency's
// own action date.
function detRatingChangesHtml(b) {
  const list = b.ratingChanges || [];
  const meta = DATA.rating_changes || {};
  if (!list.length) {
    return meta.baseline_since
      ? `<div class="det-note">Изменений рейтинга с начала наблюдения (${escapeHtml(fmtDate(meta.baseline_since))}) не было.</div>`
      : '';
  }
  return `<div class="det-rchg-head">Изменения рейтинга</div><div class="det-rchgs">${list.map(c => `
    <div class="det-rchg${ratingChangeIsDown(c) ? ' is-down' : ' is-up'}">
      <div class="det-rchg-date">${escapeHtml(fmtDate(c.detected_on))}</div>
      <div class="det-rchg-text">${escapeHtml(ratingChangeText(c))}${c.level === 'issue' ? ' · выпуск' : ''}</div>
      <div class="det-rchg-mark">${escapeHtml(RATING_KIND_LABEL[c.kind] || c.kind)} · решение ${escapeHtml(fmtDate(c.rated_on))}${c.source === 'moex_baseline' ? ' · сверка с прежним' : c.source === 'manual' ? ' · ручная запись' : ''}</div>
    </div>`).join('')}</div>`;
}

function detRatingsSection(b) {
  const all = b.ratingAgencies || [];
  if (!all.length) {
    return detCard('Разрешение рейтингов', `${detRatingChangesHtml(b)}<div class="det-note">${b.rating
      ? `У бумаги рейтинг ${escapeHtml(b.rating)}, но агентских записей по нему в выгрузке нет — показывать нечего, кроме итогового значения.`
      : 'Ни одно агентство не присваивало рейтинг ни эмитенту, ни выпуску — по данным MOEX ЦКИ. Без рейтинга нет модельной PD, поэтому справедливый спред и AEY после потерь для бумаги не считаются.'}</div>`);
  }
  const rows = all.map(a => {
    const displayOnly = a.scale !== 'national';
    const mark = a.used ? 'принят'
      : a.status === 'withdrawn' ? 'отозван'
      : a.status === 'suspended' ? 'приостановлен'
      : displayOnly ? 'не в модели'
      : 'выше — не используется';
    const grade = a.status === 'withdrawn' || a.status === 'suspended' ? DASH : (a.grade || a.raw);
    return `<div class="det-rating${a.used ? ' is-win' : ''}">
      <div class="det-rating-ag">${escapeHtml(a.agency || DASH)}</div>
      <div class="det-rating-grade">${escapeHtml(grade || DASH)}</div>
      <div class="det-rating-date">${a.status === 'withdrawn' ? 'отозван' : 'присвоен'} ${escapeHtml(fmtDate(a.rated_on))}${a.level === 'issue' ? ' · выпуск' : ''}${a.source === 'legacy' ? ' · ручная запись' : ''}</div>
      <div class="det-rating-mark">${mark}</div>
    </div>`;
  }).join('');
  const notes = [];
  if (b.rating) {
    notes.push(`Итоговый рейтинг бумаги: ${escapeHtml(b.rating)}${b.ratingLevel === 'issue' ? ' (рейтинг выпуска)' : ''}.`);
  } else if (b.ratingWithdrawnOn) {
    notes.push(`Все рейтинги отозваны, последний — ${escapeHtml(fmtDate(b.ratingWithdrawnOn))}. Бумага считается без рейтинга.`);
  } else if (b.ratingSf) {
    notes.push(`Есть только рейтинг структурного финансирования ${escapeHtml(b.ratingSf)} — он показан, но в модель PD не идёт.`);
  }
  if (b.ratingDefaulted) notes.push('Рейтинг дефолтный (D): модельной PD для него нет, кредитная поправка и справедливый спред не считаются.');
  if (b.ratingSplit) notes.push('Агентства расходятся — в модель идёт самый низкий действующий рейтинг.');
  if (b.ratingStale) notes.push(`MOEX не подтверждал эти рейтинги с ${escapeHtml(fmtDate(b.ratingLastSeen))} — показан последний известный.`);
  return detCard('Разрешение рейтингов', `<div class="det-ratings">${rows}</div>
    ${detRatingChangesHtml(b)}
    <div class="det-note">Источник — MOEX ЦКИ. Рейтинг выпуска важнее рейтинга эмитента; из действующих рейтингов по национальной шкале берётся самый консервативный. ${notes.join(' ')}</div>`);
}

function detGapSection(b) {
  const band = gapBand(b);
  if (band == null) {
    return detCard('G-спред против кривой ОФЗ', `<div class="det-gap-row">
        <div><div class="det-gap-v">${b.gs == null ? DASH : fmtBpSigned(b.gs) + ' б.п.'}</div><div class="det-gap-k">рыночный спред</div></div>
      </div>
      <div class="det-note">Справедливый спред считается от рейтинга (PD × LGD). У бумаги рейтинга нет, поэтому разрыв не вычисляется — это не «спред справедливый», а «сравнивать не с чем».</div>`, { cls: 'det-gap det-gap-none' });
  }
  const cls = band === 'above' ? 'det-gap-up' : band === 'below' ? 'det-gap-down' : 'det-gap-fair';
  const note = band === 'above'
    ? `Рынок платит на ${fmtBpSigned(b.gap)} б.п. больше, чем требует кредитное качество по модели. Проверьте ленту событий: разрыв такого размера обычно означает либо неучтённую новость, либо реальную недооценку.`
    : band === 'below'
      ? `Спред на ${fmtNum(Math.abs(Math.round(b.gap)), 0)} б.п. ниже модельного. За тот же кредитный риск платят меньше рынка — держать можно, добирать смысла нет.`
      : `Спред в пределах модели (±${fmtNum(FAIR_BAND_BP, 0)} б.п.): бумага оценена справедливо, доход придёт от купона и сползания по кривой, а не от переоценки.`;
  return detCard('G-спред против кривой ОФЗ', `<div class="det-gap-row">
      <div><div class="det-gap-v">${fmtBpSigned(b.gs)} б.п.</div><div class="det-gap-k">рыночный спред</div></div>
      <div><div class="det-gap-v is-dim">${fmtBpSigned(b.fair)} б.п.</div><div class="det-gap-k">справедливый по модели</div></div>
      <div class="det-gap-spacer"></div>
      <div class="det-gap-right"><div class="det-gap-v">${fmtBpSigned(b.gap)} б.п.</div><div class="det-gap-k">разрыв</div></div>
    </div>
    <div class="det-note is-strong">${escapeHtml(note)}</div>`, { cls: `det-gap ${cls}` });
}

const DET_SEVERITY = {
  critical: ['Критическое', 'ev-hot'],
  high: ['Существенное', 'ev-hot'],
  medium: ['Заметное', 'ev-mid'],
  low: ['Фоновое', 'ev-calm'],
  noise: ['Рутина', 'ev-calm'],
};

function detEventsSection(b) {
  const iss = b.issuer || b.passiveIssuer;
  const events = ((iss && iss.events) || []).slice().sort((x, y) => String(y.occurred).localeCompare(String(x.occurred))).slice(0, 6);
  const flags = b.flagsToday || [];
  const flagHtml = flags.map(f => `<div class="det-event">
      <span class="det-event-cls ev-hot">детектор</span>
      <div class="det-event-body"><div class="det-event-text">${escapeHtml(f.detail || f.signal || '')}</div>
      <div class="det-event-meta">сегодня · ${escapeHtml(f.detector || '')}</div></div>
    </div>`).join('');
  if (!events.length && !flags.length) {
    return detCard('События по эмитенту', `<div class="det-note">Классификатор не нашёл событий по этому эмитенту. Отсутствие событий — это отсутствие событий, а не признак качества.</div>`);
  }
  const rows = events.map(e => {
    const [label, cls] = DET_SEVERITY[e.severity] || ['Событие', 'ev-calm'];
    return `<div class="det-event">
      <span class="det-event-cls ${cls}">${escapeHtml(label)}</span>
      <div class="det-event-body">
        <div class="det-event-text">${escapeHtml(e.summary || '')}</div>
        <div class="det-event-meta">${escapeHtml(fmtDate(e.occurred))} · ${escapeHtml(e.category || '')} · ${escapeHtml(e.source || '')}</div>
      </div>
    </div>`;
  }).join('');
  return detCard('События по эмитенту', `<div class="det-events">${flagHtml}${rows}</div>`);
}

// ── entry point ─────────────────────────────────────────────────────────
function bondDetail(b) {
  const picked = BASKET.has(b.secid);
  const subParts = [b.n];
  if (b.sectorLabel) subParts.push(b.sectorLabel);
  if (b.matDate) subParts.push(`погашение ${fmtDate(b.matDate)}`);
  if (b.offerDate) subParts.push(`оферта ${fmtDate(b.offerDate)}`);
  subParts.push(`оборот ${fmtRub(b.vol)} за сессию`);

  const tiles = `<div class="det-tiles">
    ${detTile('AEY', fmtPct(b.aey, 1), 'is-green')}
    ${detTile('YTM', fmtPct(b.ytm, 1))}
    ${detTile('При выживании', b.decomposition ? fmtPct(b.decomposition.return_if_survive_pct, 1) : DASH)}
  </div>`;

  const facts = `<div class="det-facts">
    <div><span>Дюрация</span><b>${b.dur == null ? DASH : fmtNum(b.dur, 2) + ' г.'}</b></div>
    <div><span>Купон</span><b>${b.coupon == null ? DASH : fmtPct(b.coupon, 2)}${b.couponType === 'floater' ? ' · плавающий' : ''}</b></div>
    <div><span>Цена</span><b>${b.price == null ? DASH : fmtNum(b.price, 2) + '%'}</b></div>
    <div><span>Сделок</span><b>${fmtNum(b.trades, 0)}</b></div>
  </div>`;

  const flags = b.flags.length
    ? `<div class="det-flags">${b.flags.map(f => `<span class="det-flag">${escapeHtml(f)}</span>`).join('')}</div>`
    : '';

  const priceWarn = b.yieldSource && b.yieldSource !== 'moex'
    ? `<div class="det-warn">Доходность посчитана не по обычной рыночной цене (${escapeHtml(b.yieldSource)}) — число показано, но сравнивать его с остальной вселенной напрямую нельзя.</div>`
    : '';

  const bodyHtml = [
    tiles, facts, flags, priceWarn,
    detAeySection(b),
    detSurvivalSection(b),
    detPillarsSection(b),
    detRatingsSection(b),
    detGapSection(b),
    detEventsSection(b),
    `<div class="det-actions">
      <button type="button" class="pill-outline" id="termDetailPick" data-detail-action="pick" data-secid="${escapeHtml(b.secid)}">${picked ? 'Убрать из корзины' : 'Добавить в корзину'}</button>
      <button type="button" class="pill-primary" data-detail-action="similar" data-secid="${escapeHtml(b.secid)}">Показать похожие на карте</button>
    </div>`,
  ].join('');

  return { title: b.t, sub: subParts.join(' · '), bodyHtml };
}
