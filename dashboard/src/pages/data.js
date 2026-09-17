// Данные -- the isData page of bond-lab-design-reference.html.
//
// Four blocks, in the reference's order: pipeline health by code, the
// sources and their freshness, the "ничего не выдумываем" panel, and the
// event feed. The health chip in the top bar links here.
//
// What the reference shows and this page does NOT:
//   - a fixed nine-code registry. There is no code registry in this
//     codebase; codes are logged ad hoc by health.log_health(), so the list
//     is whatever DATA.pipeline_health.unresolved actually carries, grouped.
//   - "Семь источников". The export has no source inventory, so this block
//     shows the dated artefacts it does carry, each with its own date.
//   - a per-event confidence score. Classified events carry a category and
//     a severity, never a confidence, so severity is what is shown.
//   - the reference's 834/27/142 counters. Test and CLI counts are not
//     dashboard content; the defaults count is read from the store.

const D_FEED_PAGE = 40;
const D_SEVERITY = {
  // [label, chip class, rank] -- rank orders the feed, worst first
  critical: ['критическое', 'ev-hot', 0],
  high: ['высокое', 'ev-hot', 1],
  medium: ['среднее', 'ev-mid', 2],
  low: ['низкое', 'ev-calm', 3],
  noise: ['шум', 'ev-calm', 4],
};
// All ten categories of classify_ollama.VALID_CATEGORIES (the taxonomy in
// skills/hy-event-classify/references/taxonomy.md). Kept complete on
// purpose: an unmapped category falls through to its raw English key, which
// is what a partial map here would print into a Russian UI.
const D_CATEGORY = {
  reporting_delay: 'задержка отчётности',
  coupon_event: 'купонное событие',
  offer_put_call: 'оферта / колл',
  covenant: 'ковенант',
  rating_action: 'рейтинговое действие',
  governance: 'корпоративное управление',
  insolvency: 'несостоятельность',
  operations: 'операционное',
  capital_action: 'действие с капиталом',
  routine: 'плановое раскрытие',
};
// Feed `source` values, as carried by issuer events and market notes. Same
// rule as D_CATEGORY: an unmapped source falls through to its raw key.
const D_SOURCE = {
  event: 'раскрытие эмитента',
  moex_listing: 'листинг МосБиржи',
  cbr_press: 'пресс-релиз ЦБ',
  smartlab_news: 'Smartlab',
  google_news: 'Google News',
  watchlist_health_scan: 'скан здоровья',
  manual: 'ручная запись',
};
const D_LEVEL = {
  critical: ['критическая', 'критические', 'критических'],
  error: ['ошибка', 'ошибки', 'ошибок'],
  warn: ['замечание', 'замечания', 'замечаний'],
};

// filter holds a D_FILTERS label, not a key -- the chips compare against it
// directly, so the default has to be one of those labels or nothing lights up.
const D = { filter: 'Значимые', shown: D_FEED_PAGE };

