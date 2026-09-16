// Bond model shared by the map, the screener and the detail drawer.
// One row per bond in DATA.universe_now.bonds (the latest MOEX snapshot),
// joined to its issuer's monitoring record where one exists. Nothing is
// estimated: a field the export does not carry stays null and renders "—".
//
// Field sources (scripts/export_dashboard_data.py):
//   ytm          ytm_pct
//   aeyBase      aey_base_pct            AEY before credit losses
//   aey          aey_base_risk_adj_pct   (1 − PD)·AEY − PD·LGD; rated bonds only
//   gs           g_spread_bp             spread to the ОФЗ zero curve
//   fair         implied_fair_spread_bp  PD × LGD for the rating (compute/default_probs.py)
//   gap          mispricing_bp           gs − fair; null when the signal is excluded
//   score        mean of the issuer's four РСБУ pillar scores (compute/pillars.py), 0–10
//   pdRating     pd_rating × 100         annual PD in percent (export gives a fraction)
//   watch        in_watchlist            the "наблюдение" flag on a bond

// Bands and thresholds. Chosen for this data, not copied from the design's
// mock numbers (its ±60 б.п. band and 500 млн ₽ liquidity cut sit outside
// the real distribution).
const FAIR_BAND_BP = 300;          // |gap| within this = "в пределах модели" (old map's ±300 б.п. band)
const CANDIDATE_GAP_BP = 300;      // "кандидат": wider than fair by more than the band, EV zone green
const LIQUID_VOLUME_RUB = 1e6;     // "Ликвидные" chip: оборот за сессию ≥ 1 млн ₽ (top ~7% of the universe)
const LOW_LIQ_VOLUME_RUB = 1e4;    // "низкая ликв." flag: оборот за сессию < 10 тыс. ₽ (bottom quartile)
const EVENT_LOOKBACK_DAYS = 30;

const SECTOR_LABELS = DATA.sector_labels || {};
function sectorLabel(s) { return s ? (SECTOR_LABELS[s] || s) : null; }

const PILLARS = [
  // [score key, name, raw-ratio formatter]
  ['score_leverage', 'Долговая нагрузка', p => p.debt_to_ebit != null ? `долг/EBIT ${fmtNum(p.debt_to_ebit, 1)}×` : null],
  ['score_liquidity', 'Ликвидность', p => p.current_ratio != null ? `тек. ликв. ${fmtNum(p.current_ratio, 2)}` : null],
  ['score_refinancing', 'Рефинансирование', p => p.short_term_debt_share != null ? `кор. долг ${fmtNum(p.short_term_debt_share * 100, 0)}%` : null],
  ['score_operations', 'Рентабельность', p => p.net_margin != null ? `чистая маржа ${fmtNum(p.net_margin * 100, 1)}%` : null],
];

function pillarScore(sixPillars) {
  if (!sixPillars) return null;
  const v = PILLARS.map(([k]) => sixPillars[k]).filter(x => x != null);
  return v.length ? v.reduce((a, c) => a + c, 0) / v.length / 10 : null;
}

