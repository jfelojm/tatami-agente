/* Filtros compartidos: dropdowns con checkboxes, períodos, gráficos */
(function (w) {
  const MESES = ['', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic'];
  const DIAS_SEM = [
    { v: 1, n: 'Lun' }, { v: 2, n: 'Mar' }, { v: 3, n: 'Mié' },
    { v: 4, n: 'Jue' }, { v: 5, n: 'Vie' }, { v: 6, n: 'Sáb' }, { v: 7, n: 'Dom' },
  ];

  w.monthEnd = function (year, monthStr) {
    const y = parseInt(year, 10);
    const m = parseInt(monthStr, 10);
    return String(new Date(y, m, 0).getDate()).padStart(2, '0');
  };

  w.closeAllDropdowns = function () {
    document.querySelectorAll('.dd-menu.open').forEach(m => m.classList.remove('open'));
    document.querySelectorAll('.dd-btn.open').forEach(b => b.classList.remove('open'));
  };

  w.toggleDropdownMenu = function (wrapId) {
    const wrap = document.getElementById(wrapId);
    if (!wrap) return;
    const menu = wrap.querySelector('.dd-menu');
    const btn = wrap.querySelector('.dd-btn');
    if (!menu || !btn) return;
    const wasOpen = menu.classList.contains('open');
    w.closeAllDropdowns();
    if (!wasOpen) {
      menu.classList.add('open');
      btn.classList.add('open');
    }
  };

  document.addEventListener('click', e => {
    if (!e.target.closest('.dd-wrap')) w.closeAllDropdowns();
  });

  w.getCheckedValues = function (container) {
    if (!container) return [];
    const el = typeof container === 'string' ? document.getElementById(container) : container;
    if (!el) return [];
    return [...el.querySelectorAll('input[type=checkbox]:checked')].map(i => i.value);
  };

  w.updateDropdownLabel = function (containerId, labelId, prefix, items, opts) {
    opts = opts || {};
    const label = document.getElementById(labelId);
    const c = document.getElementById(containerId);
    if (!label || !c) return;
    if (!items || !items.length) {
      items = [...c.querySelectorAll('input[type=checkbox]')].map(i => ({
        v: i.value,
        n: (i.closest('label')?.textContent || i.value).trim(),
      }));
    }
    const checked = w.getCheckedValues(c);
    const total = items.length || c.querySelectorAll('input[type=checkbox]').length;
    if (!checked.length) {
      label.textContent = prefix + ': ' + (opts.emptyLabel || 'ninguno');
      return;
    }
    if (checked.length >= total) {
      label.textContent = prefix + ': todos';
      return;
    }
    if (checked.length <= 3) {
      const names = checked.map(v => (items.find(it => it.v === v) || {}).n || v);
      label.textContent = prefix + ': ' + names.join(', ');
      return;
    }
    label.textContent = prefix + ': ' + checked.length + ' sel.';
  };

  w.selectAllInDropdown = function (containerId, labelId, prefix, items, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    c.querySelectorAll('input[type=checkbox]').forEach(i => { i.checked = true; });
    w.updateDropdownLabel(containerId, labelId, prefix, items);
    if (onChange) onChange();
  };

  w.selectNoneInDropdown = function (containerId, labelId, prefix, items, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    c.querySelectorAll('input[type=checkbox]').forEach(i => { i.checked = false; });
    w.updateDropdownLabel(containerId, labelId, prefix, items);
    if (onChange) onChange();
  };

  w.buildCheckboxPanel = function (container, items, { allByDefault = true, labelId, prefix, onChange } = {}) {
    if (!container) return;
    const cid = container.id || 'panel';
    const pre = prefix || 'Items';
    const safeId = cid.replace(/[^a-z0-9]/gi, '_');
    const onChg = () => {
      if (labelId) w.updateDropdownLabel(cid, labelId, pre, items);
      if (onChange) onChange();
    };
    const rows = items.map(it => {
      const id = cid + '-cb-' + String(it.v).replace(/[^a-z0-9]/gi, '_');
      const chk = allByDefault ? ' checked' : '';
      return `<label class="chk-item" for="${id}"><input type="checkbox" id="${id}" value="${it.v}"${chk}> ${it.n}</label>`;
    }).join('');
    const actions = labelId ? `<div class="dd-actions">
      <button type="button" onclick="event.stopPropagation(); selectAllInDropdown('${cid}','${labelId}','${pre}',null,window._ddOnChange_${safeId})">Todos</button>
      <button type="button" onclick="event.stopPropagation(); selectNoneInDropdown('${cid}','${labelId}','${pre}',null,window._ddOnChange_${safeId})">Ninguno</button>
    </div>` : '';
    container.innerHTML = rows + actions;
    window['_ddOnChange_' + safeId] = onChange;
    container.querySelectorAll('input[type=checkbox]').forEach(inp => {
      inp.addEventListener('change', onChg);
    });
    if (labelId) w.updateDropdownLabel(cid, labelId, pre, items);
  };

  w.filterSearchablePanel = function (containerId, query) {
    const list = document.getElementById(containerId + '-list');
    if (!list) return;
    const q = (query || '').toLowerCase().trim();
    list.querySelectorAll('.chk-item').forEach(el => {
      const t = el.getAttribute('data-search') || (el.textContent || '').toLowerCase();
      el.style.display = !q || t.includes(q) ? '' : 'none';
    });
  };

  w.buildSearchableCheckboxPanel = function (container, items, opts) {
    opts = opts || {};
    if (!container) return;
    if (!items || !items.length) {
      container.innerHTML = '<div style="padding:12px;font-size:12px;color:#888780">Sin opciones disponibles</div>';
      return;
    }
    const cid = container.id || 'panel';
    const pre = opts.prefix || 'Items';
    const safeId = cid.replace(/[^a-z0-9]/gi, '_');
    const placeholder = opts.searchPlaceholder || 'Buscar…';
    const onChg = () => {
      if (opts.labelId) w.updateDropdownLabel(cid, opts.labelId, pre, items, { emptyLabel: opts.emptyLabel });
      if (opts.onChange) opts.onChange();
    };
    const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    const rows = items.map(it => {
      const id = cid + '-cb-' + String(it.v).replace(/[^a-z0-9]/gi, '_');
      const chk = opts.allByDefault ? ' checked' : '';
      const searchText = (it.n + ' ' + (it.search || it.v)).toLowerCase();
      return `<label class="chk-item" data-search="${esc(searchText)}" for="${id}"><input type="checkbox" id="${id}" value="${esc(it.v)}"${chk}> ${esc(it.n)}</label>`;
    }).join('');
    const actions = opts.labelId ? `<div class="dd-actions">
      <button type="button" onclick="event.stopPropagation(); selectAllInDropdown('${cid}','${opts.labelId}','${pre}',null,window._ddOnChange_${safeId})">Todos</button>
      <button type="button" onclick="event.stopPropagation(); selectNoneInDropdown('${cid}','${opts.labelId}','${pre}',null,window._ddOnChange_${safeId})">Ninguno</button>
    </div>` : '';
    container.innerHTML =
      `<input type="text" class="dd-search" placeholder="${esc(placeholder)}" onclick="event.stopPropagation()" oninput="filterSearchablePanel('${cid}', this.value)">` +
      actions +
      `<div class="dd-list" id="${cid}-list">${rows}</div>`;
    window['_ddOnChange_' + safeId] = opts.onChange;
    container.querySelectorAll('input[type=checkbox]').forEach(inp => {
      inp.addEventListener('change', onChg);
    });
    if (opts.labelId) w.updateDropdownLabel(cid, opts.labelId, pre, items, { emptyLabel: opts.emptyLabel });
  };

  w.initMesesPanel = function (year, containerId, labelId, onChange, selectedValues) {
    const c = document.getElementById(containerId);
    if (!c) return;
    const items = [];
    for (let m = 1; m <= 12; m++) {
      const mm = String(m).padStart(2, '0');
      items.push({ v: `${year}-${mm}`, n: MESES[m] });
    }
    const sel = selectedValues == null
      ? items.map(i => i.v)
      : selectedValues.filter(v => String(v).startsWith(year + '-'));
    w.buildCheckboxPanel(c, items, { allByDefault: false, labelId, prefix: 'Meses', onChange });
    c.querySelectorAll('input[type=checkbox]').forEach(inp => {
      inp.checked = sel.includes(inp.value);
    });
    if (labelId) w.updateDropdownLabel(containerId, labelId, 'Meses', items);
  };

  w.initDiasSemPanel = function (containerId, labelId, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    const items = DIAS_SEM.map(d => ({ v: String(d.v), n: d.n }));
    w.buildCheckboxPanel(c, items, { allByDefault: true, labelId, prefix: 'Días sem.', onChange });
  };

  w.initPvPanel = function (containerId, labelId, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    const items = [
      { v: 'BARRA', n: 'Barra' }, { v: 'COCINA', n: 'Cocina' }, { v: 'OTRO', n: 'Otro' },
    ];
    w.buildCheckboxPanel(c, items, { allByDefault: true, labelId, prefix: 'P. venta', onChange });
  };

  w.isoWeekKeyFromDate = function (date) {
    const d = new Date(date.getTime());
    d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() + 3 - ((d.getDay() + 6) % 7));
    const week1 = new Date(d.getFullYear(), 0, 4);
    const weekNum = 1 + Math.round(
      ((d - week1) / 86400000 - 3 + ((week1.getDay() + 6) % 7)) / 7
    );
    return `${d.getFullYear()}-W${String(weekNum).padStart(2, '0')}`;
  };

  w.weekLabelsForYear = function (year, hastaIso) {
    const y = parseInt(year, 10);
    if (!y) return [];
    const end = hastaIso ? new Date(hastaIso + 'T12:00:00') : new Date(y, 11, 31);
    const start = new Date(y, 0, 1);
    const seen = new Set();
    const labels = [];
    for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
      const key = w.isoWeekKeyFromDate(d);
      if (parseInt(key.slice(0, 4), 10) === y && !seen.has(key)) {
        seen.add(key);
        labels.push(key);
      }
    }
    return labels.sort();
  };

  w.isoWeekMonday = function (isoYear, week) {
    const simple = new Date(isoYear, 0, 1 + (week - 1) * 7);
    const dow = simple.getDay();
    const monday = new Date(simple);
    if (dow <= 4) monday.setDate(simple.getDate() - simple.getDay() + 1);
    else monday.setDate(simple.getDate() + 8 - simple.getDay());
    return monday;
  };

  w.weekKeysToDateRange = function (weekKeys) {
    if (!weekKeys || !weekKeys.length) return null;
    let minD = null;
    let maxD = null;
    weekKeys.forEach(k => {
      const m = /^(\d{4})-W(\d{2})$/.exec(String(k));
      if (!m) return;
      const mon = w.isoWeekMonday(parseInt(m[1], 10), parseInt(m[2], 10));
      const sun = new Date(mon);
      sun.setDate(sun.getDate() + 6);
      if (!minD || mon < minD) minD = mon;
      if (!maxD || sun > maxD) maxD = sun;
    });
    if (!minD || !maxD) return null;
    const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    return { desde: fmt(minD), hasta: fmt(maxD) };
  };

  w.syncSemanasFromData = function (labels, containerId, labelId, onChange, selectedValues) {
    const c = document.getElementById(containerId);
    if (!c || !labels || !labels.length) return;
    const items = labels.map(l => ({ v: l, n: l.replace('-W', ' S') }));
    const sel = selectedValues;
    const allDefault = sel == null;
    w.buildCheckboxPanel(c, items, { allByDefault: allDefault, labelId, prefix: 'Semanas', onChange });
    if (sel && sel.length) {
      c.querySelectorAll('input[type=checkbox]').forEach(inp => {
        inp.checked = sel.includes(inp.value);
      });
      if (labelId) w.updateDropdownLabel(containerId, labelId, 'Semanas', items);
    }
  };

  w.initSemanasPanel = function (year, containerId, labelId, onChange, selectedValues, hastaIso) {
    const labels = w.weekLabelsForYear(year, hastaIso);
    w.syncSemanasFromData(labels, containerId, labelId, onChange, selectedValues);
  };

  w.syncDiasFromData = function (labels, containerId, labelId, onChange, selectedValues) {
    const c = document.getElementById(containerId);
    if (!c || !labels || !labels.length) return;
    const items = labels.map(l => ({ v: l, n: l.slice(8) + '/' + l.slice(5, 7) }));
    const sel = selectedValues;
    const allDefault = sel == null;
    w.buildCheckboxPanel(c, items, { allByDefault: allDefault, labelId, prefix: 'Días', onChange });
    if (sel && sel.length) {
      c.querySelectorAll('input[type=checkbox]').forEach(inp => {
        inp.checked = sel.includes(inp.value);
      });
      if (labelId) w.updateDropdownLabel(containerId, labelId, 'Días', items);
    }
  };

  w.dayLabelsForMonth = function (year, monthStr, hastaIso) {
    const y = parseInt(year, 10);
    const m = parseInt(monthStr, 10);
    if (!y || !m) return [];
    const mm = String(m).padStart(2, '0');
    let maxDay = parseInt(w.monthEnd(String(y), mm), 10);
    if (hastaIso) {
      const parts = String(hastaIso).slice(0, 10).split('-');
      if (parts.length >= 3) {
        const hy = parseInt(parts[0], 10);
        const hm = parseInt(parts[1], 10);
        const hd = parseInt(parts[2], 10);
        if (hy === y && hm === m && hd > 0 && hd < maxDay) maxDay = hd;
      }
    }
    const labels = [];
    for (let d = 1; d <= maxDay; d++) {
      labels.push(`${y}-${mm}-${String(d).padStart(2, '0')}`);
    }
    return labels;
  };

  w.initDiasPanel = function (year, monthStr, containerId, labelId, onChange, selectedValues, hastaIso) {
    const labels = w.dayLabelsForMonth(year, monthStr, hastaIso);
    w.syncDiasFromData(labels, containerId, labelId, onChange, selectedValues);
  };

  w.pvIncludesCheck = function (containerId, key) {
    const sel = w.getCheckedValues(document.getElementById(containerId));
    return sel.length === 0 || sel.includes(key);
  };

  w.filterLabelsData = function (labels, datasets, selectedKeys) {
    if (!labels || !selectedKeys || selectedKeys.length === 0) return { labels, datasets };
    const idx = labels.map((l, i) => selectedKeys.includes(l) ? i : -1).filter(i => i >= 0);
    const nl = idx.map(i => labels[i]);
    const nd = datasets.map(ds => ({ ...ds, data: idx.map(i => ds.data[i]) }));
    return { labels: nl, datasets: nd };
  };

  w.rangeFromMeses = function (checked, year) {
    if (!checked.length) {
      return { desde: `${year}-01-01`, hasta: `${year}-12-31` };
    }
    const sorted = [...checked].sort();
    const first = sorted[0];
    const last = sorted[sorted.length - 1];
    const lm = last.split('-')[1];
    const ly = last.split('-')[0];
    return { desde: `${first}-01`, hasta: `${ly}-${lm}-${w.monthEnd(ly, lm)}` };
  };

  w.drawPieChart = function (canvasId, chartRef, labels, values, colors, opts) {
    opts = opts || {};
    const fmt = opts.format || 'money';
    const legendPosition = opts.legendPosition || 'right';
    const el = document.getElementById(canvasId);
    if (!el) return chartRef;
    if (chartRef && chartRef.destroy) chartRef.destroy();
    const pal = colors || ['#378ADD', '#1D9E75', '#BA7517', '#85B7EB', '#5DCAA5', '#888780', '#a32d2d', '#0c447c'];
    const nums = (values || []).map(v => Number(v) || 0);
    const total = nums.reduce((a, b) => a + b, 0) || 1;
    const shortLbl = (s, max) => {
      const n = max || (legendPosition === 'bottom' ? 32 : 20);
      s = String(s || '');
      return s.length > n ? s.slice(0, n - 1) + '…' : s;
    };
    const legendLine = (lbl, v) => {
      const pct = ((Number(v) || 0) / total * 100).toFixed(1);
      if (fmt === 'pct') return `${shortLbl(lbl)}: ${pct}%`;
      return `${shortLbl(lbl)}: $${Number(v).toLocaleString('es-EC')} · ${pct}%`;
    };
    return new Chart(el, {
      type: 'pie',
      data: {
        labels,
        datasets: [{ data: nums, backgroundColor: labels.map((_, i) => pal[i % pal.length]) }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: 6 },
        plugins: {
          legend: {
            position: legendPosition,
            labels: {
              boxWidth: 12,
              font: { size: 10 },
              padding: 10,
              generateLabels: chart => {
                const ds = chart.data.datasets[0];
                return chart.data.labels.map((lbl, i) => ({
                  text: legendLine(lbl, ds.data[i]),
                  fillStyle: ds.backgroundColor[i],
                  hidden: false,
                  index: i,
                }));
              },
            },
          },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = Number(ctx.parsed) || 0;
                const pct = (v / total * 100).toFixed(1);
                if (fmt === 'pct') return `${ctx.label}: ${v.toFixed(1)}%`;
                return `${ctx.label}: $${v.toLocaleString('es-EC')} (${pct}%)`;
              },
            },
          },
        },
      },
    });
  };

  w.drawBarLineChart = function (canvasId, chartRef, labels, barDatasets, lineData, lineLabel, opts) {
    opts = opts || {};
    const fmt = opts.format || 'money';
    const el = document.getElementById(canvasId);
    if (!el) return chartRef;
    if (chartRef && chartRef.destroy) chartRef.destroy();
    const datasets = [...barDatasets];
    barDatasets.forEach((ds, i) => {
      datasets[i] = { ...ds, type: ds.type || 'bar', order: ds.order != null ? ds.order : 2 };
    });
    if (lineData) {
      datasets.push({
        type: 'line', label: lineLabel || 'Total', data: lineData,
        borderColor: '#BA7517', backgroundColor: 'transparent', borderWidth: 2, pointRadius: 3, tension: 0.3, order: 0,
      });
    }
    const stacked = barDatasets.some(ds => ds.stack);
    const yTick = fmt === 'pct'
      ? v => Number(v).toFixed(0) + '%'
      : v => '$' + Number(v).toLocaleString();
    return new Chart(el, {
      type: 'bar',
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: true, labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.parsed.y;
                if (fmt === 'pct') return `${ctx.dataset.label}: ${Number(v).toFixed(1)}%`;
                return `${ctx.dataset.label}: $${Number(v).toLocaleString('es-EC')}`;
              },
              footer: items => {
                if (!items.length || !barDatasets[0]?.stack) return '';
                const barItems = items.filter(i => i.dataset.type === 'bar' && i.parsed.y != null);
                if (barItems.length < 2) return '';
                const sum = barItems.reduce((s, i) => s + Number(i.parsed.y || 0), 0);
                return fmt === 'pct' ? '' : `Total barra: $${sum.toLocaleString('es-EC')}`;
              },
            },
          },
        },
        scales: {
          x: { stacked, ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
          y: { stacked, ticks: { callback: yTick } },
        },
      },
    });
  };

  w.drawFiltroTrendChart = function (canvasId, chartRef, filtroTrend, opts) {
    opts = opts || {};
    const el = document.getElementById(canvasId);
    if (!el) return chartRef;
    if (chartRef && chartRef.destroy) chartRef.destroy();
    if (!filtroTrend || !filtroTrend.series || !filtroTrend.series.length) return null;
    const pal = ['#378ADD', '#1D9E75', '#BA7517', '#85B7EB', '#5DCAA5', '#a32d2d', '#0c447c', '#888780'];
    const labels = filtroTrend.labels || [];
    const datasets = filtroTrend.series.map((s, i) => ({
      type: 'line',
      label: s.nombre,
      data: s.vta || [],
      borderColor: pal[i % pal.length],
      backgroundColor: 'transparent',
      borderWidth: 2,
      pointRadius: 3,
      tension: 0.3,
    }));
    return new Chart(el, {
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: ctx => `${ctx.dataset.label}: $${Number(ctx.parsed.y || 0).toLocaleString('es-EC')}`,
            },
          },
        },
        scales: {
          x: { grid: { color: 'rgba(128,128,128,0.08)' }, ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
          y: { grid: { color: 'rgba(128,128,128,0.08)' }, ticks: { callback: v => '$' + Number(v).toLocaleString() } },
        },
      },
    });
  };

  w.toggleCollapse = function (headEl) {
    const body = headEl.nextElementSibling;
    if (body) {
      body.classList.toggle('open');
      headEl.classList.toggle('open');
    }
  };

  w.MESES = MESES;
})(window);