// ── 1. pipeline health, grouped by code ─────────────────────────────────
function dHealthHtml() {
  const health = DATA.pipeline_health || {};
  const open = health.unresolved || [];

  // Group by code, keeping the worst level and the newest occurrence.
  const rank = { critical: 0, error: 1, warn: 2 };
  const by = new Map();
  open.forEach(u => {
    const g = by.get(u.code) || { code: u.code, n: 0, level: u.level, stage: u.stage, latest: u.recorded_at, detail: u.detail };
    g.n += 1;
    if ((rank[u.level] ?? 9) < (rank[g.level] ?? 9)) { g.level = u.level; g.detail = u.detail; }
    if (u.recorded_at > g.latest) { g.latest = u.recorded_at; }
    by.set(u.code, g);
  });
  const groups = Array.from(by.values())
    .sort((a, b) => (rank[a.level] ?? 9) - (rank[b.level] ?? 9) || b.n - a.n);

  const counts = health.unresolved_counts || {};
  const n = lvl => counts[lvl] || 0;
  // The counts are the authoritative totals; `unresolved` is a capped
  // sample of them, so say when the list is shorter than the count.
  const totalCounted = n('critical') + n('error') + n('warn');
  const summary = health.status === 'ok'
    ? 'Нерешённых записей нет.'
    : ['critical', 'error', 'warn'].filter(l => n(l))
        .map(l => `${fmtNum(n(l), 0)} ${plural(n(l), D_LEVEL[l])}`).join(' · ');

  const rows = groups.length ? groups.map(g => {
    const lvl = g.level === 'warn' ? 'warn' : 'err';
    return `<div class="d-code is-${lvl}">
      <div class="d-code-id">${escapeHtml(g.code)}</div>
      <div class="d-code-body">
        <div class="d-code-detail">${escapeHtml(g.detail || '')}</div>
        <div class="d-code-meta">этап ${escapeHtml(g.stage || DASH)} · последняя запись ${fmtDate(g.latest)}</div>
      </div>
      <div class="d-code-n">${g.n > 1 ? `${fmtNum(g.n, 0)} ${plural(g.n, ['запись', 'записи', 'записей'])}` : '1 запись'}</div>
    </div>`;
  }).join('') : '<div class="d-empty">Открытых записей о здоровье конвейера нет.</div>';

  const capped = totalCounted > open.length
    ? `<div class="d-code-capped">В выгрузку попали ${fmtNum(open.length, 0)} последних из ${fmtNum(totalCounted, 0)} — полный список в <code>hybond health</code>.</div>`
    : '';

  const last = health.latest_error;
  return `<section class="d-card">
    <div class="d-card-head">
      <div class="d-card-title">Здоровье конвейера</div>
      <div class="d-card-aside">${escapeHtml(summary)}</div>
    </div>
    ${last ? `<div class="d-latest">
      <div class="d-latest-k">Последняя ошибка</div>
      <div class="d-latest-v">${escapeHtml(last.code)} — ${escapeHtml(last.detail)}</div>
      <div class="d-latest-m">${fmtDate(last.recorded_at)}</div>
    </div>` : ''}
    <div class="d-codes">${rows}</div>
    ${capped}
    <div class="d-card-note">Красный код блокирует промоушен данных в аналитический слой: пока причина не разобрана, вчерашний снимок остаётся текущим, а поле помечается как устаревшее — не подменяется оценкой.</div>
  </section>`;
}

// ── 2. sources and their freshness ──────────────────────────────────────
// Every chip is a dated artefact the export actually carries. Age is
// measured against the newest of them rather than the browser's clock, so
// an old dashboard file does not read as a fresh one.
function dSourcesHtml() {
  const fresh = DATA.portfolio_data_freshness || {};
  const meta = DATA.pd_table_meta || {};
  const items = [
    ['MOEX ISS · вселенная', (DATA.universe_now || {}).snap_date],
    ['MOEX ISS · спреды', fresh.spread_snapshot_date],
    ['Мониторинг эмитентов', DATA.day2_snap_date],
    ['ОФЗ · кривая', (DATA.ofz || {}).snap_date],
    ['ЦБ · ключевая ставка', ((DATA.cbr || {}).meetings || []).filter(m => m.rate_after != null)
      .map(m => m.date).sort().pop()],
    [`PD-таблица · ${meta.source || 'источник не указан'}`, meta.dated],
    ['MOEX ЦКИ · рейтинги', ((DATA.ratings_meta || {}).last_refresh || '').slice(0, 10) || null],
    ['Дефолты МосБиржи', ((DATA.moex_defaults || {}).watchlist_matches || [])
      .map(m => m.planned_date).filter(Boolean).sort().pop()],
  ].filter(([, d]) => d);

  const newest = items.map(([, d]) => d).sort().pop();
  const chips = items.sort((a, b) => b[1].localeCompare(a[1])).map(([name, d]) => {
    const days = newest ? Math.round((Date.parse(newest) - Date.parse(d)) / 864e5) : null;
    const tone = days == null ? 'neutral' : days <= 1 ? 'ok' : days <= 14 ? 'warn' : 'old';
    const ago = days == null ? '' : days <= 0 ? 'свежий' : `−${fmtNum(days, 0)} ${plural(days, ['день', 'дня', 'дней'])}`;
    return `<div class="d-src is-${tone}"><span class="d-src-dot"></span><span>${escapeHtml(name)}</span>
      <span class="d-src-ago">${fmtDate(d)}${ago ? ` · ${ago}` : ''}</span></div>`;
  }).join('');

  const rebuild = DATA.rebuild_at ? `${fmtDate(DATA.rebuild_at)} ${DATA.rebuild_at.slice(11, 16)}` : DASH;
  return `<section class="d-card">
    <div class="d-card-head"><div class="d-card-title">Источники</div></div>
    <div class="d-srcs">${chips}</div>
    <div class="d-card-note">Возраст считается относительно самого свежего артефакта в этой выгрузке, а не от текущего времени — устаревший файл дашборда не выглядит свежим. Дашборд собран ${escapeHtml(rebuild)}. Расхождение между источниками не усредняется: оно фиксируется как конфликт и уходит в коды здоровья.</div>
  </section>`;
}