// 'Общество с ограниченной ответственностью "Ромашка"' -> 'Ромашка'
const LEGAL_FORM_RE = /^(публичное акционерное общество|непубличное акционерное общество|акционерное общество|общество с ограниченной ответственностью|пао|ао|ооо|нао)\s+/i;
function shortIssuerName(title) {
  if (!title) return null;
  const s = title.replace(LEGAL_FORM_RE, '').replace(/["«»“”]/g, '').trim();
  return s || title;
}

function ratingClass(r) {
  // 'ruBBB+' -> 'BBB'; CC/C fold into CCC like the old map's rating tiers
  if (!r) return null;
  const core = r.replace(/^ru/i, '').replace(/[+-]$/, '').split('.')[0];
  return core === 'CC' || core === 'C' ? 'CCC' : core;
}

function daysAgo(iso, asOf) {
  const a = Date.parse((iso || '').slice(0, 10)), b = Date.parse((asOf || '').slice(0, 10));
  return isFinite(a) && isFinite(b) ? (b - a) / 864e5 : null;
}

const BONDS = (() => {
  const asOf = (DATA.universe_now && DATA.universe_now.snap_date) || DATA.day2_snap_date;
  const monitored = new Map((DATA.issuers || []).map(i => [i.issuer_id, i]));
  const passive = new Map((DATA.issuers_passive || []).map(i => [i.issuer_id, i]));
  const known = DATA.known_issuers || {};

  return ((DATA.universe_now && DATA.universe_now.bonds) || []).map(b => {
    const iss = monitored.get(b.emitent_inn) || null;
    const pass = passive.get(b.emitent_inn) || null;
    const events = ((iss || pass || {}).events || []);
    const recentSerious = events.filter(e => (e.severity === 'critical' || e.severity === 'high')
      && daysAgo(e.occurred, asOf) != null && daysAgo(e.occurred, asOf) <= EVENT_LOOKBACK_DAYS);
    const flagsToday = iss && iss.snap_date === asOf ? (iss.flags_today || []).filter(f => f.secid === b.secid) : [];
    const gap = b.signal_excluded ? null : b.mispricing_bp;
    const name = (known[b.emitent_inn] && known[b.emitent_inn].name) || shortIssuerName((iss && iss.name) || b.emitent_title) || b.secid;

    const bond = {
      secid: b.secid, t: b.short_name || b.secid, n: name, inn: b.emitent_inn,
      sector: b.sector, sectorLabel: sectorLabel(b.sector),
      m: b.tenor_yrs, dur: b.mod_dur, matDate: b.mat_date, offerDate: b.offer_date,
      ytm: b.ytm_pct, aeyBase: b.aey_base_pct, aey: b.aey_base_risk_adj_pct,
      gs: b.g_spread_bp, fair: b.implied_fair_spread_bp, gap,
      rating: b.rating, ratingClass: ratingClass(b.rating),
      // pd_rating is exported as a fraction (0.0598); DATA.pd_by_rating is the
      // same number in percent (5.98). Kept in percent here, like every other
      // rate on the page.
      pdRating: b.pd_rating == null ? null : b.pd_rating * 100,
      decomposition: b.aey_risk_decomposition,
      score: pillarScore(iss && iss.six_pillars),
      vol: b.volume, trades: b.num_trades, coupon: b.coupon_pct, couponType: b.coupon_type,
      price: b.last_pct, yieldSource: b.yield_source, yieldZone: b.yield_zone,
      vdo: b.vdo_scope === true, watch: b.in_watchlist === true,
      issuer: iss, passiveIssuer: pass, recentSerious, flagsToday,
      raw: b,
    };
    bond.flags = bondFlags(bond);
    return bond;
  });
})();

const BOND_BY_SECID = new Map(BONDS.map(b => [b.secid, b]));

// Short, highest-priority-first. The first one is what the screener's Флаг
// column shows, so the zone verdict leads: a bond can carry a 300% AEY and
// still be one the model says to stay away from, and the number alone does
// not say that.
function bondFlags(b) {
  const f = [];
  if (b.yieldZone === 'negative_ev') f.push('красная зона');
  else if (b.yieldZone === 'caution') f.push('жёлтая зона');
  if (b.watch) f.push('наблюдение');
  if (b.recentSerious.length || b.flagsToday.length) f.push('событие');
  if (b.gap != null && b.gap > CANDIDATE_GAP_BP && b.yieldZone === 'green') f.push('кандидат');
  if (b.vol != null && b.vol < LOW_LIQ_VOLUME_RUB) f.push('низкая ликв.');
  if (b.yieldSource && b.yieldSource !== 'moex') f.push('цена искажена');
  return f;
}

// 'выше' / 'в модели' / 'ниже' / null (no rating -> no fair spread)
function gapBand(b) {
  if (b.gap == null) return null;
  return b.gap > FAIR_BAND_BP ? 'above' : b.gap < -FAIR_BAND_BP ? 'below' : 'fair';
}

// Russian money: 1 234 ₽ / 56 тыс. ₽ / 1,2 млн ₽
function fmtRub(v) {
  if (v == null || !isFinite(v)) return DASH;
  if (v >= 1e6) return `${fmtNum(v / 1e6, 1)} млн ₽`;
  if (v >= 1e4) return `${fmtNum(v / 1e3, 0)} тыс. ₽`;
  return `${fmtNum(v, 0)} ₽`;
}

function fmtBpSigned(v) {
  return v == null || !isFinite(v) ? DASH : `${v > 0 ? '+' : v < 0 ? '−' : ''}${fmtNum(Math.abs(Math.round(v)), 0)}`;
}

// Basket ("Отобрано в корзину") shared with the Портфель page (stage 3).
// Kept per browser in localStorage; the page works without it.
const BASKET = (() => {
  const KEY = 'kl.basket';
  let ids = [];
  try { ids = JSON.parse(localStorage.getItem(KEY) || '[]').filter(id => BOND_BY_SECID.has(id)); } catch (e) { ids = []; }
  const listeners = [];
  function save() { try { localStorage.setItem(KEY, JSON.stringify(ids)); } catch (e) { /* storage unavailable */ } }
  function emit() { save(); listeners.forEach(fn => fn()); }
  return {
    ids: () => ids.slice(),
    has: id => ids.includes(id),
    toggle(id) { ids = ids.includes(id) ? ids.filter(x => x !== id) : ids.concat([id]); emit(); },
    clear() { ids = []; emit(); },
    onChange(fn) { listeners.push(fn); },
  };
})();
