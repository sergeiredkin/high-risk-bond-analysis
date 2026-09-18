// "Изменения рейтингов" -- the rating-change alert on Обзор. NOT in the design
// reference: added at the captain's request (2026-09-17, "every change in
// rating is reported and notified in the app"), built from the same
// vocabulary as the market band next to it (group header + stat tiles) and
// the Данные feed rows. Data: DATA.rating_changes (export_dashboard_data.
// rating_changes_export), helpers in src/bonds.js.
//
// Order is by prominence, not date: defaults first, then downgrades on bonds
// held in a portfolio or on the ВДО screen, then other downgrades and
// withdrawals, then upgrades and new ratings. Each row opens the bond.

const RC_BAND_ROWS = 8;

function renderOverviewRatingBand(el) {
  const meta = DATA.rating_changes || {};
  const asOf = (DATA.universe_now && DATA.universe_now.snap_date) || DATA.day2_snap_date;
  const recent = ratingChangesRecent(RATING_CHANGES, asOf);
  const count = kinds => recent.filter(c => kinds.includes(c.kind)).length;

  const tiles = [
    { k: 'Дефолты', n: count(['default']), note: 'рейтинг D у агентства' },
    { k: 'Понижения', n: count(['downgrade']), note: 'грейд агентства ниже прежнего' },
    { k: 'Отзывы и снятия', n: count(['withdrawn', 'suspended', 'dropped']), note: 'рейтинга агентства больше нет' },
    { k: 'Повышения и новые', n: count(['upgrade', 'new', 'changed']), note: 'грейд выше или присвоен' },
  ];

  const rows = recent.slice()
    .sort((a, b) => ratingChangeProminence(a) - ratingChangeProminence(b)
      || String(b.detected_on).localeCompare(String(a.detected_on)))
    .slice(0, RC_BAND_ROWS)
    .map(c => {
      const bond = ratingChangeBond(c);
      const touched = (c.secids || []).map(s => BOND_BY_SECID.get(s)).filter(Boolean);
      const held = touched.filter(b => b.held).length;
      const vdo = touched.filter(b => b.vdo).length;
      const sev = ratingChangeSeverity(c);
      const [sevLabel, sevCls] = (typeof D_SEVERITY !== 'undefined' && D_SEVERITY[sev]) || [sev, 'ev-mid'];
      const chips = [
        `<span class="d-ev-chip ${sevCls}">${escapeHtml(RATING_KIND_LABEL[c.kind] || c.kind)}</span>`,
        held ? `<span class="d-ev-chip ev-hot">в портфеле · ${held}</span>` : '',
        vdo ? `<span class="d-ev-chip ev-plain">ВДО · ${vdo}</span>` : '',
        `<span class="d-ev-chip ev-plain">${touched.length ? `${touched.length} ${plural(touched.length, ['бумага', 'бумаги', 'бумаг'])}` : 'нет бумаг в снимке'}</span>`,
        c.level === 'issue' ? '<span class="d-ev-chip ev-plain">выпуск</span>' : '',
        `<span class="d-ev-chip ev-src">решение ${escapeHtml(fmtDate(c.rated_on))}</span>`,
      ].join('');
      const resolved = c.resolved_before !== c.resolved_after
        ? ` · итоговый рейтинг ${escapeHtml(c.resolved_before || 'нет')} → ${escapeHtml(c.resolved_after || 'нет')}` : '';
      return `<button type="button" class="rc-row${ratingChangeProminence(c) <= 2 ? ' is-loud' : ''}"${bond ? ` data-rc-secid="${escapeHtml(bond.secid)}"` : ''} title="${escapeHtml(sevLabel)}">
        <div class="d-ev-date">${fmtDayMonth(c.detected_on)}</div>
        <div class="d-ev-body">
          <div class="d-ev-text"><b>${escapeHtml(shortIssuerName(c.issuer) || c.issuer || '')}</b> — ${escapeHtml(ratingChangeText(c))}${resolved}</div>
          <div class="d-ev-chips">${chips}</div>
        </div>
      </button>`;
    }).join('');

  const since = meta.baseline_since ? fmtDate(meta.baseline_since) : null;
  const empty = since
    ? `С начала наблюдения (${escapeHtml(since)}) рейтинговых действий по бумагам вселенной не зафиксировано.`
    : 'Обновление рейтингов из MOEX ЦКИ ещё не запускалось — изменений не с чем сравнивать.';

  el.className = 'group';
  el.hidden = false;
  el.innerHTML = `
    <div class="group-head">
      <div class="group-title">Изменения рейтингов</div>
      <div class="group-rule"></div>
      <div class="group-count">за ${EVENT_LOOKBACK_DAYS} дней · ${fmtNum(recent.length, 0)}${since ? ` · наблюдение с ${escapeHtml(since)}` : ''}</div>
    </div>
    <div class="stat-grid">
      ${tiles.map((t, i) => `<div class="stat${t.n && i < 2 ? ' is-alert' : ''}">
        <div class="stat-k">${escapeHtml(t.k)}</div>
        <div class="stat-v">${fmtNum(t.n, 0)}</div>
        <div class="stat-note">${escapeHtml(t.note)}</div>
      </div>`).join('')}
    </div>
    ${recent.length ? `<div class="rc-list">${rows}</div>` : `<div class="stat-note">${empty}</div>`}
    <div class="rc-foot">
      <span>Сначала дефолты и понижения по бумагам в портфеле и на экране ВДО. Первая загрузка из MOEX — точка отсчёта: присвоенные раньше рейтинги сюда не попадают, попадают только расхождения с тем, что показывалось до неё.</span>
      ${RATING_CHANGES.length ? '<button type="button" class="pill-outline" data-rc-feed>Все изменения в ленте</button>' : ''}
    </div>`;

  el.addEventListener('click', e => {
    const row = e.target.closest('[data-rc-secid]');
    if (row) {
      const b = BOND_BY_SECID.get(row.dataset.rcSecid);
      if (b) openDetail(bondDetail(b));
      return;
    }
    if (e.target.closest('[data-rc-feed]')) {
      if (typeof D !== 'undefined') D.filter = 'Рейтинговые действия';
      goPage('data');
      if (typeof dRenderFeed === 'function' && document.getElementById('dFeedRows')) dRenderFeed();
    }
  });
}
