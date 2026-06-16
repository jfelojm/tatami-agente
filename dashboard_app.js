/* Portal multi-dashboard Tatami — complemento de dashboard.html */
(function () {
  const DASH_IDS = ['ventas', 'compras', 'rentabilidad', 'inventario', 'roturas', 'confianza'];
  window.currentDash = window.currentDash || 'ventas';
  window.invResp = window.invResp || '';
  window.cmpArea = window.cmpArea || '';
  window.rentSocioFmt = window.rentSocioFmt || 'valor';

  let chCompras = null;
  let chComprasPie = null;
  let chRent = null;
  let chRentPv = null;
  let chRentSocios = null;

  window.syncSharedFilters = function () {
    const dash = window.currentDash || 'ventas';
    const aud = window.audience || 'socios';
    const showShared = dash === 'ventas' || dash === 'rentabilidad';
    const filt = document.getElementById('filters-ventas');
    const dashVentas = document.getElementById('dash-ventas');
    if (filt) filt.classList.toggle('hid', !showShared);
    if (dashVentas) dashVentas.classList.toggle('hid', !showShared);
    if (!showShared) return;
    const isOp = aud === 'operativo';
    const frTabla = document.getElementById('fr-tabla');
    const fo = document.getElementById('filtros-operativo');
    if (frTabla) frTabla.classList.toggle('hid', dash !== 'ventas' || !isOp);
    if (fo) fo.classList.toggle('hid', !isOp);
    const lblPlato = document.getElementById('lbl-plato');
    const sp = document.getElementById('sp');
    if (lblPlato) lblPlato.classList.toggle('hid', dash === 'rentabilidad');
    if (sp) sp.classList.toggle('hid', dash === 'rentabilidad');
    const pvSocios = document.getElementById('panel-socios');
    const pvOp = document.getElementById('panel-operativo');
    const rentSocios = document.getElementById('rent-panel-socios');
    const rentOp = document.getElementById('rent-panel-operativo');
    if (dash === 'ventas') {
      if (pvSocios) pvSocios.classList.toggle('hid', aud !== 'socios');
      if (pvOp) pvOp.classList.toggle('hid', aud !== 'operativo');
      if (rentSocios) rentSocios.classList.add('hid');
      if (rentOp) rentOp.classList.add('hid');
    } else if (dash === 'rentabilidad') {
      if (pvSocios) pvSocios.classList.add('hid');
      if (pvOp) pvOp.classList.add('hid');
      if (rentSocios) rentSocios.classList.toggle('hid', aud !== 'socios');
      if (rentOp) rentOp.classList.toggle('hid', aud !== 'operativo');
    }
  };

  window.setDashboard = function (id) {
    if (!DASH_IDS.includes(id)) return;
    window.currentDash = id;
    document.querySelectorAll('#portal-nav [data-dash]').forEach(el => {
      el.classList.toggle('on', el.dataset.dash === id);
    });
    const showVentasFilters = id === 'ventas' || id === 'rentabilidad';
    ['dash-compras', 'dash-rentabilidad', 'dash-inventario', 'dash-roturas', 'dash-confianza'].forEach(did => {
      const el = document.getElementById(did);
      if (el) el.classList.toggle('hid', id !== did.replace('dash-', ''));
    });
    document.getElementById('filters-global').classList.toggle('hid', showVentasFilters || id === 'compras' || id === 'inventario');
    window.syncSharedFilters();
    if (typeof window.cargarDashboard === 'function') window.cargarDashboard();
  };

  window.getGlobalRange = function () {
    const ga = document.getElementById('ga');
    const gm = document.getElementById('gm');
    if (ga && gm) {
      const anio = ga.value;
      const mes = gm.value;
      const finMes = monthEnd(anio, mes);
      const agrup = (document.getElementById('gagrup') || {}).value || 'mes';
      if (agrup === 'anio') return { desde: anio + '-01-01', hasta: anio + '-12-31' };
      return { desde: anio + '-' + mes + '-01', hasta: anio + '-' + mes + '-' + finMes };
    }
    if (typeof window.getSociosRange === 'function') return window.getSociosRange();
    return { desde: '2026-01-01', hasta: '2026-12-31' };
  };

  window.cargarDashboard = async function () {
    const err = document.getElementById('error-msg');
    err.classList.add('hid');
    try {
      if (window.currentDash === 'ventas') {
        if (typeof window.cargar === 'function') await window.cargar();
        return;
      }
      if (window.currentDash === 'compras') return window.cargarCompras();
      if (window.currentDash === 'rentabilidad') return window.cargarRentabilidadFull();
      if (window.currentDash === 'inventario') return window.cargarInventario();
      if (window.currentDash === 'roturas') return window.cargarRoturas();
      if (window.currentDash === 'confianza') return window.cargarConfianza();
    } catch (e) {
      err.textContent = 'Error: ' + e.message;
      err.classList.remove('hid');
    }
  };

  function deltaHtml(d) {
    if (d == null) return '<span class="delta-flat">—</span>';
    const cls = d > 0 ? 'delta-up' : d < 0 ? 'delta-down' : 'delta-flat';
    return `<span class="${cls}">${d > 0 ? '+' : ''}${d}%</span>`;
  }

  function card(label, value, sub) {
    return `<div class="mc"><div class="ml">${label}</div><div class="mv">${value}</div><div class="ms">${sub}</div></div>`;
  }

  /* ——— Compras ——— */
  window.initComprasMeses = function () {
    const y = document.getElementById('cmp-anio').value;
    initMesesPanel(y, 'cmp-meses', 'dd-cmp-meses-label', cargarCompras);
  };

  window.getComprasRange = function () {
    const y = document.getElementById('cmp-anio').value;
    const meses = getCheckedValues(document.getElementById('cmp-meses'));
    return rangeFromMeses(meses.length ? meses : [`${y}-01`, `${y}-12`], y);
  };

  window.setCmpArea = function (area, btn) {
    window.cmpArea = area;
    btn.parentElement.querySelectorAll('.mb').forEach(x => x.classList.remove('on'));
    btn.classList.add('on');
    cargarCompras();
  };

  window.cargarCompras = async function () {
    document.getElementById('compras-metrics').innerHTML = '<div class="loading">Cargando...</div>';
    const r = getComprasRange();
    const agrup = document.getElementById('cmp-agrup').value;
    const q = new URLSearchParams({ token: window.TOKEN, ...r, agrup });
    if (window.cmpArea) q.set('area', window.cmpArea);
    const res = await fetch(`${window.BASE}/api/dashboard/compras?${q}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.status);
    const d = await res.json();
    const a = d.actual;
    const cmp = d.comparativo_anterior.metricas;
    const areaLbl = window.cmpArea === 'COCINA' ? 'Cocina' : window.cmpArea === 'BARRA' ? 'Barra' : 'Todas';
    document.getElementById('compras-metrics').innerHTML = [
      card('Compras inventario', '$' + a.vta.toLocaleString('es-EC'), `${areaLbl} · vs ant. ${deltaHtml(cmp.vta.delta_pct)}`),
      card('Cocina', '$' + (a.por_area.COCINA || 0).toLocaleString('es-EC'), 'Bodegas cocina + externa'),
      card('Barra', '$' + (a.por_area.BARRA || 0).toLocaleString('es-EC'), 'Barra + consignación'),
      card('Proveedores', String(a.proveedores.length), `Acum. año $${d.acumulado_anio.vta.toLocaleString('es-EC')}`),
    ].join('');

    const labels = a.labels || [];
    const serie = a.serie || [];
    chCompras = drawBarLineChart('cv-compras', chCompras, labels, [
      { type: 'bar', label: 'Compras $', data: serie, backgroundColor: '#85B7EB', borderColor: '#378ADD', borderWidth: 0.5 },
    ], serie, 'Total');

    const provs = a.top_proveedores || [];
    chComprasPie = drawPieChart(
      'cv-compras-pie', chComprasPie,
      provs.map(p => p.nombre),
      provs.map(p => p.vta),
    );

    let tb = '';
    (a.proveedores || []).forEach(p => {
      tb += `<tr><td>${esc(p.nombre)}</td><td class="r">${p.facturas}</td><td class="r">$${p.vta.toLocaleString('es-EC')}</td><td class="r">${p.pct}%</td></tr>`;
    });
    document.getElementById('compras-tb').innerHTML = tb || '<tr><td colspan="4">Sin datos</td></tr>';
    document.getElementById('last-update').textContent = 'Actualizado ' + new Date().toLocaleTimeString('es-EC', { hour: '2-digit', minute: '2-digit' });
  };

  /* ——— Rentabilidad (mismos filtros que ventas) ——— */
  window.cargarRentabilidadFull = async function () {
    const err = document.getElementById('error-msg');
    err.classList.add('hid');
    const aud = window.audience || 'socios';
    const r = typeof window.getRanges === 'function' ? window.getRanges() : getGlobalRange();
    window._periodFilter = r._filterLabels || null;
    delete r._filterLabels;
    const q = new URLSearchParams({ token: window.TOKEN, ...r, agrup: r.agrup || 'mes' });
    const target = aud === 'socios' ? 'rent-socios-metrics' : 'rent-metrics';
    document.getElementById(target).innerHTML = '<div class="loading">Cargando...</div>';
    try {
      const res = await fetch(`${window.BASE}/api/dashboard/rentabilidad?${q}`);
      if (!res.ok) {
        const ej = await res.json().catch(() => ({}));
        throw new Error(ej.detail || `Error ${res.status}`);
      }
      window._rentData = await res.json();
      window.renderRentabilidad();
      document.getElementById('last-update').textContent = 'Actualizado ' + new Date().toLocaleTimeString('es-EC', { hour: '2-digit', minute: '2-digit' });
    } catch (e) {
      console.error('rentabilidad', e);
      err.textContent = 'Error rentabilidad: ' + e.message;
      err.classList.remove('hid');
      document.getElementById(target).innerHTML = '<div class="loading">No se pudo cargar rentabilidad</div>';
    }
  };

  window.renderRentabilidad = function () {
    const d = window._rentData;
    if (!d || !d.resumen) return;
    const s = d.resumen;
    const aud = window.audience || 'socios';
    const pvInc = typeof window.pvIncludes === 'function' ? window.pvIncludes : () => true;

    if (aud === 'socios') {
      const fmt = window.rentSocioFmt || 'valor';
      const pctCosto = s.vta ? Math.round(s.costo_real / s.vta * 1000) / 10 : 0;
      const metrics = fmt === 'pct'
        ? [
          card('Ventas netas', '$' + s.vta.toLocaleString('es-EC'), d.nota_costo || ''),
          card('Margen real', s.margen_real_pct + '%', '$' + s.margen_real.toLocaleString('es-EC')),
          card('Margen teórico', s.margen_teorico_pct + '%', '$' + s.margen_teorico.toLocaleString('es-EC')),
          card('Costo MP / ventas', pctCosto + '%', '$' + s.costo_real.toLocaleString('es-EC')),
        ]
        : [
          card('Ventas netas', '$' + s.vta.toLocaleString('es-EC'), d.nota_costo || ''),
          card('Margen real', '$' + s.margen_real.toLocaleString('es-EC'), s.margen_real_pct + '% del neto'),
          card('Margen teórico', '$' + s.margen_teorico.toLocaleString('es-EC'), s.margen_teorico_pct + '% del neto'),
          card('Costo MP período', '$' + s.costo_real.toLocaleString('es-EC'), 'Promedio compras ENTRADA'),
        ];
      document.getElementById('rent-socios-metrics').innerHTML = metrics.join('');

      const mpv = d.margen_pv || {};
      const pvLabels = [];
      const pvVals = [];
      const colors = { BARRA: '#378ADD', COCINA: '#1D9E75', OTRO: '#888780' };
      ['BARRA', 'COCINA', 'OTRO'].forEach(k => {
        if (!pvInc(k)) return;
        pvLabels.push(k === 'BARRA' ? 'Barra' : k === 'COCINA' ? 'Cocina' : 'Otro');
        pvVals.push(mpv[k] || 0);
      });
      const pieVals = fmt === 'pct'
        ? (() => {
          const tot = pvVals.reduce((a, b) => a + b, 0);
          return pvVals.map(v => tot ? Math.round(v / tot * 1000) / 10 : 0);
        })()
        : pvVals;
      const pieTitle = document.getElementById('rent-pie-title');
      if (pieTitle) pieTitle.textContent = fmt === 'pct' ? 'Participación del margen por PV (%)' : 'Margen por punto de venta ($)';
      const evoTitle = document.getElementById('rent-evo-title');
      if (evoTitle) evoTitle.textContent = fmt === 'pct' ? 'Evolución margen bruto (%)' : 'Evolución del margen ($)';
      chRentPv = pvLabels.length
        ? drawPieChart('cv-rent-pv', chRentPv, pvLabels, pieVals, Object.values(colors), { format: fmt === 'pct' ? 'pct' : 'money' })
        : (chRentPv && chRentPv.destroy ? (chRentPv.destroy(), null) : null);

      let labels = d.labels || [];
      let serie = fmt === 'pct' ? (d.margen_pct || []) : (d.margen || []);
      const pf = window._periodFilter;
      if (pf && pf.length) {
        const idx = labels.map((l, i) => pf.includes(l) ? i : -1).filter(i => i >= 0);
        labels = idx.map(i => labels[i]);
        serie = idx.map(i => serie[i]);
      }
      const barLbl = fmt === 'pct' ? 'Margen %' : 'Margen $';
      chRentSocios = drawBarLineChart('cv-rent-socios', chRentSocios, labels, [
        { type: 'bar', label: barLbl, data: serie, backgroundColor: '#5DCAA5', borderColor: '#1D9E75', borderWidth: 0.5 },
      ], fmt === 'pct' ? null : serie, fmt === 'pct' ? null : 'Margen', { format: fmt === 'pct' ? 'pct' : 'money' });
      return;
    }

    document.getElementById('rent-metrics').innerHTML = [
      card('Ventas netas', '$' + s.vta.toLocaleString('es-EC'), `${d.periodo.desde} → ${d.periodo.hasta}`),
      card('Margen real', s.margen_real_pct + '%', '$' + s.margen_real.toLocaleString('es-EC')),
      card('Margen teórico', s.margen_teorico_pct + '%', '$' + s.margen_teorico.toLocaleString('es-EC')),
      card('Costo real', '$' + s.costo_real.toLocaleString('es-EC'), d.nota_costo || ''),
    ].join('');

    let labels = d.labels || [];
    let barra = d.barra || [];
    let cocina = d.cocina || [];
    let otro = d.otro || [];
    let margen = d.margen || [];
    const pf = window._periodFilter;
    if (pf && pf.length) {
      const idx = labels.map((l, i) => pf.includes(l) ? i : -1).filter(i => i >= 0);
      labels = idx.map(i => labels[i]);
      barra = idx.map(i => barra[i]);
      cocina = idx.map(i => cocina[i]);
      otro = idx.map(i => otro[i]);
      margen = idx.map(i => margen[i]);
    }

    const datasets = [];
    if (pvInc('BARRA')) datasets.push({ type: 'bar', label: 'Barra', data: barra, backgroundColor: '#85B7EB', stack: 'm' });
    if (pvInc('COCINA')) datasets.push({ type: 'bar', label: 'Cocina', data: cocina, backgroundColor: '#5DCAA5', stack: 'm' });
    if (pvInc('OTRO')) datasets.push({ type: 'bar', label: 'Otro', data: otro, backgroundColor: '#d3d1c7', stack: 'm' });
    chRent = drawBarLineChart('cv-rent', chRent, labels, datasets, margen, 'Margen total');

    let tb = '';
    const ord = (document.getElementById('so') || {}).value || 'desc';
    const platos = (d.platos || []).filter(p => pvInc(p.pv)).slice().sort((a, b) => {
      const rev = ord !== 'asc';
      return rev ? (b.vta - a.vta) : (a.vta - b.vta);
    });
    platos.slice(0, 40).forEach(p => {
      tb += `<tr><td>${esc(p.nombre)}<div style="font-size:11px;color:#888780">${esc(p.cat)} · ${p.pv}</div></td><td class="r">$${p.vta.toLocaleString('es-EC')}</td><td class="r">${p.margen_real_pct}%</td><td class="r">$${p.margen_real.toLocaleString('es-EC')}</td></tr>`;
    });
    document.getElementById('rent-tb').innerHTML = tb || '<tr><td colspan="4">Sin datos</td></tr>';
    document.getElementById('rent-ct').textContent = 'Margen bruto · ' + (typeof getTitle === 'function' ? getTitle() : '');
  };

  window.cargarRentabilidad = window.cargarRentabilidadFull;

  window.setRentSocioFmt = function (fmt, btn) {
    window.rentSocioFmt = fmt;
    if (btn && btn.parentElement) {
      btn.parentElement.querySelectorAll('.mb').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
    }
    if (window._rentData) window.renderRentabilidad();
  };

  /* ——— Inventario ——— */
  window.setInvResp = function (resp, btn) {
    window.invResp = resp;
    btn.parentElement.querySelectorAll('.mb').forEach(x => x.classList.remove('on'));
    btn.classList.add('on');
    const sel = document.getElementById('inv-bodega');
    if (sel) {
      if (resp === 'Cocina') sel.value = '';
      else if (resp === 'Barra') sel.value = '';
    }
    cargarInventario();
  };

  window.cargarInventario = async function () {
    document.getElementById('inv-metrics').innerHTML = '<div class="loading">Cargando...</div>';
    document.getElementById('inv-tree').innerHTML = '<div class="loading">Cargando...</div>';
    const q = new URLSearchParams({ token: window.TOKEN });
    const resp = window.invResp;
    const bod = document.getElementById('inv-bodega').value;
    const dias = document.getElementById('inv-dias').value;
    q.set('dias_periodo', dias);
    if (resp) q.set('responsabilidad', resp);
    if (bod) q.set('cod_bodega', bod);
    const res = await fetch(`${window.BASE}/api/dashboard/inventario/vivo?${q}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.status);
    const d = await res.json();
    const cons = d.consolidado || {};
    const resumen = d.resumen || {};
    document.getElementById('inv-metrics').innerHTML = [
      card('Items', String(d.total_items), `${dias} días consolidados`),
      card('Costo oportunidad', '$' + (cons.costo_oportunidad || 0).toLocaleString('es-EC'), 'Stock sobre PAR × costo'),
      card('Pérdida venta est.', '$' + (cons.perdida_venta || 0).toLocaleString('es-EC'), 'Quiebre / bajo PAR × consumo'),
      card('Crítico + quiebre', String((resumen.CRITICO || 0) + (resumen.QUIEBRE || 0) + (resumen.NEGATIVO || 0)), `Bajo PAR: ${resumen.BAJO_PAR || 0}`),
    ].join('');
    document.getElementById('inv-tree').innerHTML = renderInvTree(d.arbol || []);
    document.getElementById('last-update').textContent = 'En vivo · ' + new Date().toLocaleTimeString('es-EC', { hour: '2-digit', minute: '2-digit' });
  };

  function renderInvTree(arbol) {
    if (!arbol.length) return '<div style="padding:12px;color:#888780">Sin items para los filtros seleccionados</div>';
    let html = '';
    arbol.forEach(resp => {
      html += `<div class="collapse-head" onclick="toggleCollapse(this)">${esc(resp.nombre)}</div><div class="collapse-body">`;
      (resp.bodegas || []).forEach(bod => {
        html += `<div class="collapse-head" onclick="toggleCollapse(this)" style="margin-left:8px">${esc(bod.nombre)}</div><div class="collapse-body">`;
        (bod.categorias || []).forEach(cat => {
          html += `<div class="collapse-head" onclick="toggleCollapse(this)" style="margin-left:16px">${esc(cat.nombre)}</div><div class="collapse-body"><table><thead><tr><th>MP</th><th class="r">Stock</th><th class="r">PAR</th><th class="r">Estado</th><th class="r">Oport. $</th><th class="r">Pérdida $</th></tr></thead><tbody>`;
          (cat.items || []).forEach(it => {
            html += `<tr><td>${esc(it.nombre_mp)}</td><td class="r">${it.stock}</td><td class="r">${it.par_level}</td><td class="r"><span class="badge">${it.estado}</span></td><td class="r">$${(it.oportunidad_periodo || 0).toLocaleString('es-EC')}</td><td class="r">$${(it.perdida_periodo || 0).toLocaleString('es-EC')}</td></tr>`;
          });
          html += '</tbody></table></div>';
        });
        html += '</div>';
      });
      html += '</div>';
    });
    return html;
  }

  window.cargarRoturas = async function () {
    const r = window.getGlobalRange();
    const q = new URLSearchParams({ token: window.TOKEN, ...r, agrup: 'mes' });
    const res = await fetch(`${window.BASE}/api/dashboard/roturas?${q}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.status);
    const d = await res.json();
    document.getElementById('rot-metrics').innerHTML = [
      card('Rotura contable', '$' + d.total_rotura.toLocaleString('es-EC'), d.lineas_ajuste + ' ajustes'),
      card('Período', d.periodo.desde + ' → ' + d.periodo.hasta, d.nota || ''),
    ].join('');
    let tb = '';
    (d.top_mps || []).forEach(m => {
      tb += `<tr><td>${esc(m.nombre_mp)}</td><td class="r">${Math.round(m.uds)}</td><td class="r">$${m.vta.toLocaleString('es-EC')}</td></tr>`;
    });
    document.getElementById('rot-tb').innerHTML = tb || '<tr><td colspan="3">Sin ajustes negativos</td></tr>';
  };

  window.cargarConfianza = async function () {
    const res = await fetch(`${window.BASE}/api/dashboard/confianza?token=${window.TOKEN}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.status);
    const d = await res.json();
    document.getElementById('conf-metrics').innerHTML = [
      card('Score global', d.score_global + '/100', 'Confianza inventario'),
    ].join('');
    let tb = '';
    Object.values(d.bodegas || {}).forEach(b => {
      tb += `<tr><td>${esc(b.nombre)}</td><td class="r">${b.score}</td><td class="r">${b.dias_ultimo_conteo != null ? b.dias_ultimo_conteo + ' d' : '—'}</td><td class="r">${b.precision_delta_pct}%</td></tr>`;
    });
    document.getElementById('conf-tb').innerHTML = tb || '<tr><td colspan="4">Sin datos</td></tr>';
  };

  function esc(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
})();
