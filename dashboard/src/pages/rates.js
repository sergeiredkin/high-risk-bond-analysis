// Ставка и ОФЗ -- the fifth page (plan rev 2, approved by the captain
// 2026-09-17). Not in the design reference: it is assembled only from
// components the four reference pages already use, so it reads as one of
// them -- the Портфель skeleton (title pills, stat tiles, card + aside),
// Карта и скринер's split for the OFZ block, Данные's source chips, and the
// bond drawer's vocabulary for every publication.
//
// Sources (scripts/export_dashboard_data.py):
//   DATA.expectations   CBR forecasts, ОНЕГДКП scenario editions, macro
//                       surveys, decisions, discussion summaries, schedule,
//                       per-source page URL + last good fetch
//   DATA.ofz            OFZ snapshot; bonds[].returns are horizon returns
//                       priced off each issue's own cash flows
//   DATA.zcyc           the CBR zero-coupon curve (percent)
//   DATA.rates_premium  preset portfolios vs the nearest-duration OFZ, one
//                       AEY formula on both legs
//
// Every rate on this page is a percent. Scenario levels are the CBR's
// average key rate for a year exactly as published -- no path inside a year
// is drawn, and nothing is interpolated between publications.

const RT_ACC = '#c67139', RT_ACC2 = '#7a8a5e', RT_ACC6 = '#b2622d', RT_GREY = '#c0b6a5', RT_INK = '#201e1d';
const RT_FEED_SHOWN = 6;
const RT_SHIFT = { 1: 0.40, 2: 0.75, 3: 1.00 };  // ofz_carry.GRADUATED_SHIFT, the taper the returns used
const RT_MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
// page scenario key -> the ОНЕГДКП scenario it is (base comes from the forecast)
const RT_ONDKP_KEY = { base: 'base', bull: 'disinflation', stress: 'proinflation', risky: 'risk' };
const RT_SHORT = { base: 'Баз.', bull: 'Дез.', stress: 'Про.', risky: 'Риск.', analysts: 'Анал.' };

const RE = DATA.expectations || {};
const RT_SCENARIOS = RE.scenarios || [];
const RT_FORECASTS = RE.forecasts || [];
const RT_ONDKP = RE.ondkp || [];
const RT_SURVEYS = RE.surveys || [];
const RT_OFZ = DATA.ofz || {};

const RT = {
  scen: 'base',
  year: RE.horizon_year || (new Date()).getFullYear() + 1,
  h: 1,
  expanded: false,
  feed: [],
  ofzSort: null,  // null = the selected scenario's return
};

// ── small helpers ───────────────────────────────────────────────────────
function rtDay(iso) { return Date.parse(String(iso).slice(0, 10) + 'T00:00:00Z') / 864e5; }
function rtMid(r) { return r ? (r[0] + r[1]) / 2 : null; }
function rtRange(r, digits) {
  if (!r) return DASH;
  const d = digits == null ? 1 : digits;
  return r[0] === r[1] ? fmtPct(r[0], d) : `${fmtNum(r[0], d)}–${fmtNum(r[1], d)}%`;
}
function rtSigned(v, d) { return v == null || !isFinite(v) ? DASH : `${v > 0 ? '+' : v < 0 ? '−' : ''}${fmtNum(Math.abs(v), d == null ? 1 : d)}`; }
function rtMonth(period) {
  const m = /^(\d{4})-(\d{2})/.exec(period || '');
  return m ? `${RT_MONTHS[+m[2] - 1]} ${m[1]}` : DASH;
}
function rtScenario(key) { return RT_SCENARIOS.find(s => s.key === key) || null; }
function rtLatest(list) { return list.length ? list[list.length - 1] : null; }
function rtSource(key) { return (RE.sources || []).find(s => s.key === key) || {}; }
function rtStatusNote(s) { return s && s.status === 'draft' ? ', проект' : ''; }

// Survey periods carry only a month; the page knows the exact publication
// date of the latest one from the source record.
function rtSurveyDate(sv) {
  const latest = rtLatest(RT_SURVEYS);
  const pub = rtSource('survey').last_published;
  return sv === latest && pub ? pub : `${sv.period}-01`;
}
function rtSurveyStat(sv, year, stat) {
  const y = sv && sv.years && sv.years[String(year)];
  return y && y[stat] != null ? y[stat] : null;
}

// A scenario's level for a year: the forecast for base, the latest ОНЕГДКП
// version for the alternatives, the latest survey median for analysts.
function rtLevel(key, year) {
  if (key === 'analysts') {
    const m = rtSurveyStat(rtLatest(RT_SURVEYS), year, 'median');
    return m == null ? null : [m, m];
  }
  if (key === 'base') {
    const f = rtLatest(RT_FORECASTS);
    return (f && f.years[String(year)]) || null;
  }
  const e = rtLatest(RT_ONDKP);
  const sc = e && e.scenarios[RT_ONDKP_KEY[key]];
  return (sc && sc[String(year)]) || null;
}

// ── title pills + stat tiles ────────────────────────────────────────────
function rtPillsHtml() {
  return RT_SCENARIOS.map(s =>
    `<button type="button" class="pf-pill${RT.scen === s.key ? ' is-on' : ''}" data-rt-scen="${s.key}">${escapeHtml(s.label)}</button>`).join('');
}

function rtPremium() {
  const all = (DATA.rates_premium && DATA.rates_premium.portfolios) || [];
  const order = ['conservative', 'balanced', 'aggressive'];
  return all.slice().sort((a, b) => order.indexOf(a.preset_type) - order.indexOf(b.preset_type));
}

function rtStats() {
  const cur = rtScenario(RT.scen);
  const hy = RE.horizon_year;
  const lastDecision = (RE.decisions || [])[0];
  const next = (RE.schedule || []).find(m => m.date > (RE.as_of || ''));
  const sv = rtLatest(RT_SURVEYS), svPrev = RT_SURVEYS.length > 1 ? RT_SURVEYS[RT_SURVEYS.length - 2] : null;
  const svMed = rtSurveyStat(sv, hy, 'median'), svPrevMed = rtSurveyStat(svPrev, hy, 'median');
  const base = rtScenario('base');

  const decisionNote = lastDecision
    ? `${fmtDate(lastDecision.date)} — ${lastDecision.change_bp === 0 ? 'сохранить' : lastDecision.change_bp < 0 ? `снизить на ${-lastDecision.change_bp} б.п.` : `повысить на ${lastDecision.change_bp} б.п.`}`
    : 'решений в выгрузке нет';
  const nextNote = next ? ` · следующее ${fmtDate(next.date)}${next.anchor ? ', опорное' : ''}` : ' · дат заседаний следующего года ЦБ ещё не опубликовал';

  const tiles = [
    { k: 'Ключевая ставка', v: fmtPct(RE.current_rate, 2), note: decisionNote + nextNote },
  ];
  if (cur) {
    const src = cur.source === 'survey' ? `макроопрос ${fmtDate(rtSource('survey').last_published || cur.published)}`
      : cur.source === 'cbr_forecast' ? `прогноз ЦБ ${fmtDate(cur.published)}` : `ОНЕГДКП${rtStatusNote(cur)} ${fmtDate(cur.published)}`;
    tiles.push({ k: `«${cur.label}» · ${hy}`, v: cur.key === 'analysts' ? fmtPct(cur.range && cur.range[0], 1) : rtRange(cur.range), green: true,
      note: `${src} · ${cur.delta_bp == null ? DASH : fmtBpSigned(cur.delta_bp)} б.п. к текущей` });
  }
  if (RT.scen === 'analysts') {
    tiles.push({ k: `ЦБ · базовый · ${hy}`, v: rtRange(base && base.range), note: base ? `прогноз ${fmtDate(base.published)} · середина ${fmtPct(rtMid(base.range), 1)}` : 'прогноза в выгрузке нет' });
  } else {
    tiles.push({ k: `Аналитики · ${hy}`, v: fmtPct(svMed, 1),
      note: sv ? `медиана опроса ${fmtDate(rtSurveyDate(sv))}${svPrevMed != null ? ` · было ${fmtPct(svPrevMed, 1)}` : ''} · квартили ${fmtNum(rtSurveyStat(sv, hy, 'q1'), 1)}–${fmtNum(rtSurveyStat(sv, hy, 'q3'), 1)}%`
        : 'опросов в выгрузке нет' });
  }
  const pfs = rtPremium();
  const first = pfs[0];
  const p = first && first.by_scenario[RT.scen];
  if (!first) {
    tiles.push({ k: 'Премия к ОФЗ', v: DASH, note: 'активных пресетов портфеля в выгрузке нет' });
  } else if (!p || p.premium_pp == null) {
    tiles.push({ k: `Премия к ОФЗ · ${first.name}`, v: DASH,
      note: first.ofz ? 'по этому сценарию AEY портфеля не считается' : `нет ОФЗ-ПД в пределах ${fmtNum((DATA.rates_premium || {}).match_max_diff_years, 0)} лет дюрации портфеля` });
  } else {
    const others = pfs.slice(1).map(o => o.by_scenario[RT.scen] && o.by_scenario[RT.scen].premium_pp != null
      ? `${o.name} ${rtSigned(o.by_scenario[RT.scen].premium_pp, 1)}` : null).filter(Boolean);
    tiles.push({ k: `Премия к ОФЗ · ${first.name}`, v: `${rtSigned(p.premium_pp, 1)} п.п.`,
      note: `AEY после потерь ${fmtPct(p.pf_aey_pct, 1)} против ${first.ofz.short_name} ${fmtPct(p.ofz_aey_pct, 1)} (дюр. ${fmtNum(first.avg_mod_dur, 1)} и ${fmtNum(first.ofz.mod_dur, 1)} г.)`
        + (others.length ? ` · ${others.join(' · ')}` : '') });
  }
  return `<div class="stat-grid">${tiles.map(t => `<div class="stat${t.green ? ' is-green' : ''}">
      <div class="stat-k">${escapeHtml(t.k)}</div><div class="stat-v">${escapeHtml(t.v)}</div><div class="stat-note">${escapeHtml(t.note)}</div>
    </div>`).join('')}</div>`;
}

