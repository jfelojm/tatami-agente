/* Filtros compartidos: paneles checkbox, períodos, gráficos */
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

  w.buildCheckboxPanel = function (container, items, { allByDefault = true, onChange } = {}) {
    if (!container) return;
    container.innerHTML = items.map(it => {
      const id = container.id + '-cb-' + String(it.v).replace(/[^a-z0-9]/gi, '_');
      const chk = allByDefault ? ' checked' : '';
      return `<label class="chk-item" for="${id}"><input type="checkbox" id="${id}" value="${it.v}"${chk}> ${it.n}</label>`;
    }).join('');
    container.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('change', () => { if (onChange) onChange(); });
    });
  };

  w.getCheckedValues = function (container) {
    if (!container) return [];
    return [...container.querySelectorAll('input:checked')].map(i => i.value);
  };

  w.initMesesPanel = function (year, containerId, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    const items = [];
    for (let m = 1; m <= 12; m++) {
      const mm = String(m).padStart(2, '0');
      items.push({ v: `${year}-${mm}`, n: MESES[m] });
    }
    w.buildCheckboxPanel(c, items, { allByDefault: true, onChange });
  };

  w.initDiasSemPanel = function (containerId, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    w.buildCheckboxPanel(c, DIAS_SEM.map(d => ({ v: String(d.v), n: d.n })), { allByDefault: true, onChange });
  };

  w.initPvPanel = function (containerId, onChange) {
    const c = document.getElementById(containerId);
    if (!c) return;
    const items = [
      { v: 'BARRA', n: 'Barra' }, { v: 'COCINA', n: 'Cocina' }, { v: 'OTRO', n: 'Otro' },
    ];
    w.buildCheckboxPanel(c, items, { allByDefault: true, onChange });
  };

  w.syncSemanasFromData = function (labels, containerId, onChange) {
    const c = document.getElementById(containerId);
    if (!c || !labels) return;
    const items = labels.map(l => ({ v: l, n: l.replace('-W', ' S') }));
    w.buildCheckboxPanel(c, items, { allByDefault: true, onChange });
  };

  w.syncDiasFromData = function (labels, containerId, onChange) {
    const c = document.getElementById(containerId);
    if (!c || !labels) return;
    const items = labels.map(l => ({ v: l, n: l.slice(8) + '/' + l.slice(5, 7) }));
    w.buildCheckboxPanel(c, items, { allByDefault: true, onChange });
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

  w.drawPieChart = function (canvasId, chartRef, labels, values, colors) {
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
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: $${ctx.parsed.toLocaleString('es-EC')}` } },
        },
      },
    });
  };

  w.drawBarLineChart = function (canvasId, chartRef, labels, barDatasets, lineData, lineLabel) {
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
    return new Chart(el, {
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: true, labels: { boxWidth: 12, font: { size: 11 } } } },
        scales: {
          x: { stacked: !!barDatasets[0]?.stack, ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
          y: { stacked: !!barDatasets[0]?.stack, ticks: { callback: v => '$' + Number(v).toLocaleString() } },
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
