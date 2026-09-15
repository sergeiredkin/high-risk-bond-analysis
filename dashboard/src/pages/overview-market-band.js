// "Рынок сегодня" -- the one real-data band on Обзор. NOT in the design
// reference: added at the captain's request (2026-09-15, "add life band,
// will see how it looks") and built from the reference's own vocabulary
// (capGroups section header + Портфель pfStats tiles). Self-contained: to
// remove it, delete this file and its INCLUDE line in template.html.

function renderOverviewMarketBand(el) {
  const now = DATA.universe_now || {};
  const bonds = now.bonds || [];

  // 1. Universe size
  const universeTile = {
    k: 'Вселенная', v: bonds.length ? fmtNum(bonds.length, 0) : DASH,
    note: now.snap_date ? `бумаг с котировкой МосБиржи на ${fmtDate(now.snap_date)}` : 'снимок вселенной не выгружен',
  };

  // 2. Median G-spread across the same universe, compared with the previous
  // exported snapshot (the export has no daily series -- see
  // export_dashboard_data.py's universe_now/_week_ago buckets).
  const medG = median(bonds.map(b => b.g_spread_bp));
  const prev = DATA.universe_week_ago || {};
  const medPrev = prev.snap_date && prev.snap_date !== now.snap_date
    ? median((prev.bonds || []).map(b => b.g_spread_bp)) : null;
  const spreadTile = {
    k: 'Медианный G-спред', v: medG != null ? `${fmtNum(Math.round(medG), 0)} б.п.` : DASH,
    note: medPrev != null
      ? `снимок ${fmtDate(prev.snap_date)}: ${fmtNum(Math.round(medPrev), 0)} б.п.`
      : 'предыдущего снимка для сравнения нет',
  };

  // 3. Detector flags on today's snapshot
  const asOf = DATA.day2_snap_date;
  const issuers = (DATA.issuers || []).filter(i => i.snap_date === asOf);
  const flags = issuers.reduce((s, i) => s + (i.flags_today || []).length, 0);
  const flagged = issuers.filter(i => (i.flags_today || []).length).length;
  const flagsTile = {
    k: 'Флаги сегодня', v: asOf ? fmtNum(flags, 0) : DASH,
    note: asOf
      ? `у ${flagged} из ${issuers.length} ${plural(issuers.length, ['эмитента', 'эмитентов', 'эмитентов'])} в мониторинге · ${fmtDate(asOf)}`
      : 'дата снимка не выгружена',
  };

  // 4. Last CBR decision on record. A past meeting with no rate_after means
  // the decision was not captured -- say so instead of implying 14% is fresh.
  const meetings = ((DATA.cbr && DATA.cbr.meetings) || []).filter(m => m.is_past)
    .sort((a, b) => b.date.localeCompare(a.date));
  const decided = meetings.find(m => m.rate_after != null);
  const missing = decided ? meetings.filter(m => m.rate_after == null && m.date > decided.date) : [];
  let cbrTile = { k: 'Последнее решение ЦБ', v: DASH, note: 'решений ЦБ в выгрузке нет' };
  if (decided) {
    const bp = decided.rate_change_bp;
    const v = bp == null ? fmtPct(decided.rate_after) : bp === 0 ? 'без изменений' : `${bp > 0 ? '+' : '−'}${Math.abs(bp)} б.п.`;
    let note = `${fmtDate(decided.date)} · ставка ${fmtPct(decided.rate_after)}`;
    if (missing.length) note += ` · решение ${fmtDate(missing[0].date)} не загружено`;
    cbrTile = { k: 'Последнее решение ЦБ', v, note };
  }

  const tiles = [universeTile, spreadTile, flagsTile, cbrTile];
  el.className = 'group';
  el.innerHTML = `
    <div class="group-head">
      <div class="group-title">Рынок сегодня</div>
      <div class="group-rule"></div>
      <div class="group-count">${asOf ? 'снимок ' + fmtDate(asOf) : ''}</div>
    </div>
    <div class="stat-grid">
      ${tiles.map((t, i) => `<div class="stat${i === 0 ? ' is-green' : ''}">
        <div class="stat-k">${escapeHtml(t.k)}</div>
        <div class="stat-v">${escapeHtml(t.v)}</div>
        <div class="stat-note">${escapeHtml(t.note)}</div>
      </div>`).join('')}
    </div>`;
  el.hidden = false;
}
