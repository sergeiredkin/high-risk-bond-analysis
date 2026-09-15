// App shell: header readouts, page routing, bond-detail drawer.
// Reference mapping: isOverview/isTerminal/isPortfolio/isData -> the
// visible <section id="page-*">; detailOpen -> #detail not hidden.

const PAGE_IDS = ['overview', 'terminal', 'portfolio', 'data'];

// ── header: ЦБ key rate + pipeline health ───────────────────────────────
function renderHeader() {
  const cbr = DATA.cbr || {};
  document.getElementById('cbrRate').textContent = fmtPct(cbr.current_rate);
  document.getElementById('cbrNext').textContent = cbr.next_meeting ? `заседание ${fmtDayMonth(cbr.next_meeting)}` : '';

  // DATA.pipeline_health.status is the worst unresolved level (health.summary()).
  const health = DATA.pipeline_health || {};
  const counts = health.unresolved_counts || {};
  const n = lvl => counts[lvl] || 0;
  const forms = {
    warn: ['замечание', 'замечания', 'замечаний'],
    error: ['ошибка', 'ошибки', 'ошибок'],
    critical: ['критическая', 'критические', 'критических'],
  };
  const count = lvl => `${n(lvl)} ${plural(n(lvl), forms[lvl])}`;
  // Dot colour follows the reference (accent-400 on the dark bar for any
  // open issue); severity is carried by the label, since darker ramp steps
  // disappear against neutral-900.
  let label = 'Конвейер: нет данных', dot = 'var(--color-neutral-400)';
  if (health.status === 'ok') { label = 'Конвейер: в норме'; dot = 'var(--color-accent-2-400)'; }
  else if (forms[health.status]) { label = `Конвейер: ${count(health.status)}`; dot = 'var(--color-accent-400)'; }
  document.getElementById('healthLabel').textContent = label;
  document.getElementById('healthDot').style.background = dot;

  const tip = [`Нерешённых: ${count('critical')}, ${count('error')}, ${count('warn')}`];
  if (health.latest_error) tip.push(`Последняя ошибка: ${health.latest_error.code} — ${health.latest_error.detail}`);
  // Two separate freshness signals, never merged: the DB snapshot date
  // (day2_snap_date, can be stale) and when this file was rebuilt (rebuild_at).
  const rebuildAt = DATA.rebuild_at ? `${fmtDate(DATA.rebuild_at)} ${DATA.rebuild_at.slice(11, 16)}` : DASH;
  tip.push(`Снимок данных: ${fmtDate(DATA.day2_snap_date)} · дашборд собран: ${rebuildAt}`);
  document.getElementById('healthChip').title = tip.join('\n');
}

// ── routing ─────────────────────────────────────────────────────────────
const rendered = new Set();

function currentPage() {
  const id = location.hash.replace(/^#/, '');
  return PAGE_IDS.includes(id) ? id : 'overview';
}

function showPage(id) {
  PAGE_IDS.forEach(p => {
    const el = document.getElementById('page-' + p);
    const on = p === id;
    if (on && !rendered.has(p)) { PAGES[p].render(el); rendered.add(p); }
    el.hidden = !on;
  });
  document.querySelectorAll('.topnav-btn').forEach(btn => {
    if (btn.dataset.page === id) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });
  closeDetail();
}

function goPage(id) {
  if (currentPage() === id && location.hash) return;
  location.hash = id; // hashchange -> showPage
  window.scrollTo(0, 0);
}

// ── bond detail drawer (filled by stage 2's screener/map) ────────────────
function openDetail({ title, sub, bodyHtml }) {
  document.getElementById('detailTitle').textContent = title || '';
  document.getElementById('detailSub').textContent = sub || '';
  document.getElementById('detailBody').innerHTML = bodyHtml || '';
  document.getElementById('detail').hidden = false;
}
function closeDetail() {
  document.getElementById('detail').hidden = true;
}

// ── wiring ──────────────────────────────────────────────────────────────
document.addEventListener('click', e => {
  const nav = e.target.closest('[data-page]');
  if (nav) { goPage(nav.dataset.page); return; }
  if (e.target.closest('[data-close-detail]')) closeDetail();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDetail();
});
window.addEventListener('hashchange', () => showPage(currentPage()));

renderHeader();
showPage(currentPage());
