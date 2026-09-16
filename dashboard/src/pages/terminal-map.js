// Карта -- D3 scatter for the Карта и скринер page. Geometry, colours and the
// drag-a-frame gesture follow buildMap() in bond-lab-design-reference.html;
// the zoom/pan behaviour and the event-layering lessons come from the old
// D3 market map (dashboard/legacy/template.html, commits 660357d, f8a81d7).
//
// Gestures:
//   drag on the plot        draw a selection frame (the screener filters live)
//   click a point           open the bond drawer; Shift+click toggles the basket
//   wheel                   zoom around the cursor (1×–16×)
//   Shift+drag              pan
// The frame is stored in unzoomed plot coordinates, so zooming or panning
// never moves it off the bonds it selected.
//
// Faults the old maps had, and why they cannot recur here:
//   - stale pixel buffer after a page-zoom change (canvas map, a502a27): this
//     is SVG in a fixed viewBox, there is no bitmap to resync.
//   - pan stuck after a fast drag leaves the plot (canvas map, a2676d5): the
//     frame drag uses pointer capture and ends on pointerup, pointercancel or
//     lostpointercapture; the pan is d3.zoom's own gesture, which listens on
//     window.
//   - d3.brush's overlay swallowing every hover and click (660357d): there is
//     no d3.brush; the frame is a pointer-events:none <rect> drawn above the
//     points, and the gesture is bound on the <svg> itself.