// ── 3. "ничего не выдумываем" ───────────────────────────────────────────
function dPolicyHtml() {
  const meta = DATA.pd_table_meta || {};
  const defaults = ((DATA.moex_defaults || {}).watchlist_matches || []).length;
  const issuers = (DATA.issuers || []).length;
  const passive = (DATA.issuers_passive || []).length;
  const universe = ((DATA.universe_now || {}).bonds || []).length;
  const rm = DATA.ratings_meta || {};

  const counters = [
    [fmtNum(universe, 0), 'бумаг с котировкой в снимке'],
    [rm.bonds ? fmtPct(rm.rated / rm.bonds * 100, 0) : DASH, 'из них с рейтингом агентства'],
    [fmtNum(issuers + passive, 0), 'эмитентов в мониторинге'],
    [fmtNum(defaults, 0), `${plural(defaults, ['дефолт', 'дефолта', 'дефолтов'])} МосБиржи по ним`],
    [meta.recovery_rate == null ? DASH : fmtPct(meta.recovery_rate * 100, 1), 'ставка восстановления в модели'],
  ];

  return `<section class="d-policy">
    <div class="d-policy-kicker">Политика «ничего не выдумываем»</div>
    <div class="d-policy-body">Отсутствующее поле показывается как отсутствующее. Ни интерполяции по отрасли, ни переноса прошлого квартала без метки. Бумага без рейтинга не получает справедливый спред, а не получает выдуманный.</div>
    <div class="d-policy-counters">
      ${counters.map(([v, k]) => `<div><div class="d-policy-v">${escapeHtml(v)}</div><div class="d-policy-k">${escapeHtml(k)}</div></div>`).join('')}
    </div>
  </section>`;
}

// ── 4. event feed ───────────────────────────────────────────────────────
// Classified issuer events (hy-event-classify) merged with market-wide
// notes. Issuer events carry a category and a severity; market notes carry
// a severity and a link but no category, and are labelled as market-wide
// rather than given a class they do not have.
function dFeed() {
  const out = [];
  const push = (list, issuer) => (list || []).forEach(e => out.push({
    occurred: e.occurred, severity: e.severity, category: e.category,
    summary: e.summary, source: e.source, url: e.url || null, issuer,
  }));
  (DATA.issuers || []).forEach(i => push(i.events, i.name));
  (DATA.issuers_passive || []).forEach(i => push(i.events, i.name));
  push(DATA.market_wide_notes, null);
  return out.sort((a, b) => String(b.occurred).localeCompare(String(a.occurred)));
}

const D_FEED = dFeed();

const D_FILTERS = [
  ['Значимые', e => (D_SEVERITY[e.severity] || [, , 9])[2] <= 2],
  ['Все', () => true],
  ['Рейтинговые действия', e => e.category === 'rating_action'],
  ['Корпоративные', e => e.category === 'governance' || e.category === 'capital_action' || e.category === 'covenant'],
  ['Несостоятельность', e => e.category === 'insolvency'],
  ['Рынок в целом', e => !e.issuer],
];