// ── «Как менялись ожидания» ─────────────────────────────────────────────
// CBR baseline: one range per anchor forecast that covers the year, a line
// through the midpoints (the pfCbrChart solid idiom). Analysts: the survey
// median dashed with the interquartile band (the «Выживаемость» fill idiom).
// The selected ОНЕГДКП alternative: its range at each edition's date.
function rtExpChart(year, alt) {
  const W = 560, H = 220, PL = 34, PR = 104, PT = 14, PB = 24;
  const cbr = RT_FORECASTS.map(f => ({ date: f.published, r: f.years[String(year)] || null, f })).filter(v => v.r);
  const ana = RT_SURVEYS.map(sv => ({ date: rtSurveyDate(sv), m: rtSurveyStat(sv, year, 'median'),
    q1: rtSurveyStat(sv, year, 'q1'), q3: rtSurveyStat(sv, year, 'q3') })).filter(v => v.m != null);
  const alts = alt ? RT_ONDKP.map(e => ({ date: e.published, r: (e.scenarios[RT_ONDKP_KEY[alt]] || {})[String(year)] || null }))
    .filter(v => v.r) : [];
  const dates = [].concat(cbr.map(v => v.date), ana.map(v => v.date), alts.map(v => v.date));
  if (!dates.length) return null;
  const t0 = Math.min.apply(null, dates.map(rtDay)) - 20, t1 = Math.max(rtDay(RE.as_of || dates[0]), Math.max.apply(null, dates.map(rtDay))) + 20;
  const x = iso => PL + (rtDay(iso) - t0) / (t1 - t0) * (W - PL - PR);
  const vals = [].concat(...cbr.map(v => v.r), ...ana.map(v => [v.q1 == null ? v.m : v.q1, v.q3 == null ? v.m : v.q3]), ...alts.map(v => v.r));
  const lo = Math.floor(Math.min.apply(null, vals) - 0.5), hi = Math.ceil(Math.max.apply(null, vals) + 0.5);
  const y = v => PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB);
  const k = [];
  const step = hi - lo > 8 ? 2 : 1;
  for (let v = lo; v <= hi; v += step) {
    k.push(`<line x1="${PL}" x2="${W - PR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="${RT_INK}" stroke-opacity="0.07"></line>
      <text x="${PL - 6}" y="${(y(v) + 3.5).toFixed(1)}" class="pf-cbr-tick" text-anchor="end">${v}%</text>`);
  }
  // half-year ticks inside the window
  const y0 = new Date(t0 * 864e5).getUTCFullYear(), y1 = new Date(t1 * 864e5).getUTCFullYear();
  for (let yy = y0; yy <= y1; yy++) {
    ['01', '07'].forEach(mm => {
      const d = `${yy}-${mm}-01`;
      if (rtDay(d) > t0 && rtDay(d) < t1) k.push(`<text x="${x(d).toFixed(1)}" y="${H - 6}" class="pf-cbr-tick" text-anchor="middle">${mm === '01' ? 'янв' : 'июл'} ${yy}</text>`);
    });
  }
  const band = ana.filter(v => v.q1 != null && v.q3 != null);
  if (band.length > 1) {
    k.push(`<path d="M${band.map(v => `${x(v.date).toFixed(1)},${y(v.q3).toFixed(1)}`).join(' L')} L${band.slice().reverse().map(v => `${x(v.date).toFixed(1)},${y(v.q1).toFixed(1)}`).join(' L')} Z" fill="${RT_ACC2}" fill-opacity="0.14"></path>`);
  }
  if (ana.length > 1) k.push(`<path d="M${ana.map(v => `${x(v.date).toFixed(1)},${y(v.m).toFixed(1)}`).join(' L')}" fill="none" stroke="${RT_ACC2}" stroke-width="2.4" stroke-dasharray="5 5" stroke-linecap="round"></path>`);
  ana.forEach(v => k.push(`<circle cx="${x(v.date).toFixed(1)}" cy="${y(v.m).toFixed(1)}" r="2.6" fill="${RT_ACC2}"></circle>`));
  alts.forEach(v => k.push(`<rect x="${(x(v.date) - 5).toFixed(1)}" y="${y(v.r[1]).toFixed(1)}" width="10" height="${Math.max(3, y(v.r[0]) - y(v.r[1])).toFixed(1)}" rx="5" fill="${RT_ACC6}" fill-opacity="0.35"></rect>`));
  cbr.forEach(v => k.push(`<rect x="${(x(v.date) - 3.5).toFixed(1)}" y="${y(v.r[1]).toFixed(1)}" width="7" height="${Math.max(2, y(v.r[0]) - y(v.r[1])).toFixed(1)}" rx="3.5" fill="${RT_ACC}" fill-opacity="0.3"></rect>`));
  if (cbr.length > 1) k.push(`<path d="M${cbr.map(v => `${x(v.date).toFixed(1)},${y(rtMid(v.r)).toFixed(1)}`).join(' L')}" fill="none" stroke="${RT_ACC}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"></path>`);
  cbr.forEach((v, i) => k.push(`<circle cx="${x(v.date).toFixed(1)}" cy="${y(rtMid(v.r)).toFixed(1)}" r="${i === cbr.length - 1 ? 4.5 : 3.2}" fill="${RT_ACC}" stroke="#f5ead8" stroke-width="${i === cbr.length - 1 ? 2 : 1.2}"></circle>`));

  const labels = [];
  const lc = rtLatest(cbr), la = rtLatest(ana), lx = rtLatest(alts);
  if (lc) labels.push({ yy: y(rtMid(lc.r)), cls: 'pf-cbr-now', t: `ЦБ ${rtRange(lc.r)}` });
  if (la) labels.push({ yy: y(la.m), cls: 'pf-cbr-fwd', t: `аналитики ${fmtPct(la.m, 1)}` });
  if (lx) labels.push({ yy: y(rtMid(lx.r)), cls: 'pf-cbr-now', t: `${RT_SHORT[alt]} ${rtRange(lx.r)}` });
  labels.sort((a, b) => a.yy - b.yy);
  for (let i = 1; i < labels.length; i++) if (labels[i].yy - labels[i - 1].yy < 13) labels[i].yy = labels[i - 1].yy + 13;
  labels.forEach(l => k.push(`<text x="${W - PR + 8}" y="${(l.yy + 3.5).toFixed(1)}" class="${l.cls}">${escapeHtml(l.t)}</text>`));
  return { svg: `<svg viewBox="0 0 ${W} ${H}" width="100%" class="pf-cbr-svg" role="img" aria-label="Ожидания средней ключевой ставки на ${year} год">${k.join('')}</svg>`, cbr, ana, alts };
}