const TerminalMap = (() => {
  const PL = 54, PR = 26, PT = 28, PB = 30;
  // The viewBox is sized to the container so one SVG unit is one CSS pixel:
  // a fixed 760-unit viewBox would magnify every label when the card is
  // wider (layout B is full-width), which the reference's own 760 assumes
  // away because its map never leaves the split.
  const MW_MIN = 520, MW_MAX = 1800;
  let MW = 760;
  const INK = '#201e1d', BG = '#f5ead8', ACC = '#c67139', ACC2 = '#7a8a5e', GREY = '#8a8478', MUTED = '#c0b6a5';
  const DUR_MAX = 8;             // modified duration axis, years
  const AXES = {
    // y domains are fixed so the picture does not jump between rebuilds;
    // bonds outside them are counted, not silently lost (see offAxisCount).
    spread: { y: [-500, 3000], yUnit: 'б.п.', yFmt: v => fmtNum(v, 0), title: 'Карта спредов' },
    aey: { y: [0, 60], yUnit: 'AEY', yFmt: v => fmtNum(v, 0) + '%', title: 'Доходность против качества' },
    curve: { y: [0, 55], yUnit: 'YTM', yFmt: v => fmtNum(v, 0) + '%', title: 'Кривая доходности' },
  };

  let ctl = null;                // controller from terminal.js
  let root = null, svg = null, tip = null;
  let H = 430, axis = 'spread', transform = null, zoom = null;
  let xBase = null, yBase = null;
  let drag = null;               // frame being drawn, unzoomed plot coords
  let base = new Map();          // secid -> [x, y] unzoomed, or absent if not plottable
  let rScale = null;
  let pending = 0;

  // ОФЗ curve. DATA.rf_curve keeps one bond per 0.1-year bucket ("last one
  // wins" in the exporter), so a bond carrying a fallback rf_rate makes the
  // published curve zigzag. Rebuilt here as the median of every bond's own
  // rf_rate_pct per bucket, and drawn only while a bucket holds enough bonds
  // for that median to mean anything -- the thin long end is left undrawn
  // rather than smoothed over. Bucketed by modified duration, not tenor, so
  // the curve lands on the same axis the bonds are plotted against.
  const CURVE_MIN_PER_TENOR = 8;
  const RF_CURVE = (() => {
    const buckets = new Map();
    BONDS.forEach(b => {
      const t = b.dur, r = b.raw.rf_rate_pct;
      if (t == null || r == null || t > DUR_MAX) return;
      const k = Math.round(t * 10) / 10;
      if (!buckets.has(k)) buckets.set(k, []);
      buckets.get(k).push(r);
    });
    const out = [];
    [...buckets.keys()].sort((a, b) => a - b).forEach(k => {
      const v = buckets.get(k);
      if (v.length < CURVE_MIN_PER_TENOR) return;
      if (out.length && k - out[out.length - 1][0] > 0.45) return;   // stop at the first real gap
      v.sort((a, b) => a - b);
      out.push([k, v.length % 2 ? v[v.length >> 1] : (v[(v.length >> 1) - 1] + v[v.length >> 1]) / 2]);
    });
    return out;
  })();

  const hasD3 = () => typeof d3 !== 'undefined';
  const plotW = () => MW - PL - PR;
  const durT = d => Math.log(d + 0.5);

  function mapHeight(layout) { return layout === 'A' ? 430 : 230; }
  function measureWidth() {
    const w = root ? Math.round(root.clientWidth) : 0;
    return w ? Math.max(MW_MIN, Math.min(MW_MAX, w)) : MW;
  }
  // x = PL + t·(MW − PL − PR): re-express a frame drawn at one width in the
  // coordinates of another, so a resize keeps the same bonds selected.
  function rescaleBrush(br, oldMW, newMW) {
    const o = oldMW - PL - PR, n = newMW - PL - PR;
    const f = x => PL + (x - PL) / o * n;
    return { x0: f(br.x0), x1: f(br.x1), y0: br.y0, y1: br.y1 };
  }

  // ── geometry ──────────────────────────────────────────────────────────
  function buildScales() {
    const ax = AXES[axis];
    xBase = axis === 'aey'
      ? d3.scaleLinear().domain([0, 10]).range([PL, MW - PR])
      : d3.scaleLinear().domain([durT(0), durT(DUR_MAX)]).range([PL, MW - PR]);
    yBase = d3.scaleLinear().domain(ax.y).range([H - PB, PT]);
    base = new Map();
    BONDS.forEach(b => {
      const xv = axis === 'aey' ? b.score : (b.dur != null && b.dur >= 0 && b.dur <= DUR_MAX ? durT(b.dur) : null);
      const yv = axis === 'spread' ? b.gs : axis === 'aey' ? b.aey : b.ytm;
      if (xv == null || yv == null || !isFinite(yv) || yv < ax.y[0] || yv > ax.y[1]) return;
      base.set(b.secid, [xBase(xv), yBase(yv)]);
    });
  }

  function offAxisCount() {
    // bonds that have the axis' values but fall outside the fixed domain
    const ax = AXES[axis];
    return BONDS.filter(b => {
      const yv = axis === 'spread' ? b.gs : axis === 'aey' ? b.aey : b.ytm;
      const hasX = axis === 'aey' ? b.score != null : b.dur != null;
      return hasX && yv != null && !base.has(b.secid) && isFinite(yv) && (yv < ax.y[0] || yv > ax.y[1] || b.dur > DUR_MAX);
    }).length;
  }

  function inBrush(b) {
    const br = drag || ctl.state.brush;
    if (!br) return true;
    const p = base.get(b.secid);
    if (!p) return false;
    return p[0] >= br.x0 && p[0] <= br.x1 && p[1] >= br.y0 && p[1] <= br.y1;
  }

  function norm(d) {
    return { x0: Math.min(d.x0, d.x1), x1: Math.max(d.x0, d.x1), y0: Math.min(d.y0, d.y1), y1: Math.max(d.y0, d.y1) };
  }

  // ── build (layout or axis change) ─────────────────────────────────────
  function mount(el, controller) {
    ctl = controller;
    root = el;
    if (!hasD3()) {
      root.innerHTML = `<div class="map-empty">Библиотека D3 не загрузилась (нужен доступ к cdnjs.cloudflare.com), поэтому карты нет. Скринер работает без неё.</div>`;
      return;
    }
    root.innerHTML = '';
    tip = document.createElement('div');
    tip.className = 'map-tip';
    tip.hidden = true;
    root.appendChild(tip);
    rScale = d3.scaleSqrt()
      .domain([0, d3.quantile(BONDS.map(b => b.vol || 0).sort(d3.ascending), 0.98) || 1])
      .range([3, 9]).clamp(true);
    rebuild(false);
    if (typeof ResizeObserver === 'function') {
      let raf = 0;
      new ResizeObserver(() => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          if (!root.clientWidth || measureWidth() === MW) return;   // hidden page, or nothing to do
          rebuild(false);
          ctl.onResize();
        });
      }).observe(root);
    }
  }

  function rebuild(animate) {
    if (!hasD3() || !root) return;
    const newH = mapHeight(ctl.state.layout), newMW = measureWidth();
    const layoutChanged = newH !== H || newMW !== MW || !svg;
    const prevPos = animate && !layoutChanged ? new Map(base) : null;
    if (newMW !== MW && ctl.state.brush) ctl.state.brush = rescaleBrush(ctl.state.brush, MW, newMW);
    H = newH;
    MW = newMW;
    axis = ctl.state.axis;
    drag = null;
    buildScales();
    if (layoutChanged) createSvg();
    transform = d3.zoomIdentity;
    zoom.extent([[PL, PT], [MW - PR, H - PB]]).translateExtent([[PL, PT], [MW - PR, H - PB]]);
    svg.property('__zoom', d3.zoomIdentity);
    drawGrid();
    drawOverlay();
    drawDots(prevPos);
    refresh();
  }

  function createSvg() {
    if (svg) svg.remove();
    svg = d3.select(root).append('svg')
      .attr('class', 'map-svg')
      .attr('viewBox', `0 0 ${MW} ${H}`)
      .attr('width', '100%');
    svg.append('defs').append('clipPath').attr('id', 'mapClip')
      .append('rect').attr('x', PL).attr('y', PT - 12).attr('width', plotW()).attr('height', H - PT - PB + 12);
    svg.append('g').attr('class', 'map-grid');
    const plot = svg.append('g').attr('clip-path', 'url(#mapClip)');
    plot.append('g').attr('class', 'map-overlay');
    plot.append('g').attr('class', 'map-dots');
    plot.append('g').attr('class', 'map-labels').attr('pointer-events', 'none');
    const brushG = svg.append('g').attr('pointer-events', 'none');
    brushG.append('rect').attr('class', 'map-brush').attr('rx', 10).attr('display', 'none');
    brushG.append('text').attr('class', 'map-brush-count').attr('display', 'none');

    zoom = d3.zoom()
      .scaleExtent([1, 16])
      // wheel always; mouse drag only with Shift (a plain drag draws the
      // frame); two-finger touch pans/zooms.
      .filter(e => e.type === 'wheel' || (e.type === 'mousedown' && e.shiftKey && e.button === 0) || e.type.startsWith('touch'))
      .on('zoom', e => {
        transform = e.transform;
        hideTip();
        position();
        ctl.onZoom(transform.k > 1.001 || Math.abs(transform.x) > 0.5 || Math.abs(transform.y) > 0.5);
      });
    svg.call(zoom).on('dblclick.zoom', null);

    const node = svg.node();
    node.addEventListener('pointerdown', onDown);
    node.addEventListener('pointermove', onMove);
    node.addEventListener('pointerup', onUp);
    node.addEventListener('pointercancel', onUp);
    node.addEventListener('lostpointercapture', onUp);
  }

  // ── frame gesture ─────────────────────────────────────────────────────
  function svgPoint(e) {
    const [x, y] = d3.pointer(e, svg.node());
    return transform.invert([x, y]);
  }
  function onDown(e) {
    if (e.button !== 0 || e.shiftKey || e.pointerType === 'touch') return;
    if (e.target.closest && e.target.closest('.map-dot')) return;
    e.preventDefault(); // also suppresses the compatibility mousedown, so d3.zoom does not pan
    const [x, y] = svgPoint(e);
    // capture can be refused (pointer already gone); the gesture still ends
    // on pointerup/cancel, so a failure here must not abort the drag
    try { svg.node().setPointerCapture(e.pointerId); } catch (err) { /* no capture */ }
    drag = { x0: x, y0: y, x1: x, y1: y, id: e.pointerId };
    hideTip();
  }
  function onMove(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const [x, y] = svgPoint(e);
    drag.x1 = x; drag.y1 = y;
    schedule();
  }
  function onUp(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    try { if (svg.node().hasPointerCapture(e.pointerId)) svg.node().releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
    const n = norm(d);
    // a click (or a tiny wiggle) on empty plot clears the frame, as in the reference
    const tooSmall = (n.x1 - n.x0) * transform.k < 8 || (n.y1 - n.y0) * transform.k < 8;
    ctl.setBrush(tooSmall ? null : { x0: n.x0, x1: n.x1, y0: n.y0, y1: n.y1 });
  }
  function schedule() {
    if (pending) return;
    pending = requestAnimationFrame(() => { pending = 0; ctl.onBrushDrag(); });
  }

  // ── drawing ───────────────────────────────────────────────────────────
  function drawDots(prevPos) {
    const dots = svg.select('.map-dots').selectAll('circle.map-dot')
      .data(BONDS, b => b.secid)
      .join(enter => enter.append('circle').attr('class', 'map-dot')
        .on('click', (e, b) => {
          e.stopPropagation();
          if (e.shiftKey) ctl.onTogglePick(b.secid); else ctl.onOpen(b.secid);
        })
        .on('pointerenter', (e, b) => showTip(e, b))
        .on('pointermove', e => moveTip(e))
        .on('pointerleave', hideTip));
    dots.attr('display', b => base.has(b.secid) ? null : 'none');
    if (prevPos) {
      // axis switch: glide from the old position where the bond had one
      dots.filter(b => base.has(b.secid) && prevPos.has(b.secid))
        .attr('cx', b => prevPos.get(b.secid)[0]).attr('cy', b => prevPos.get(b.secid)[1])
        .transition().duration(500).ease(d3.easeCubicOut)
        .attr('cx', b => base.get(b.secid)[0]).attr('cy', b => base.get(b.secid)[1]);
      dots.filter(b => base.has(b.secid) && !prevPos.has(b.secid)).call(placeDots);
    } else {
      dots.call(placeDots);
    }
  }

  function placeDots(sel) {
    sel.filter(b => base.has(b.secid))
      .attr('cx', b => transform.applyX(base.get(b.secid)[0]))
      .attr('cy', b => transform.applyY(base.get(b.secid)[1]));
  }

  function colorOf(b) {
    const band = gapBand(b);
    return band === 'above' ? ACC : band === 'below' ? GREY : band === 'fair' ? ACC2 : MUTED;
  }

  // Styles that depend on filter / frame / selection / basket.
  function refresh() {
    if (!hasD3() || !svg) return;
    const S = ctl.state;
    const on = new Set(ctl.visibleIds());
    svg.select('.map-dots').selectAll('circle.map-dot')
      .attr('r', b => rScale(b.vol || 0) + (S.sel === b.secid ? 3.5 : 0))
      .attr('fill', colorOf)
      .attr('fill-opacity', b => on.has(b.secid) ? (S.sel === b.secid ? 1 : 0.82) : 0.14)
      .attr('stroke', b => BASKET.has(b.secid) ? INK : BG)
      .attr('stroke-width', b => BASKET.has(b.secid) ? 2.2 : 1.2)
      .filter(b => S.sel === b.secid || BASKET.has(b.secid)).raise();
    drawBrush(on.size);
    drawLabels(on);
  }

  // Zoom/pan: move everything without restyling.
  function position() {
    svg.select('.map-dots').selectAll('circle.map-dot').interrupt().call(placeDots);
    drawGrid();
    drawOverlay();
    drawLabels(new Set(ctl.visibleIds()));
    drawBrush(null);
  }

  function drawGrid() {
    const g = svg.select('.map-grid');
    g.selectAll('*').remove();
    const ax = AXES[axis];
    const ys = transform.rescaleY(yBase);
    ys.ticks(ctl.state.layout === 'A' ? 7 : 4).forEach(v => {
      const y = ys(v);
      if (y < PT - 1 || y > H - PB + 1) return;
      g.append('line').attr('x1', PL - 6).attr('x2', MW - PR).attr('y1', y).attr('y2', y)
        .attr('stroke', INK).attr('stroke-opacity', v === 0 ? 0.2 : 0.07);
      g.append('text').attr('x', PL - 11).attr('y', y + 3.5).attr('class', 'map-tick').attr('text-anchor', 'end').text(ax.yFmt(v));
    });
    g.append('text').attr('x', 2).attr('y', 11).attr('class', 'map-axis-name').text(ax.yUnit);

    const xs = transform.rescaleX(xBase);
    if (axis === 'aey') {
      xs.ticks(6).forEach(v => {
        const x = xs(v);
        if (x < PL || x > MW - PR - 60) return;
        g.append('text').attr('x', x).attr('y', H - 9).attr('class', 'map-tick').attr('text-anchor', 'middle').text(fmtNum(v, v % 1 ? 1 : 0));
      });
      g.append('text').attr('x', MW - PR).attr('y', H - 9).attr('class', 'map-axis-name').attr('text-anchor', 'end').text('кредитный балл →');
    } else {
      let last = -Infinity;
      [0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 6, 7].forEach(v => {
        const x = xs(durT(v));
        if (x < PL + 4 || x > MW - PR - 64 || x - last < 34) return;
        last = x;
        g.append('text').attr('x', x).attr('y', H - 9).attr('class', 'map-tick').attr('text-anchor', 'middle').text(fmtNum(v, v % 1 ? (v * 4) % 1 ? 2 : v % 0.5 ? 2 : 1 : 0) + ' г.');
      });
      g.append('text').attr('x', MW - PR).attr('y', H - 9).attr('class', 'map-axis-name').attr('text-anchor', 'end').text('дюрация →');
    }
  }

  function drawOverlay() {
    const g = svg.select('.map-overlay');
    g.selectAll('*').remove();
    const xs = transform.rescaleX(xBase), ys = transform.rescaleY(yBase);
    if (axis === 'curve') {
      const pts = RF_CURVE.map(([t, r]) => [xs(durT(t)), ys(r)]);
      if (pts.length < 2) return;
      const line = d3.line().curve(d3.curveMonotoneX);
      const bottom = ys(AXES.curve.y[0]);
      g.append('path').attr('d', line(pts) + `L${pts[pts.length - 1][0]},${bottom}L${pts[0][0]},${bottom}Z`)
        .attr('fill', ACC).attr('fill-opacity', 0.09);
      g.append('path').attr('d', line(pts)).attr('fill', 'none').attr('stroke', ACC)
        .attr('stroke-width', 2.75).attr('stroke-linecap', 'round');
      const end = pts[pts.length - 1];
      g.append('text').attr('x', Math.min(end[0] + 8, MW - PR)).attr('y', end[1] - 12).attr('class', 'map-curve-label')
        .attr('text-anchor', end[0] + 8 > MW - PR - 70 ? 'end' : 'start')
        .text(`кривая ОФЗ · до ${fmtNum(RF_CURVE[RF_CURVE.length - 1][0], 1)} г.`);
    } else if (axis === 'spread') {
      // Fair spread is a function of the rating, not of duration
      // (PD × LGD per notch, DATA.fair_value_bp_by_tier), so it is drawn as
      // one dashed level per rating class rather than a sloped band.
      const tiers = Object.entries(DATA.fair_value_bp_by_tier || {}).sort((a, b) => b[1] - a[1]);
      let lastY = -Infinity;
      tiers.forEach(([tier, bp]) => {
        const y = ys(bp);
        if (y < PT || y > H - PB) return;
        g.append('line').attr('x1', PL).attr('x2', MW - PR).attr('y1', y).attr('y2', y)
          .attr('stroke', ACC2).attr('stroke-width', 2).attr('stroke-dasharray', '6 5').attr('stroke-opacity', 0.7);
        if (y - lastY >= 16) {
          g.append('text').attr('x', MW - PR - 2).attr('y', y - 5).attr('class', 'map-fair-label').attr('text-anchor', 'end')
            .text(`справедливый спред · ${tier}`);
          lastY = y;
        }
      });
    }
  }

  function drawLabels(on) {
    const g = svg.select('.map-labels');
    g.selectAll('*').remove();
    const S = ctl.state;
    const shown = new Set();
    if (S.layout === 'A' && axis !== 'curve') {
      // the most liquid bonds in view get a name, like the reference's liq > 800 rule
      BONDS.filter(b => on.has(b.secid) && base.has(b.secid))
        .filter(b => {
          const x = transform.applyX(base.get(b.secid)[0]), y = transform.applyY(base.get(b.secid)[1]);
          return x >= PL && x <= MW - PR - 40 && y >= PT && y <= H - PB;
        })
        .sort((a, b) => (b.vol || 0) - (a.vol || 0)).slice(0, 8)
        .forEach(b => shown.add(b.secid));
    }
    if (S.sel && base.has(S.sel)) shown.add(S.sel);
    shown.forEach(id => {
      const b = BOND_BY_SECID.get(id), p = base.get(id);
      const sel = id === S.sel;
      g.append('text')
        .attr('x', transform.applyX(p[0]) + rScale(b.vol || 0) + (sel ? 3.5 : 0) + 5)
        .attr('y', transform.applyY(p[1]) + 3.5)
        .attr('class', 'map-dot-label').attr('fill-opacity', sel ? 0.9 : 0.42)
        .text(b.t);
    });
  }

  function drawBrush(count) {
    const br = drag ? norm(drag) : ctl.state.brush;
    const rect = svg.select('.map-brush'), text = svg.select('.map-brush-count');
    if (!br) { rect.attr('display', 'none'); text.attr('display', 'none'); return; }
    const x0 = transform.applyX(br.x0), x1 = transform.applyX(br.x1);
    const y0 = transform.applyY(br.y0), y1 = transform.applyY(br.y1);
    rect.attr('display', null).attr('x', x0).attr('y', y0).attr('width', Math.max(0, x1 - x0)).attr('height', Math.max(0, y1 - y0));
    text.attr('display', null).attr('x', x0 + 8).attr('y', y0 - 6 < 14 ? y1 + 16 : y0 - 7);
    if (count != null) text.text(`${fmtNum(count, 0)} ${plural(count, ['бумага', 'бумаги', 'бумаг'])} в выделении`);
  }

  // ── tooltip ───────────────────────────────────────────────────────────
  function showTip(e, b) {
    if (drag) return;
    tip.innerHTML = `
      <div class="map-tip-title">${escapeHtml(b.t)}</div>
      <div class="map-tip-sub">${escapeHtml(b.n)}${b.rating ? ' · ' + escapeHtml(b.rating) : ''}</div>
      <div class="map-tip-row"><span>YTM ${fmtPct(b.ytm, 1)}</span><span>AEY ${fmtPct(b.aey, 1)}</span></div>
      <div class="map-tip-row"><span>G-спред ${b.gs != null ? fmtBpSigned(b.gs) + ' б.п.' : DASH}</span><span>дюр. ${b.dur != null ? fmtNum(b.dur, 1) + ' г.' : DASH}</span></div>`;
    tip.hidden = false;
    moveTip(e);
  }
  function moveTip(e) {
    if (tip.hidden) return;
    const r = root.getBoundingClientRect();
    let x = e.clientX - r.left + 14, y = e.clientY - r.top + 14;
    if (x + tip.offsetWidth > r.width) x = e.clientX - r.left - 14 - tip.offsetWidth;
    if (y + tip.offsetHeight > r.height) y = e.clientY - r.top - 14 - tip.offsetHeight;
    tip.style.left = Math.max(0, x) + 'px';
    tip.style.top = Math.max(0, y) + 'px';
  }
  function hideTip() { if (tip) tip.hidden = true; }

  function resetZoom() {
    if (svg && zoom) svg.transition().duration(250).call(zoom.transform, d3.zoomIdentity);
  }

  return {
    mount,
    rebuild(animate) { rebuild(animate); },
    refresh,
    inBrush: b => (hasD3() ? inBrush(b) : true),
    isDragging: () => !!drag,
    plottedCount: () => base.size,
    offAxisCount: () => (hasD3() ? offAxisCount() : 0),
    title: a => AXES[a].title,
    resetZoom,
    colors: { ACC, ACC2, GREY, MUTED },
  };
})();