function dFeedVisible() {
  const f = (D_FILTERS.find(x => x[0] === D.filter) || D_FILTERS[0])[1];
  return D_FEED.filter(f);
}

function dEventHtml(e) {
  const [sevLabel, sevCls] = D_SEVERITY[e.severity] || [e.severity || 'без оценки', 'ev-mid'];
  const cat = e.category ? (D_CATEGORY[e.category] || e.category) : (e.issuer ? null : 'рынок в целом');
  const chips = [`<span class="d-ev-chip ${sevCls}">${escapeHtml(sevLabel)}</span>`];
  if (cat) chips.push(`<span class="d-ev-chip ev-plain">${escapeHtml(cat)}</span>`);
  if (e.issuer) chips.push(`<span class="d-ev-chip ev-plain">${escapeHtml(e.issuer)}</span>`);
  if (e.source) chips.push(`<span class="d-ev-chip ev-src">${escapeHtml(D_SOURCE[e.source] || e.source)}</span>`);
  const text = e.url
    ? `<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener">${escapeHtml(e.summary || '')}</a>`
    : escapeHtml(e.summary || '');
  return `<div class="d-ev">
    <div class="d-ev-date">${fmtDayMonth(e.occurred)}</div>
    <div class="d-ev-body">
      <div class="d-ev-text">${text}</div>
      <div class="d-ev-chips">${chips.join('')}</div>
    </div>
  </div>`;
}

function dRenderFeed() {
  const vis = dFeedVisible();
  const rows = vis.slice(0, D.shown);
  document.getElementById('dFeedRows').innerHTML = rows.length
    ? rows.map(dEventHtml).join('')
    : '<div class="d-empty">Под этот фильтр событий нет.</div>';
  const rest = vis.length - rows.length;
  const more = document.getElementById('dFeedMore');
  more.hidden = rest <= 0;
  if (rest > 0) more.innerHTML = `<button type="button" class="pill-outline" id="dFeedMoreBtn">Показать ещё ${fmtNum(Math.min(rest, D_FEED_PAGE), 0)} · осталось ${fmtNum(rest, 0)}</button>`;
  document.getElementById('dFeedCount').textContent =
    `${fmtNum(vis.length, 0)} из ${fmtNum(D_FEED.length, 0)} ${plural(D_FEED.length, ['записи', 'записей', 'записей'])}`;
  document.querySelectorAll('[data-d-filter]').forEach(b =>
    b.classList.toggle('is-on', b.dataset.dFilter === D.filter));
}

function dFeedHtml() {
  return `<section class="d-card">
    <div class="d-card-head">
      <div class="d-card-title">Лента событий</div>
      <div class="d-card-aside" id="dFeedCount"></div>
    </div>
    <div class="d-card-sub">Классификация локальной моделью (Ollama) — документы не покидают машину. Рыночные записи не классифицируются, у них есть только уровень и ссылка.</div>
    <div class="d-chips">${D_FILTERS.map(([label]) =>
      `<button type="button" class="chip" data-d-filter="${escapeHtml(label)}">${escapeHtml(label)}</button>`).join('')}</div>
    <div class="d-feed" id="dFeedRows"></div>
    <div class="d-feed-more" id="dFeedMore" hidden></div>
  </section>`;
}

// ── page ────────────────────────────────────────────────────────────────
function dOnClick(e) {
  const chip = e.target.closest('[data-d-filter]');
  if (chip) { D.filter = chip.dataset.dFilter; D.shown = D_FEED_PAGE; dRenderFeed(); return; }
  if (e.target.closest('#dFeedMoreBtn')) { D.shown += D_FEED_PAGE; dRenderFeed(); }
}

PAGES.data = {
  render(el) {
    el.innerHTML = `<div class="page-wide">
      <div class="d-split">
        ${dHealthHtml()}
        <div class="d-aside">
          ${dSourcesHtml()}
          ${dPolicyHtml()}
        </div>
      </div>
      ${dFeedHtml()}
    </div>`;
    el.addEventListener('click', dOnClick);
    dRenderFeed();
  },
};