function rtYears() {
  const hy = RE.horizon_year || (new Date()).getFullYear() + 1;
  return [hy - 1, hy, hy + 1];
}

function rtExpHtml() {
  const alt = RT_ONDKP_KEY[RT.scen] && RT.scen !== 'base' ? RT.scen : null;
  const c = rtExpChart(RT.year, alt);
  const altLabel = alt ? (rtScenario(alt) || {}).label : null;
  const note = c
    ? `ЦБ даёт диапазон средней ставки на год на опорных заседаниях (${c.cbr.length} ${plural(c.cbr.length, ['прогноз', 'прогноза', 'прогнозов'])} с оценкой ${RT.year} г.), аналитики — медиану и квартили макроопроса (${c.ana.length} ${plural(c.ana.length, ['опрос', 'опроса', 'опросов'])}).${altLabel ? ` Сценарий «${altLabel}» — из ОНЕГДКП, ${c.alts.length} ${plural(c.alts.length, ['редакция', 'редакции', 'редакций'])}.` : ''}`
    : '';
  const legend = [[RT_ACC, 'прогноз ЦБ, базовый: диапазон и середина'], [RT_ACC2, 'аналитики: медиана и квартили']]
    .concat(altLabel ? [[RT_ACC6, `сценарий «${altLabel}»`]] : [])
    .map(([cl, t]) => `<div class="map-legend-item"><span class="map-legend-dot" style="background:${cl}"></span><span>${escapeHtml(t)}</span></div>`).join('');
  return `<div class="term-card-head">
      <div class="pf-card-title">Как менялись ожидания: средняя ставка в ${RT.year} г.</div>
      <div class="page-title-spacer"></div>
      <div class="seg-group">${rtYears().map(yy => `<button type="button" class="seg-btn${RT.year === yy ? ' is-on' : ''}" data-rt-year="${yy}">${yy}</button>`).join('')}</div>
    </div>
    <div class="pf-card-note">${escapeHtml(note)}</div>
    ${c ? c.svg : '<div class="pf-cbr-empty">Прогнозов на этот год в выгрузке нет.</div>'}
    <div class="map-legend">${legend}</div>
    <div class="pf-rows" id="rtFeed">${rtFeedHtml()}</div>
    <button type="button" class="pf-more" id="rtMore">${RT.expanded ? 'Свернуть список' : `Показать все ${fmtNum(RT.feed.length, 0)}`}</button>`;
}

// ── publication feed ─────────────────────────────────────────────────────
// Every publication that says something about the selected year, newest
// first; decisions and summaries are listed regardless of year.
function rtFeedItems() {
  const y = String(RT.year), out = [];
  RT_FORECASTS.forEach((f, i) => {
    const r = f.years[y];
    if (!r) return;
    const prev = RT_FORECASTS.slice(0, i).reverse().find(p => p.years[y]);
    out.push({ kind: 'forecast', date: f.published, color: RT_ACC, t: `Прогноз ЦБ · ${fmtDate(f.published)}`,
      sub: `опорное заседание · средняя ставка ${y} г.`, v: rtRange(r),
      d: prev ? `${fmtBpSigned((rtMid(r) - rtMid(prev.years[y])) * 100)} б.п. к прошлому` : 'первый прогноз на год', ref: f });
  });
  RT_SURVEYS.forEach((sv, i) => {
    const m = rtSurveyStat(sv, y, 'median');
    if (m == null) return;
    const prev = RT_SURVEYS.slice(0, i).reverse().find(p => rtSurveyStat(p, y, 'median') != null);
    out.push({ kind: 'survey', date: rtSurveyDate(sv), color: RT_ACC2, t: `Опрос аналитиков · ${rtMonth(sv.period)}`,
      sub: `медиана · квартили ${fmtNum(rtSurveyStat(sv, y, 'q1'), 1)}–${fmtNum(rtSurveyStat(sv, y, 'q3'), 1)}% · ответов ${fmtNum(rtSurveyStat(sv, y, 'n'), 0)}`,
      v: fmtPct(m, 1), d: prev ? `${fmtBpSigned((m - rtSurveyStat(prev, y, 'median')) * 100)} б.п. к прошлому` : 'первый опрос на год', ref: sv });
  });
  RT_ONDKP.forEach(e => {
    const names = ['disinflation', 'base', 'proinflation', 'risk'];
    const rs = names.map(n => (e.scenarios[n] || {})[y]);
    if (!rs.some(Boolean)) return;
    const short = { disinflation: 'Дез.', base: 'Баз.', proinflation: 'Про.', risk: 'Риск.' };
    out.push({ kind: 'ondkp', date: e.published, color: RT_ACC6,
      t: `Сценарии ЦБ · ОНЕГДКП ${e.edition.replace('on_', '').replace('_', '–')}${e.status === 'draft' ? ' · проект' : ''}`,
      sub: names.map((n, i) => `${short[n]} ${rs[i] ? `${fmtNum(rs[i][0], 1)}–${fmtNum(rs[i][1], 1)}` : DASH}`).join(' · '),
      v: '4 сценария', d: fmtDate(e.published), ref: e });
  });
  (RE.decisions || []).forEach(dc => {
    out.push({ kind: 'decision', date: dc.date, color: '#82796a', t: `Решение ЦБ · ${fmtDate(dc.date)}`,
      sub: dc.signal ? `сигнал: ${dc.signal.replace(/^Банк России /, '')}` : (dc.title || ''),
      v: fmtPct(dc.rate, 2), d: dc.change_bp == null ? DASH : dc.change_bp === 0 ? 'без изменений' : `${fmtBpSigned(dc.change_bp)} б.п.`, ref: dc });
  });
  (RE.summaries || []).forEach(sm => {
    out.push({ kind: 'summary', date: sm.published || sm.meeting_date, color: '#82796a', t: `Резюме обсуждения · ${fmtDate(sm.published)}`,
      sub: sm.options ? `по заседанию ${fmtDate(sm.meeting_date)}: ${sm.options}` : `по заседанию ${fmtDate(sm.meeting_date)}`,
      v: sm.options ? `${sm.options.split(';').length} ${plural(sm.options.split(';').length, ['вариант', 'варианта', 'вариантов'])}` : DASH,
      d: `решение ${fmtDate(sm.meeting_date)}`, ref: sm });
  });
  return out.sort((a, b) => String(b.date).localeCompare(String(a.date)));
}

function rtFeedHtml() {
  RT.feed = rtFeedItems();
  if (!RT.feed.length) return '<div class="pf-cbr-empty">Публикаций ЦБ и опросов в выгрузке нет — запустите «hybond expectations --backfill».</div>';
  const shown = RT.expanded ? RT.feed : RT.feed.slice(0, RT_FEED_SHOWN);
  return shown.map((f, i) => `<div class="pf-row" data-rt-feed="${i}">
      <div class="pf-row-swatch" style="background:${f.color}"></div>
      <div class="pf-row-main"><div class="pf-row-t">${escapeHtml(f.t)}</div><div class="pf-row-sub">${escapeHtml(f.sub)}</div></div>
      <div class="pf-row-right"><div class="pf-row-w">${escapeHtml(f.v)}</div><div class="pf-row-aey">${escapeHtml(f.d)}</div></div>
    </div>`).join('');
}

