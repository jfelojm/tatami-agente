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

  w.updateDropdownLabel = function (containerId, labelId, prefix, items) {
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
      label.textContent = prefix + ': ninguno';
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

  w.syncSemanasFromData = function (labels, containerId, labelId, onChange) {
    const c = document.getElementById(containerId);
    if (!c || !labels) return;
    const items = labels.map(l => ({ v: l, n: l.replace('-W', ' S') }));
    w.buildCheckboxPanel(c, items, { allByDefault: true, labelId, prefix: 'Semanas', onChange });
  };

  w.syncDiasFromData = function (labels, containerId, labelId, onChange) {
    const c = document.getElementById(containerId);
    if (!c || !labels) return;
    const items = labels.map(l => ({ v: l, n: l.slice(8) + '/' + l.slice(5, 7) }));
    w.buildCheckboxPanel(c, items, { allByDefault: true, labelId, prefix: 'Días', onChange });
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
    const el = document.getElementById(canvasId);
    if (!el) return chartRef;
    if (chartRef && chartRef.destroy) chartRef.destroy();
    const pal = colors || ['#378ADD', '#1D9E75', '#BA7517', '#85B7EB', '#5DCAA5', '#888780', '#a32d2d', '#0c447c'];
    return new Chart(el, {
      type: 'pie',
      data: {
        labels,
        datasets: [{ data: values, backgroundColor: labels.map((_, i) => pal[i % pal.length]) }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'right',
            labels: {
              boxWidth: 12,
              font: { size: 11 },
              generateLabels: chart => {
                const ds = chart.data.datasets[0];
                const total = ds.data.reduce((a, b) => a + b, 0) || 1;
                return chart.data.labels.map((lbl, i) => {
                  const v = ds.data[i];
                  const txt = fmt === 'pct'
                    ? `${lbl}: ${Number(v).toFixed(1)}%`
                    : `${lbl}: $${Number(v).toLocaleString('es-EC')}`;
                  return {
                    text: txt,
                    fillStyle: ds.backgroundColor[i],
                    hidden: false,
                    index: i,
                  };
                });
              },
            },
          },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.parsed;
                if (fmt === 'pct') return `${ctx.label}: ${Number(v).toFixed(1)}%`;
                return `${ctx.label}: $${Number(v).toLocaleString('es-EC')}`;
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
    if (lineData) {
      datasets.push({
        type: 'line', label: lineLabel || 'Total', data: lineData,
        borderColor: '#BA7517', backgroundColor: 'transparent', borderWidth: 2, pointRadius: 3, tension: 0.3, order: 1,
      });
    }
    const yTick = fmt === 'pct'
      ? v => Number(v).toFixed(0) + '%'
      : v => '$' + Number(v).toLocaleString();
    return new Chart(el, {
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
            },
          },
        },
        scales: {
          x: { stacked: !!barDatasets[0]?.stack, ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
          y: { stacked: !!barDatasets[0]?.stack, ticks: { callback: yTick } },
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
