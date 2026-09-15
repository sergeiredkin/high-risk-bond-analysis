// Core: embedded data + formatting helpers shared by every page.
// DATA is data/dashboard_data.json, embedded by scripts/build_dashboard.py.
// Rule inherited from the pipeline: a missing value renders as "—", never
// as an estimate.

const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);

const DASH = '—';

// Russian number formatting: decimal comma, thin grouping space.
function fmtNum(v, digits) {
  if (v == null || !isFinite(v)) return DASH;
  return v.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtPct(v, digits) {
  return v == null || !isFinite(v) ? DASH : fmtNum(v, digits == null ? 2 : digits) + '%';
}

// plural(3, ['замечание', 'замечания', 'замечаний']) -> 'замечания'
function plural(n, forms) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

const RU_MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

// '2026-10-23' -> '23 окт'
function fmtDayMonth(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${parseInt(m[3], 10)} ${RU_MONTHS_SHORT[parseInt(m[2], 10) - 1]}` : DASH;
}
// '2026-10-23' -> '23.10.2026'
function fmtDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${m[3]}.${m[2]}.${m[1]}` : DASH;
}

function median(values) {
  const v = values.filter(x => x != null && isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = v.length >> 1;
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Pages register here; app.js owns routing and calls render(el) on first show.
const PAGES = {};