// ── Траектория по сценариям (the Портфель tracker panel) ─────────────────
// Solid: the key rate as decided (daily series, collapsed to its steps).
// Dashed: the rest of this year as the latest forecast's footnote states,
// then each year's published average for every scenario -- a step per
// year, never a slope.
function rtPathSvg() {
  const years = [0, 1, 2, 3].map(i => (RE.horizon_year || 2027) + i - 1);
  // history from the start of last year: enough to see the path into today
  const from = `${years[0] - 1}-01-01`;
  const all = RE.key_rate_steps || [];
  const before = all.filter(s => s[0] < from).pop();
  const steps = (before ? [[from, before[1]]] : []).concat(all.filter(s => s[0] >= from));
  const f = rtLatest(RT_FORECASTS);
  if (steps.length < 1) return null;
  const W = 420, H = 150, PX = 10, PT = 16, PB = 22, LBL = 66;
  const start = steps[0][0], end = `${years[years.length - 1]}-12-31`;
  const t0 = rtDay(start), t1 = rtDay(end);
  const x = iso => PX + (rtDay(iso) - t0) / (t1 - t0) * (W - 2 * PX - LBL);
  const allVals = steps.map(s => s[1]);
  RT_SCENARIOS.forEach(s => years.forEach(yy => { const l = rtLevel(s.key, yy); if (l) allVals.push(rtMid(l)); }));
  const lo = Math.floor(Math.min.apply(null, allVals) - 1), hi = Math.ceil(Math.max.apply(null, allVals) + 1);
  const y = v => PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB);
  const asOf = RE.current_rate_date || RE.as_of;
  let d = '';
  steps.forEach((s, i) => {
    d += i ? ` L${x(s[0]).toFixed(1)},${y(steps[i - 1][1]).toFixed(1)} L${x(s[0]).toFixed(1)},${y(s[1]).toFixed(1)}` : `M${x(s[0]).toFixed(1)},${y(s[1]).toFixed(1)}`;
  });
  if (asOf) d += ` L${x(asOf).toFixed(1)},${y(RE.current_rate).toFixed(1)}`;
  const k = [`<path d="${d}" fill="none" stroke="${RT_ACC}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"></path>`];

  const thisYear = years[0];
  const rest = f && f.rest && f.rest.year === thisYear ? f.rest : null;
  const path = key => {
    const pts = [];
    if (asOf && rest) pts.push([asOf, RE.current_rate], [asOf, (rest.lo + rest.hi) / 2], [`${thisYear + 1}-01-01`, (rest.lo + rest.hi) / 2]);
    years.slice(1).forEach(yy => {
      const l = rtLevel(key, yy);
      if (!l) return;
      if (pts.length) pts.push([`${yy}-01-01`, pts[pts.length - 1][1]]);
      pts.push([`${yy}-01-01`, rtMid(l)], [`${yy}-12-31`, rtMid(l)]);
    });
    return pts.length > 1 ? 'M' + pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' L') : null;
  };
  // the other scenarios: their year levels only, faint, so the selected path reads
  RT_SCENARIOS.filter(s => s.key !== RT.scen).forEach(s => years.slice(1).forEach(yy => {
    const l = rtLevel(s.key, yy);
    if (l) k.push(`<line x1="${x(`${yy}-01-01`).toFixed(1)}" x2="${x(`${yy}-12-31`).toFixed(1)}" y1="${y(rtMid(l)).toFixed(1)}" y2="${y(rtMid(l)).toFixed(1)}" stroke="${RT_ACC2}" stroke-opacity="0.28" stroke-width="1.6" stroke-dasharray="5 5"></line>`);
  }));
  const sel = path(RT.scen);
  if (sel) k.push(`<path d="${sel}" fill="none" stroke="${RT_ACC2}" stroke-width="2.4" stroke-dasharray="5 5" stroke-linecap="round"></path>`);
  const lastYear = years[years.length - 1];
  const labels = RT_SCENARIOS.map(s => ({ s, v: rtMid(rtLevel(s.key, lastYear)) })).filter(l => l.v != null).sort((a, b) => a.v - b.v).reverse();
  let lastY = -1;
  labels.forEach(l => {
    let yy = y(l.v) + 3.5;
    if (lastY > 0 && yy - lastY < 11) yy = lastY + 11;
    lastY = yy;
    k.push(`<text x="${W - PX}" y="${yy.toFixed(1)}" class="pf-cbr-fwd" text-anchor="end"${l.s.key === RT.scen ? '' : ' fill-opacity="0.45"'}>${escapeHtml(RT_SHORT[l.s.key])} ${fmtNum(l.v, 1)}</text>`);
  });
  if (asOf) {
    k.push(`<circle cx="${x(asOf).toFixed(1)}" cy="${y(RE.current_rate).toFixed(1)}" r="4.5" fill="${RT_ACC}" stroke="#f0fae1" stroke-width="2"></circle>`);
    k.push(`<text x="${x(asOf).toFixed(1)}" y="${(y(RE.current_rate) - 10).toFixed(1)}" class="pf-cbr-now" text-anchor="middle">${fmtDayMonth(asOf)}</text>`);
  }
  [years[0] - 1].concat(years).forEach(yy => k.push(`<text x="${x(`${yy}-07-01`).toFixed(1)}" y="${H - 5}" class="pf-cbr-tick" text-anchor="middle">${yy}</text>`));
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="pf-cbr-svg" role="img" aria-label="Ключевая ставка и траектории сценариев">${k.join('')}</svg>`;
}

function rtPathHtml() {
  const cur = rtScenario(RT.scen);
  const f = rtLatest(RT_FORECASTS);
  const e = rtLatest(RT_ONDKP);
  const next = (RE.schedule || []).find(m => m.date > (RE.as_of || ''));
  const svg = rtPathSvg();
  const years = [0, 1, 2].map(i => (RE.horizon_year || 2027) + i);
  const levels = years.map(yy => { const l = rtLevel(RT.scen, yy); return l ? `${yy} — ${RT.scen === 'analysts' ? fmtPct(l[0], 1) : rtRange(l)}` : null; }).filter(Boolean);
  const rest = f && f.rest ? `остаток ${f.rest.year} г. — ${rtRange([f.rest.lo, f.rest.hi])} (сноска прогноза ${fmtDate(f.published)})` : null;
  const note = cur
    ? `«${cur.label}»: ${[rest, levels.join(', ')].filter(Boolean).join('; ')}. Уровни — средние за год, как их публикует ${RT.scen === 'analysts' ? 'макроопрос' : 'ЦБ'}; путь внутри года не выдумываем.`
    : 'Сценариев ЦБ в выгрузке нет.';
  const kicker = ['Траектория по сценариям', e ? `ОНЕГДКП${e.status === 'draft' ? ' (проект)' : ''} ${fmtDate(e.published)}` : null,
    f ? `прогноз ${fmtDate(f.published)}` : null].filter(Boolean).join(' · ');
  return `<section class="pf-cbr">
    <div class="pf-cbr-kicker">${escapeHtml(kicker)}</div>
    <div class="pf-cbr-head">
      <div class="pf-cbr-rate">${fmtPct(RE.current_rate)}</div>
      <div class="pf-cbr-next">${next ? `следующее заседание ${fmtDate(next.date)}` : 'календарь следующего года ЦБ ещё не опубликовал'}</div>
    </div>
    ${svg || '<div class="pf-cbr-empty">Ставки по дням в выгрузке нет.</div>'}
    <div class="pf-cbr-note">${escapeHtml(note)}</div>
  </section>`;
}

// ── Что сказал ЦБ (the Проверка портфеля list) ───────────────────────────
function rtLink(url, label) {
  return url ? ` <a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)} ↗</a>` : '';
}

function rtSaidHtml() {
  const dc = (RE.decisions || [])[0];
  const items = [];
  if (dc) {
    const what = dc.change_bp === 0 ? `сохранить ключевую ставку на уровне ${fmtPct(dc.rate, 2)}`
      : `${dc.change_bp < 0 ? 'снизить' : 'повысить'} ключевую ставку на ${Math.abs(dc.change_bp)} б.п., до ${fmtPct(dc.rate, 2)}`;
    items.push({ g: 'i', tone: 'neutral', html: `Решение ${fmtDate(dc.date)}: ${escapeHtml(what)}.${rtLink(dc.url, 'Пресс-релиз')}` });
    if (dc.signal) items.push({ g: 'i', tone: 'neutral', html: `Сигнал: «${escapeHtml(dc.signal)}»${rtLink(dc.statement_url, 'Заявление Председателя')}` });
  }
  const sm = (RE.summaries || [])[0];
  const decided = (RE.schedule || []).filter(m => m.date <= (RE.as_of || '')).pop();
  if (decided && (!sm || sm.meeting_date < decided.date) && decided.resume_date) {
    items.push({ g: 'i', tone: 'neutral', html: `Резюме обсуждения по ${fmtDate(decided.date)} выйдет ${fmtDate(decided.resume_date)} по календарю ЦБ.` });
  }
  if (sm) {
    items.push({ g: 'i', tone: 'neutral', html: `По ${fmtDate(sm.meeting_date)} обсуждали: ${escapeHtml(sm.options || 'варианты в резюме не перечислены')}.`
      + (sm.positions ? ` ${escapeHtml(sm.positions)}` : '') + rtLink(sm.url, 'Резюме') });
  }
  const hy = RE.horizon_year;
  const svMed = rtSurveyStat(rtLatest(RT_SURVEYS), hy, 'median');
  const base = rtScenario('base');
  const baseMid = base ? rtMid(base.range) : null;
  if (svMed != null && baseMid != null) {
    const above = svMed > baseMid;
    items.push({ g: above ? '!' : '✓', tone: above ? 'warn' : 'ok',
      html: `Аналитики (${fmtDate(rtSource('survey').last_published)}) ждут в ${hy} г. ${fmtPct(svMed, 1)} — ${above ? 'выше' : 'не выше'} середины базового диапазона ЦБ (${fmtPct(baseMid, 1)}).${rtLink(rtSource('survey').page_url, 'Опрос')}` });
  }
  const failing = (RE.sources || []).filter(s => s.last_error);
  failing.forEach(s => items.push({ g: '!', tone: 'warn',
    html: `${escapeHtml(s.name)}: последняя загрузка ${fmtDate(s.last_error_at)} не удалась — показаны данные от ${fmtDate(s.last_ok)}.` }));
  if (!items.length) items.push({ g: 'i', tone: 'neutral', html: 'Документов заседаний в выгрузке нет.' });
  const kicker = dc ? `Что сказал ЦБ · ${fmtDayMonth(dc.date)} ${dc.date.slice(0, 4)}` : 'Что сказал ЦБ';
  return `<section class="pf-checks">
    <div class="pf-checks-kicker">${escapeHtml(kicker)}</div>
    ${items.map(c => `<div class="pf-check"><div class="pf-check-g is-${c.tone}">${escapeHtml(c.g)}</div><div class="pf-check-t">${c.html}</div></div>`).join('')}
  </section>`;
}

// ── OFZ: curve (map idiom) + screener ────────────────────────────────────
function rtOfzReturn(bond, key, h) {
  const list = bond.returns && bond.returns[key];
  return list ? list.find(r => r.horizon_years === h) || null : null;
}

function rtOfzBonds() {
  return (RT_OFZ.bonds || []).filter(b => b.benchmark && b.returns);
}

// A nominal fixed-schedule issue priced on its own cash flows yet far from
// the CBR zero curve: the schedule or the price is wrong (ofz_pipeline
// MAX_CURVE_GAP_BP). Linkers sit below the nominal curve by design and are
// not "off curve" in this sense.
function rtOffCurve(b) {
  return (b.instrument_type === 'ПД' || b.instrument_type === 'АД') && b.yield_source === 'computed'
    && b.curve_gap_bp != null && RT_OFZ.max_curve_gap_bp != null && Math.abs(b.curve_gap_bp) > RT_OFZ.max_curve_gap_bp;
}

function rtCurveSvg() {
  const bonds = (RT_OFZ.bonds || []).filter(b => b.ytm_pct != null && b.ytm_pct > 0 && b.mod_dur != null
    && (b.tenor_yrs || 0) >= 0.5 && !rtOffCurve(b));
  if (!bonds.length) return null;
  const zc = (DATA.zcyc && DATA.zcyc.points) || [];
  const DX = Math.min(12, Math.max(4, Math.ceil(Math.max.apply(null, bonds.map(b => b.mod_dur)) + 0.5)));
  const vals = bonds.map(b => b.ytm_pct).concat(zc.filter(p => p[0] <= DX).map(p => p[1]));
  const shift = RT.scen && RT_OFZ.scenario_deltas_bp ? RT_OFZ.scenario_deltas_bp[RT.scen] : null;
  const dy = shift == null ? null : shift / 100 * RT_SHIFT[RT.h];
  if (dy != null) zc.forEach(p => { if (p[0] <= DX) vals.push(p[1] + dy); });
  const Y0 = Math.floor(Math.min.apply(null, vals) - 1), Y1 = Math.ceil(Math.max.apply(null, vals) + 1);
  const W = 600, H = 300, PL = 40, PR = 16, PT = 16, PB = 28;
  const x = v => PL + v / DX * (W - PL - PR), y = v => PT + (1 - (v - Y0) / (Y1 - Y0)) * (H - PT - PB);
  const k = [];
  const ystep = Y1 - Y0 > 8 ? 2 : 1;
  for (let v = Y0; v <= Y1; v += ystep) k.push(`<line x1="${PL}" x2="${W - PR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="${RT_INK}" stroke-opacity="0.06"></line><text class="map-tick" x="${PL - 7}" y="${(y(v) + 3.5).toFixed(1)}" text-anchor="end">${v}%</text>`);
  for (let v = 0; v <= DX; v += DX > 6 ? 2 : 1) k.push(`<text class="map-tick" x="${x(v).toFixed(1)}" y="${H - 9}" text-anchor="middle">${v} г.</text>`);
  const zcIn = zc.filter(p => p[0] <= DX);
  if (zcIn.length > 1) {
    k.push(`<path d="M${zcIn.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' L')}" fill="none" stroke="${RT_ACC2}" stroke-width="2.4" stroke-linecap="round"></path>`);
    if (dy != null && Math.abs(dy) > 1e-9) {
      k.push(`<path d="M${zcIn.map(p => `${x(p[0]).toFixed(1)},${y(p[1] + dy).toFixed(1)}`).join(' L')}" fill="none" stroke="${RT_ACC}" stroke-width="1.6" stroke-dasharray="5 4" stroke-opacity="0.8"></path>`);
      const e = zcIn[zcIn.length - 1];
      k.push(`<text class="map-curve-label" x="${(x(e[0]) - 4).toFixed(1)}" y="${(y(e[1] + dy) + (dy > 0 ? -8 : 15)).toFixed(1)}" text-anchor="end">через ${RT.h} ${plural(RT.h, ['год', 'года', 'лет'])}, «${escapeHtml((rtScenario(RT.scen) || {}).label || '')}»: ${rtSigned(dy, 1)} п.п.</text>`);
    }
  }
  bonds.forEach(b => {
    const main = b.benchmark;
    k.push(`<circle class="map-dot" data-rt-ofz="${escapeHtml(b.secid)}" cx="${x(b.mod_dur).toFixed(1)}" cy="${y(b.ytm_pct).toFixed(1)}" r="${main ? 4.6 : 4}" fill="${main ? RT_ACC : RT_GREY}" fill-opacity="0.9" stroke="#f5ead8" stroke-width="1"><title>${escapeHtml(b.short_name || b.secid)} · ${escapeHtml(b.instrument_type || '')} · YTM ${fmtPct(b.ytm_pct, 2)} · дюр. ${fmtNum(b.mod_dur, 1)} г.</title></circle>`);
  });
  k.push(`<text class="map-axis-name" x="${W - PR}" y="${H - 9}" text-anchor="end">дюрация →</text>`);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="map-svg" style="cursor:default" role="img" aria-label="Кривая ОФЗ">${k.join('')}</svg>`;
}

function rtOfzTableHtml() {
  const rows = rtOfzBonds();
  const keys = RT_SCENARIOS.map(s => s.key).filter(key => rows.some(b => b.returns[key]));
  const sortKey = RT.ofzSort || RT.scen;
  const total = b => { const r = rtOfzReturn(b, sortKey, RT.h); return r && !r.matured ? r.total_return_pct : -Infinity; };
  const sorted = rows.slice().sort((a, b) => total(b) - total(a));
  const matured = rows.filter(b => { const r = rtOfzReturn(b, RT.scen, RT.h); return r && r.matured; }).length;
  const head = `<th class="al-left">Бумага</th><th class="al-right">YTM</th><th class="al-right">Дюр.</th>`
    + keys.map(key => `<th class="al-right is-sortable${key === sortKey ? ' is-sorted' : ''}" data-rt-sort="${key}" title="${escapeHtml((rtScenario(key) || {}).label || key)}">${RT_SHORT[key]}${key === sortKey ? ' ↓' : ''}</th>`).join('');
  const body = sorted.map(b => `<tr data-rt-ofz="${escapeHtml(b.secid)}">
      <td class="al-left"><div class="c-name-row"><div class="c-name-text"><div class="c-ticker">${escapeHtml(b.short_name || b.secid)}</div><div class="c-issuer">${escapeHtml(b.instrument_type || '')} · погашение ${fmtDate(b.mat_date)}</div></div></div></td>
      <td class="al-right dim">${fmtPct(b.ytm_pct, 1)}</td>
      <td class="al-right dim">${fmtNum(b.mod_dur, 1)} г.</td>
      ${keys.map(key => { const r = rtOfzReturn(b, key, RT.h); return `<td class="al-right${key === RT.scen ? ' c-aey' : ' dim'}">${!r ? DASH : r.matured ? 'погаш.' : rtSigned(r.total_return_pct, 1) + '%'}</td>`; }).join('')}
    </tr>`).join('');
  const excluded = (RT_OFZ.bonds || []).length - rows.length;
  const count = `${rows.length} ПД/АД · ${matured} ${plural(matured, ['гасится', 'гасятся', 'гасятся'])} до горизонта · доходность за ${RT.h} ${plural(RT.h, ['год', 'года', 'лет'])}`
    + (excluded ? ` · ИН, ПК и выпуски без надёжных потоков (${excluded}) не считаются` : '');
  return { head, body, count, empty: !rows.length };
}

function offCurveNote() {
  const off = (RT_OFZ.bonds || []).filter(rtOffCurve);
  return off.length ? ` Не показаны: ${off.map(b => `${b.short_name} (YTM на ${fmtNum(Math.abs(b.curve_gap_bp), 0)} б.п. ${b.curve_gap_bp < 0 ? 'ниже' : 'выше'} КБД — расписание потоков или цена в данных МосБиржи неверны)`).join(', ')}.` : '';
}

function rtOfzHtml() {
  const t = rtOfzTableHtml();
  const curve = rtCurveSvg();
  const snap = RT_OFZ.snap_date;
  return `<div class="term-split" data-layout="A">
    <section class="term-card">
      <div class="term-card-head">
        <div class="term-card-title">Кривая ОФЗ</div>
        <div class="term-card-hint">YTM против дюрации · снимок ${fmtDate(snap)} · КБД ЦБ ${fmtDate((DATA.zcyc || {}).snap_date)} · клик по точке — разбор выпуска</div>
      </div>
      <div class="map-root">${curve || '<div class="map-empty">Котировок ОФЗ в выгрузке нет.</div>'}</div>
      <div class="map-legend">
        <div class="map-legend-item"><span class="map-legend-dot" style="background:${RT_ACC}"></span><span>ОФЗ-ПД / АД</span></div>
        <div class="map-legend-item"><span class="map-legend-dot" style="background:${RT_ACC2}"></span><span>КБД ЦБ</span></div>
        <div class="map-legend-item"><span class="map-legend-dot" style="background:${RT_GREY}"></span><span>ОФЗ-ИН и ПК — не для сравнения с ВДО</span></div>
      </div>
      <div class="map-note">Пунктир — КБД, сдвинутая на изменение ставки сценария с долей ${fmtNum(RT_SHIFT[RT.h] * 100, 0)}% к горизонту ${RT.h} ${plural(RT.h, ['год', 'года', 'лет'])}: то же допущение, по которому посчитана доходность в таблице. Сдвиг параллельный — модель не строит форму кривой.${offCurveNote()}</div>
    </section>
    <section class="term-card term-screener-card">
      <div class="term-card-head term-screener-head">
        <div class="term-card-title">ОФЗ до горизонта</div>
        <div class="term-card-hint">${escapeHtml(t.count)}</div>
      </div>
      <div class="term-chips">${[1, 2, 3].map(h => `<button type="button" class="chip${RT.h === h ? ' is-on' : ''}" data-rt-h="${h}">${h} ${plural(h, ['год', 'года', 'лет'])}</button>`).join('')}</div>
      <div class="screener-scroll">
        ${t.empty ? '<div class="screener-empty">Доходность до горизонта считается по денежным потокам выпуска; в этом снимке их нет — они появятся после ближайшего прогона «hybond ofz».</div>'
          : `<table class="screener"><thead><tr>${t.head}</tr></thead><tbody>${t.body}</tbody></table>`}
      </div>
    </section>
  </div>`;
}

// ── sources (Данные chips, each a link) ──────────────────────────────────
function rtSourcesHtml() {
  const asOf = RE.as_of;
  const chips = (RE.sources || []).map(s => ({ name: s.name, url: s.page_url, date: s.last_published, ok: s.last_ok, err: s.last_error }))
    .concat([
      { name: 'ОФЗ · МосБиржа', url: 'https://iss.moex.com/iss/engines/stock/markets/bonds/boards/TQOB/securities.json', date: RT_OFZ.snap_date, ok: RT_OFZ.snap_date },
      { name: 'КБД ЦБ', url: 'https://www.cbr.ru/hd_base/zcyc_params/', date: (DATA.zcyc || {}).snap_date, ok: (DATA.zcyc || {}).snap_date },
    ]);
  const html = chips.map(c => {
    const age = c.ok && asOf ? Math.round(rtDay(asOf) - rtDay(c.ok)) : null;
    const tone = c.err ? 'warn' : !c.ok ? 'neutral' : age > 7 ? 'old' : 'ok';
    const tail = c.date ? fmtDate(c.date) : 'нет данных';
    return `<a class="d-src is-${tone}" href="${escapeHtml(c.url)}" target="_blank" rel="noopener"><span class="d-src-dot"></span><span>${escapeHtml(c.name)}</span><span class="d-src-ago">${escapeHtml(tail)}</span></a>`;
  }).join('');
  return `<section class="d-card">
    <div class="d-card-head"><div class="d-card-title">Источники и обновление</div><div class="d-card-aside">конвейер: «hybond expectations», «hybond ofz» — каждый прогон</div></div>
    <div class="d-card-sub">Каждый чип — официальная страница, откуда конвейер забирает данные; дата — последняя публикация. Оранжевая точка — последняя загрузка не удалась, показаны данные прошлой удачной.</div>
    <div class="d-srcs">${html}</div>
  </section>`;
}

// ── drawers (the bond drawer's vocabulary) ───────────────────────────────
function rtTable(head, rows) {
  return `<div style="overflow-x:auto"><table class="screener"><thead><tr>${head.map((h, i) => `<th class="${i ? 'al-right' : 'al-left'}">${escapeHtml(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr style="cursor:default">${r.map((c, i) => `<td class="${i ? 'al-right' : 'al-left'}">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function rtActions(a, b) {
  const btn = (x, cls) => x && x[1] ? `<a class="${cls}" href="${escapeHtml(x[1])}" target="_blank" rel="noopener" style="text-align:center;text-decoration:none">${escapeHtml(x[0])}</a>` : '';
  return `<div class="det-actions">${btn(a, 'pill-outline')}${btn(b, 'pill-primary')}</div>`;
}

function rtOpenFeed(f) {
  if (f.kind === 'survey') {
    const sv = f.ref, i = RT_SURVEYS.indexOf(sv), prev = RT_SURVEYS[i - 1];
    const ys = Object.keys(sv.years).sort();
    const body = `<div class="det-tiles">${ys.slice(-3).map((y, j) => detTile(`${y} · медиана`, fmtPct(rtSurveyStat(sv, y, 'median'), 1), j === 1 ? 'is-green' : '')).join('')}</div>`
      + detCard('Распределение ответов', rtTable(['Год', 'Мин', '1-й кв.', 'Медиана', '3-й кв.', 'Макс', 'Ответов'],
        ys.map(y => [y, fmtNum(rtSurveyStat(sv, y, 'min'), 1), fmtNum(rtSurveyStat(sv, y, 'q1'), 1), `<b>${fmtNum(rtSurveyStat(sv, y, 'median'), 1)}</b>`,
          fmtNum(rtSurveyStat(sv, y, 'q3'), 1), fmtNum(rtSurveyStat(sv, y, 'max'), 1), fmtNum(rtSurveyStat(sv, y, 'n'), 0)])))
      + (prev ? detCard(`Изменение к опросу «${rtMonth(prev.period)}»`, `<div class="det-note">${ys.map(y => rtSurveyStat(prev, y, 'median') == null ? null
        : `${y}: ${fmtBpSigned((rtSurveyStat(sv, y, 'median') - rtSurveyStat(prev, y, 'median')) * 100)} б.п.`).filter(Boolean).join(' · ') || 'сравнимых лет нет'}</div>`) : '')
      + '<div class="det-warn">Средняя ключевая ставка за год, с учётом выходных дней. Это консенсус профессиональных аналитиков, а не прогноз ЦБ.</div>'
      + rtActions(['Страница опроса ↗', rtSource('survey').page_url], ['Вся история, xlsx ↗', RE.survey_url]);
    openDetail({ title: `Опрос аналитиков · ${rtMonth(sv.period)}`, sub: `Макроэкономический опрос Банка России${sv === rtLatest(RT_SURVEYS) ? ` · опубликован ${fmtDate(rtSurveyDate(sv))}` : ''}`, bodyHtml: body });
  } else if (f.kind === 'forecast') {
    const fc = f.ref, i = RT_FORECASTS.indexOf(fc), prev = RT_FORECASTS[i - 1];
    const ys = Object.keys(fc.years).sort();
    const body = `<div class="det-tiles">${ys.slice(-3).map((y, j) => detTile(y, rtRange(fc.years[y]), j === 1 ? 'is-green' : '')).join('')}</div>`
      + detCard('Средняя ключевая ставка по годам', rtTable(['Год', 'Диапазон', 'Середина', 'К прошлому прогнозу'],
        // the table's first column is the prior year's actual, not a forecast
        ys.map(y => [+y < +fc.published.slice(0, 4) ? `${y} (факт)` : y, rtRange(fc.years[y]), fmtPct(rtMid(fc.years[y]), 2),
          prev && prev.years[y] && +y >= +fc.published.slice(0, 4) ? `${fmtBpSigned((rtMid(fc.years[y]) - rtMid(prev.years[y])) * 100)} б.п.` : DASH])))
      + (fc.rest ? detCard('Остаток года', `<div class="det-note is-strong">С ${fmtDate(fc.rest.rest_from)} до конца ${fc.rest.year} г. средняя ставка — ${rtRange([fc.rest.lo, fc.rest.hi])}; с 1 января она составила ${fmtPct(fc.rest.ytd_avg, 1)}.</div>`, { cls: 'det-gap det-gap-fair' }) : '')
      + '<div class="det-warn">Числа — как их публикует ЦБ. Цифра сноски, которую PDF приклеивает к числу («14,61»), при разборе отрезается.</div>'
      + rtActions(['Все решения и прогнозы ↗', rtSource('cbr_forecast').page_url], ['PDF прогноза ↗', fc.url]);
    openDetail({ title: `Прогноз ЦБ · ${fmtDate(fc.published)}`, sub: 'Среднесрочный прогноз · базовый сценарий · опорное заседание', bodyHtml: body });
  } else if (f.kind === 'ondkp') {
    const e = f.ref;
    const names = [['disinflation', 'Дезинфляционный'], ['base', 'Базовый'], ['proinflation', 'Проинфляционный'], ['risk', 'Рисковый']];
    const ys = Array.from(new Set([].concat(...names.map(([n]) => Object.keys(e.scenarios[n] || {}))))).sort();
    const body = detCard('Средняя ключевая ставка по сценариям', rtTable(['Сценарий'].concat(ys), names.map(([n, label]) => [label].concat(ys.map(y => rtRange((e.scenarios[n] || {})[y]))))))
      + detCard('Как читать', '<div class="det-note">Базовый сценарий совпадает со среднесрочным прогнозом ближайшего опорного заседания. Альтернативные сценарии ЦБ публикует дважды в год на одной странице: проект в конце августа, итоговую редакцию — после октябрьского опорного заседания. В AEY стресс — «Проинфляционный», бычий — «Дезинфляционный», «Рисковый» — отдельной колонкой.</div>')
      + (e.status === 'draft' ? '<div class="det-warn">Это проект. Итоговая редакция заменит его на той же странице — конвейер сохранит обе версии.</div>' : '')
      + rtActions(['Все редакции ↗', rtSource('ondkp').page_url], ['Эта редакция ↗', e.url]);
    openDetail({ title: `Сценарии ЦБ · ОНЕГДКП ${e.edition.replace('on_', '').replace('_', '–')}`, sub: `Основные направления ДКП · ${e.status === 'draft' ? 'проект от' : 'редакция'} ${fmtDate(e.published)}`, bodyHtml: body });
  } else if (f.kind === 'decision') {
    const dc = f.ref;
    const next = (RE.schedule || []).find(m => m.date > dc.date);
    const body = `<div class="det-tiles">${detTile('Ставка', fmtPct(dc.rate, 2), 'is-green')}${detTile('Шаг', dc.change_bp == null ? DASH : dc.change_bp === 0 ? 'без изм.' : `${fmtBpSigned(dc.change_bp)} б.п.`)}${detTile('Следующее', next ? fmtDayMonth(next.date) : DASH)}</div>`
      + detCard('Пресс-релиз', `<div class="det-note">${escapeHtml(dc.title || '')}</div>`)
      + (dc.signal ? detCard('Сигнал', `<div class="det-note is-strong">${escapeHtml(dc.signal)}</div>`, { cls: 'det-gap det-gap-up' }) : '')
      + rtActions(['Заявление Председателя ↗', dc.statement_url], ['Пресс-релиз ↗', dc.url]);
    openDetail({ title: `Решение ЦБ · ${fmtDate(dc.date)}`, sub: `Совет директоров${dc.forecast_url ? ' · опорное заседание' : ''}`, bodyHtml: body });
  } else if (f.kind === 'summary') {
    const sm = f.ref;
    const body = detCard('Какие варианты обсуждали', `<div class="det-note is-strong">${escapeHtml(sm.options || 'Варианты решения в этом резюме списком не перечислены.')}</div>`, { cls: 'det-gap det-gap-fair' })
      + (sm.positions ? detCard('Позиции участников', `<div class="det-note">${escapeHtml(sm.positions)}</div>`) : '')
      + '<div class="det-warn">Выдержка из резюме; аргументы сторон — в полном тексте по ссылке.</div>'
      + rtActions(['Календарь ЦБ ↗', rtSource('calendar').page_url], ['Резюме ↗', sm.url]);
    openDetail({ title: `Резюме обсуждения · ${fmtDate(sm.published)}`, sub: `по заседанию ${fmtDate(sm.meeting_date)}`, bodyHtml: body });
  }
}

function rtOpenOfz(secid) {
  const b = (RT_OFZ.bonds || []).find(o => o.secid === secid);
  if (!b) return;
  const cur = rtScenario(RT.scen);
  const tiles = `<div class="det-tiles">${detTile('YTM', fmtPct(b.ytm_pct, 2), 'is-green')}${detTile('Дюрация', b.mod_dur == null ? DASH : `${fmtNum(b.mod_dur, 2)} г.`)}${detTile('Цена', b.last_pct == null ? DASH : `${fmtNum(b.last_pct, 2)}%`)}</div>`;
  const facts = `<div class="det-facts"><div><span>Тип</span><b>${escapeHtml(b.instrument_type || DASH)}</b></div><div><span>Купон</span><b>${fmtPct(b.coupon_pct, 2)}</b></div>
    <div><span>Погашение</span><b>${fmtDate(b.mat_date)}</b></div><div><span>Сделок</span><b>${fmtNum(b.num_trades, 0)}</b></div></div>`;
  let body;
  if (!b.benchmark || !b.returns) {
    body = tiles + facts + `<div class="det-warn">${b.instrument_type === 'ИН' ? 'ОФЗ-ИН: доходность реальная, на индексируемый номинал — с номинальными облигациями напрямую не сравнивается.'
      : b.instrument_type === 'ПК' ? 'ОФЗ-ПК: купон плавающий, будущие потоки неизвестны — доходность до горизонта не считается.'
      : rtOffCurve(b) ? `YTM по потокам на ${fmtNum(Math.abs(b.curve_gap_bp), 0)} б.п. ${b.curve_gap_bp < 0 ? 'ниже' : 'выше'} кривой КБД ЦБ — у государственного выпуска так не бывает: расписание потоков или цена в данных МосБиржи неверны. Выпуск не используется как ориентир и не считается.`
      : 'Потоков выпуска в этом снимке нет — доходность до горизонта не считается.'}</div>`;
  } else {
    const r = rtOfzReturn(b, RT.scen, RT.h);
    const wf = r && !r.matured ? rtWaterfall([
      ['Купоны и погашения', r.cash_received_pct], ['Приближение к номиналу', r.pull_to_par_pct],
      ['Переоценка по сценарию', r.repricing_pct], ['Итого', r.total_return_pct]]) : null;
    const keys = RT_SCENARIOS.map(s => s.key).filter(k => b.returns[k]);
    body = tiles + facts
      + detCard(`Доходность за ${RT.h} ${plural(RT.h, ['год', 'года', 'лет'])} · «${cur ? cur.label : ''}»`, r && r.matured
        ? '<div class="det-note">Выпуск погашается раньше горизонта — доходности до горизонта у него нет.</div>'
        : `${wf}<div class="det-note">Потоки выпуска (купоны, амортизация, погашение) без реинвестирования; цена на горизонте — оставшиеся потоки по YTM ${rtSigned(r ? r.delta_y_applied * 100 : null, 2)} п.п. Сдвиг — ${fmtNum(RT_SHIFT[RT.h] * 100, 0)}% изменения ставки сценария к горизонту.</div>`)
      + detCard('Все сценарии и горизонты', rtTable(['Сценарий', '1 год', '2 года', '3 года'], keys.map(k => [(rtScenario(k) || {}).label || k]
        .concat([1, 2, 3].map(h => { const x = rtOfzReturn(b, k, h); return !x ? DASH : x.matured ? 'погаш.' : `${rtSigned(x.total_return_pct, 1)}%`; })))));
  }
  openDetail({ title: b.short_name || b.secid, sub: `${b.secid} · снимок ${fmtDate(RT_OFZ.snap_date)}`, bodyHtml: body });
}

// Horizontal bars in the bond drawer's waterfall idiom (detWaterfall), for
// signed components.
function rtWaterfall(rows) {
  const W = 460, LX = 170, RG = 70, PW = W - LX - RG;
  const max = Math.max.apply(null, rows.map(r => Math.abs(r[1] || 0)).concat([1]));
  const bars = rows.map(([label, v], i) => {
    const yy = i * 30 + 4, w = Math.abs(v || 0) / max * PW;
    const fill = i === rows.length - 1 ? '#7a8a5e' : v < 0 ? '#8a8478' : '#c67139';
    return `<rect x="${LX}" y="${yy}" width="${Math.max(1, w).toFixed(1)}" height="18" rx="7" fill="${fill}"></rect>
      <text x="${LX - 10}" y="${yy + 13.5}" class="wf-label" text-anchor="end">${escapeHtml(label)}</text>
      <text x="${W}" y="${yy + 13.5}" class="wf-value" text-anchor="end">${rtSigned(v, 2)}%</text>`;
  }).join('');
  return `<svg viewBox="0 0 ${W} ${rows.length * 30 + 4}" width="100%" class="det-svg">${bars}</svg>`;
}

// ── page ─────────────────────────────────────────────────────────────────
// Three blocks under the title row, laid out by .page-wide's own gap: the
// Портфель-shaped top (tiles + split, spaced exactly as on Портфель), the
// OFZ split, the sources card.
function rtRender() {
  const top = document.getElementById('rtTop');
  if (!top) return;
  document.getElementById('rtPills').innerHTML = rtPillsHtml();
  const ofz = document.getElementById('rtOfz'), src = document.getElementById('rtSrc');
  if (!RT_SCENARIOS.length && !RT_FORECASTS.length && !(RT_OFZ.bonds || []).length) {
    top.innerHTML = `<section class="pf-card"><div class="pf-card-title">Данных о ставке в выгрузке пока нет</div>
      <div class="pf-card-note">Страница заполняется из «hybond expectations» (прогнозы и сценарии ЦБ, макроопрос, решения) и «hybond ofz». Первую загрузку истории делает «hybond expectations --backfill».</div></section>`;
    ofz.innerHTML = src.innerHTML = '';
    return;
  }
  top.innerHTML = `${rtStats()}
    <div class="pf-split">
      <section class="pf-card">${rtExpHtml()}</section>
      <div class="pf-aside">${rtPathHtml()}${rtSaidHtml()}</div>
    </div>`;
  ofz.innerHTML = rtOfzHtml();
  src.innerHTML = rtSourcesHtml();
}

function rtOnClick(e) {
  const sc = e.target.closest('[data-rt-scen]');
  if (sc) { RT.scen = sc.dataset.rtScen; RT.ofzSort = null; rtRender(); return; }
  const yr = e.target.closest('[data-rt-year]');
  if (yr) { RT.year = +yr.dataset.rtYear; RT.expanded = false; rtRender(); return; }
  const h = e.target.closest('[data-rt-h]');
  if (h) { RT.h = +h.dataset.rtH; rtRender(); return; }
  const so = e.target.closest('[data-rt-sort]');
  if (so) { RT.ofzSort = so.dataset.rtSort; rtRender(); return; }
  if (e.target.closest('#rtMore')) { RT.expanded = !RT.expanded; rtRender(); return; }
  const row = e.target.closest('[data-rt-feed]');
  if (row) { const f = RT.feed[+row.dataset.rtFeed]; if (f) rtOpenFeed(f); return; }
  const ofz = e.target.closest('[data-rt-ofz]');
  if (ofz) rtOpenOfz(ofz.dataset.rtOfz);
}

PAGES.rates = {
  render(el) {
    el.innerHTML = `<div class="page-wide">
      <div class="page-title-row">
        <div class="page-title">Ставка и ОФЗ</div>
        <div class="page-title-spacer"></div>
        <div class="pf-sources" id="rtPills"></div>
      </div>
      <div id="rtTop"></div>
      <div id="rtOfz"></div>
      <div id="rtSrc"></div>
    </div>`;
    el.addEventListener('click', rtOnClick);
    rtRender();
  },
};
