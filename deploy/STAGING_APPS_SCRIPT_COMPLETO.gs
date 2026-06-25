/**
 * tatami_staging.gs — SCRIPT MASTERS SHEETS (staging)
 * =============================================================================
 * Incluye: promoción al maestro, tests, facturas manuales y traslados masivos.
 *
 * LIBRO STAGING (pegar aquí, NO en el maestro de datos):
 *   https://docs.google.com/spreadsheets/d/1TJu70BNG4i3it4y51Eg3YlDNswLkh1QGRt6v-qAyexU/edit
 *
 * INSTALACIÓN:
 *   1. Abre Masters Sheets / STAGING → Extensiones → Apps Script
 *   2. Borra todos los archivos .gs viejos
 *   3. Un solo archivo: pega TODO este script
 *   4. Propiedades del script:
 *        TATAMI_FACTURA_API_URL  = .../api/factura_manual/enviar
 *        TATAMI_FACTURA_SECRET   = FACTURA_SHEETS_INGEST_SECRET
 *        TATAMI_TRASLADO_API_URL = .../api/traslado_masivo/enviar
 *        TATAMI_TRASLADO_SECRET  = TRASLADO_SHEETS_INGEST_SECRET
 *   5. Ejecutar solicitarPermisosExternos → Guardar → F5
 *
 * HOJAS (Python):
 *   python setup_ingreso_factura_manual.py
 *   python setup_ingreso_traslado_masivo.py
 *
 * MENÚS: Tatami Admin | Tatami Tests | Tatami Facturas | Tatami Traslados
 *
 * Master ID destino: 1rTVMfsOBssx2R-Sbuj1SRx9NZSd_hinEa9IK_ahGqZY
 * =============================================================================
 */

// ── IDs ──────────────────────────────────────────────────────────────────────
const MASTER_ID   = "1rTVMfsOBssx2R-Sbuj1SRx9NZSd_hinEa9IK_ahGqZY";
const TEST_PREFIX = "TEST_";

// ── Menús ────────────────────────────────────────────────────────────────────
function onOpen() {
  const ui = SpreadsheetApp.getUi();

  ui.createMenu("🍱 Tatami Admin")
    .addItem("Promover Proveedores aprobados → BD_PROV", "promoverProveedores")
    .addSeparator()
    .addItem("Promover MPs aprobadas → BD_MP_SISTEMA", "promoverMPs")
    .addSeparator()
    .addItem("Promover Recetas aprobadas → BD_RECETAS_DETALLE", "promoverRecetas")
    .addSeparator()
    .addItem("Promover Subrecetas (cabecera) → BD_SUBRECETAS", "promoverSubrecetasCab")
    .addItem("Promover Subrecetas (detalle) → BD_SUBRECETAS_DETALLE", "promoverSubrecetasDetalle")
    .addItem("Promover Subrecetas (cab + detalle)", "promoverSubrecetas")
    .addItem("Refrescar listas aux subrecetas (dropdowns)", "refrescarAuxSubrecetasMenu")
    .addSeparator()
    .addItem("Promover Productos aprobados → BD_PRODUCTOS", "promoverProductos")
    .addToUi();

  ui.createMenu("🧪 Tatami Tests")
    .addItem("1. Insertar datos de prueba", "insertarDatosPrueba")
    .addItem("2. Aprobar filas de prueba", "aprobarFilasPrueba")
    .addItem("3. Promover al Master", "promoverPrueba")
    .addItem("4. Verificar en Master", "verificarEnMaster")
    .addItem("5. Limpiar datos de prueba", "limpiarDatosPrueba")
    .addSeparator()
    .addItem("▶ TEST COMPLETO", "testCompleto")
    .addToUi();

  ui.createMenu("🍣 Tatami Facturas")
    .addItem("🔐 Autorizar conexión con agente", "solicitarPermisosExternosMenu")
    .addItem("🔍 Verificar URL y proveedores", "verificarConexionFactura")
    .addSeparator()
    .addItem("✅ Aceptar e ingresar factura", "aceptarFactura")
    .addItem("🧪 Probar (sin subir stock)", "probarFactura")
    .addToUi();

  ui.createMenu("📦 Tatami Traslados")
    .addItem("🔐 Autorizar conexión con agente", "solicitarPermisosExternosMenu")
    .addItem("🔍 Verificar URL y correos", "verificarConexionTraslado")
    .addItem("🔽 Filtrar lista por bodega origen (opcional)", "actualizarListaProductosTraslado")
    .addItem("📋 Restaurar lista completa de productos", "restaurarListaCompletaTraslado")
    .addSeparator()
    .addItem("✅ Aceptar y trasladar", "aceptarTraslado")
    .addItem("🧪 Probar (sin mover stock)", "probarTraslado")
    .addToUi();
}

// ═══════════════════════════════════════════════════════════════════════════════
// UTILIDADES
// ═══════════════════════════════════════════════════════════════════════════════

function leerHoja(spreadsheet, nombreHoja) {
  const hoja = spreadsheet.getSheetByName(nombreHoja);
  if (!hoja) throw new Error(`Hoja no encontrada: ${nombreHoja}`);
  const datos = hoja.getDataRange().getValues();
  if (datos.length < 2) return { hoja, headers: datos[0] || [], filas: [] };
  const headers = datos[0].map(h => h.toString().trim());
  const filas = datos.slice(1).map((fila, idx) => ({
    idx: idx + 1,
    data: Object.fromEntries(headers.map((h, i) => [h, fila[i]]))
  }));
  return { hoja, headers, filas };
}

function marcarPromovido(hoja, filaIdx, colEstadoIdx) {
  hoja.getRange(filaIdx + 2, colEstadoIdx + 1).setValue("PROMOVIDO");
}

function getColEstado(headers) {
  const idx = headers.findIndex(h => h.toString().trim().toLowerCase() === "estado");
  if (idx === -1) throw new Error("Columna 'estado' no encontrada");
  return idx;
}

function alerta(titulo, msg) {
  SpreadsheetApp.getUi().alert(titulo, msg, SpreadsheetApp.getUi().ButtonSet.OK);
}

function get(data, ...keys) {
  for (const k of keys) {
    if (data[k] !== undefined && data[k] !== "") return data[k].toString().trim();
  }
  const norm = s => s.toLowerCase()
    .replace(/\s+/g, "")
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  for (const k of keys) {
    const kn = norm(k);
    for (const [h, v] of Object.entries(data)) {
      if (norm(h) === kn && v !== "") return v.toString().trim();
    }
  }
  return "";
}

function normCod(cod) {
  const s = (cod || "").toString().trim();
  if (!s) return "";
  if (/^\d+$/.test(s)) return String(parseInt(s, 10));
  return s;
}

function normBodega(bod) {
  const s = (bod || "").toString().trim().toUpperCase();
  const m = s.match(/BOD-\d+/);
  return m ? m[0] : s;
}

function pctADecimal(raw, def) {
  const v = parseFloat((raw || "").toString().replace(",", "."));
  if (isNaN(v) || v <= 0) return def;
  if (v > 1) return v / 100;
  return v;
}

function cargarCodigosSubMaster(master) {
  const hoja = master.getSheetByName("BD_SUBRECETAS");
  const set = new Set();
  if (!hoja) return set;
  const datos = hoja.getDataRange().getValues();
  if (datos.length < 2) return set;
  const headers = datos[0].map(h => h.toString().trim());
  const idx = headers.indexOf("cod_subreceta");
  if (idx === -1) return set;
  for (let i = 1; i < datos.length; i++) {
    const c = normCod(datos[i][idx]);
    if (c) set.add(c);
  }
  return set;
}

function clavesDetalleExistentes(master) {
  const hoja = master.getSheetByName("BD_SUBRECETAS_DETALLE");
  const set = new Set();
  if (!hoja) return set;
  const datos = hoja.getDataRange().getValues();
  if (datos.length < 2) return set;
  const headers = datos[0].map(h => h.toString().trim());
  const iPadre = headers.indexOf("cod_subreceta_padre");
  const iHijo = headers.indexOf("cod_subreceta_hijo");
  const iMp = headers.indexOf("cod_mp_sistema");
  if (iPadre === -1) return set;
  for (let i = 1; i < datos.length; i++) {
    const padre = normCod(datos[i][iPadre]);
    const hijo = iHijo >= 0 ? normCod(datos[i][iHijo]) : "";
    const mp = iMp >= 0 ? normCod(datos[i][iMp]) : "";
    if (!padre) continue;
    if (hijo) set.add(`${padre}|SUB:${hijo}`);
    else if (mp) set.add(`${padre}|MP:${mp}`);
  }
  return set;
}

// ═══════════════════════════════════════════════════════════════════════════════
// PROMOCIÓN — PROVEEDORES, MP, PRODUCTOS
// ═══════════════════════════════════════════════════════════════════════════════

function promoverProveedores() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);

  const { hoja: hojaStaging, headers, filas } = leerHoja(staging, "STAGING_PROVEEDORES");
  const hojaMaster = master.getSheetByName("BD_PROV");
  if (!hojaMaster) { alerta("Error", "BD_PROV no encontrada en el Master"); return; }

  const idxEstado = getColEstado(headers);
  const aprobados = filas.filter(f => get(f.data, "estado").toUpperCase() === "APROBADO");
  if (aprobados.length === 0) { alerta("Sin pendientes", "No hay proveedores con estado APROBADO."); return; }

  let promovidos = 0;
  aprobados.forEach(f => {
    const d = f.data;
    hojaMaster.appendRow([
      get(d, "Razón Social", "razon_social"),
      get(d, "cod_proveedor"),
      get(d, "RUC"),
      get(d, "¿Es proveedor de inventario?", "proveedor_inventario"),
      get(d, "Correo electrónico", "correo"),
      get(d, "¿Activo?", "activo"),
      get(d, "WhatsApp de contacto", "contacto_whatsapp"),
      get(d, "Dirección", "direccion"),
      get(d, "Nombre del contacto", "contacto_nombre"),
      get(d, "Condición de pago", "condicion_pago"),
      get(d, "Días de crédito", "dias_credito"),
      get(d, "Lead time (días)", "lead_time_dias"),
      get(d, "Frecuencia de compra (días)", "frecuencia_compra_dias"),
      get(d, "Días de pedido", "ventana_pedido"),
      get(d, "Observaciones", "observaciones"),
    ]);
    marcarPromovido(hojaStaging, f.idx, idxEstado);
    promovidos++;
  });

  alerta("✓ Proveedores promovidos", `${promovidos} proveedor(es) → BD_PROV.`);
}

function promoverMPs() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);

  const { hoja: hojaStaging, headers, filas } = leerHoja(staging, "STAGING_MP");
  const hojaMaster = master.getSheetByName("BD_MP_SISTEMA");
  if (!hojaMaster) { alerta("Error", "BD_MP_SISTEMA no encontrada en el Master"); return; }

  const idxEstado = getColEstado(headers);
  const aprobados = filas.filter(f => get(f.data, "estado").toUpperCase() === "APROBADO");
  if (aprobados.length === 0) { alerta("Sin pendientes", "No hay MPs con estado APROBADO."); return; }

  const BODEGAS = {
    "cocina": "BOD-001", "barra": "BOD-002", "consignacion": "BOD-003",
    "consignación": "BOD-003", "limpieza": "BOD-004", "bodegaisrael": "BOD-005", "israel": "BOD-005",
  };
  const normUnidad = u => {
    const uu = u.toString().trim().toLowerCase();
    if (uu === "g") return "gr";
    return u.toString().trim();
  };

  let promovidos = 0;
  aprobados.forEach(f => {
    const d = f.data;
    const bodegaRaw = get(d, "Bodega", "nombre_bodega");
    const codBodegaMatch = bodegaRaw.match(/BOD-\d+/);
    const codBodega = codBodegaMatch ? codBodegaMatch[0] : (BODEGAS[bodegaRaw.toLowerCase().replace(/\s+/g, "")] ?? "");
    const nombreBodega = bodegaRaw.replace(/\s*\(BOD-\d+\)/, "").trim();

    hojaMaster.appendRow([
      get(d, "Nombre de la materia prima", "nombre_mp"),
      get(d, "cod_mp_sistema"),
      get(d, "Categoría", "categoria"),
      normUnidad(get(d, "Unidad base", "unidad_base")),
      nombreBodega,
      codBodega,
      get(d, "Tipo de control", "tipo_control"),
      get(d, "Dias de Seguridad", "dias_seguridad"),
      "", "", "", "",
      get(d, "Activa", "activo"),
    ]);
    marcarPromovido(hojaStaging, f.idx, idxEstado);
    promovidos++;
  });

  alerta("✓ MPs promovidas", `${promovidos} MP(s) → BD_MP_SISTEMA.`);
}

function promoverProductos() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);

  const { hoja: hojaStaging, headers, filas } = leerHoja(staging, "STAGING_PRODUCTOS");
  const hojaMaster = master.getSheetByName("BD_PRODUCTOS");
  if (!hojaMaster) { alerta("Error", "BD_PRODUCTOS no encontrada en el Master"); return; }

  const idxEstado = getColEstado(headers);
  const aprobados = filas.filter(f => get(f.data, "estado").toUpperCase() === "APROBADO");
  if (aprobados.length === 0) { alerta("Sin pendientes", "No hay productos con estado APROBADO."); return; }

  let promovidos = 0;
  aprobados.forEach(f => {
    const d = f.data;
    const version = (() => {
      const v = get(d, "version") || "1";
      return v.toUpperCase().startsWith("V") ? v.toUpperCase() : `V${v}`;
    })();
    hojaMaster.appendRow([
      get(d, "nombre_producto"),
      get(d, "cod_smart_menu"),
      get(d, "variedad_smart_menu"),
      get(d, "cod_receta"),
      get(d, "categoria_menu"),
      get(d, "precio_venta"),
      get(d, "activo") || "SI",
      version,
      get(d, "fecha_vigencia"),
      get(d, "rendimiento"),
      get(d, "descarga_inventario") || "SI",
    ]);
    marcarPromovido(hojaStaging, f.idx, idxEstado);
    promovidos++;
  });

  alerta("✓ Productos promovidos", `${promovidos} producto(s) → BD_PRODUCTOS.`);
}

// ═══════════════════════════════════════════════════════════════════════════════
// PROMOCIÓN — RECETAS (v1 solo MP o v2 MP + SUB)
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * BD_RECETAS_DETALLE (maestro actual):
 * A=nombre_receta | B=cod_receta | C=variedad_smart_menu | D=nombre_subreceta |
 * E=cod_subreceta | F=nombre_mp | G=cod_mp_sistema | H=cantidad | I=unidad_base |
 * J=cod_bodega | K=merma_pct | L=es_opcional | M=pct_aplicacion |
 * N=costo_unitario | O=costo_linea | P=nota_costo (agente: calcular_costo_recetas.py)
 */
function promoverRecetas() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);

  const nombreHoja = staging.getSheetByName("STAGING_RECETAS")
    ? "STAGING_RECETAS"
    : (staging.getSheetByName("STAGING_RECETAS_V2") ? "STAGING_RECETAS_V2" : "STAGING_RECETAS");

  const { hoja: hojaStaging, headers, filas } = leerHoja(staging, nombreHoja);
  const hojaMaster = master.getSheetByName("BD_RECETAS_DETALLE");
  if (!hojaMaster) { alerta("Error", "BD_RECETAS_DETALLE no encontrada en el Master"); return; }

  const idxEstado = getColEstado(headers);
  const esV2 = headers.indexOf("tipo_linea") !== -1;
  const aprobados = filas.filter(f => get(f.data, "estado").toUpperCase() === "APROBADO");
  if (aprobados.length === 0) {
    alerta("Sin pendientes", `No hay recetas APROBADO en ${nombreHoja}.`);
    return;
  }

  let promovidos = 0;
  let errores = 0;
  const msgs = [];

  aprobados.forEach(f => {
    const d = f.data;
    try {
      if (esV2) {
        const tipo = get(d, "tipo_linea").toUpperCase();
        if (tipo !== "MP" && tipo !== "SUB") throw new Error("tipo_linea debe ser MP o SUB");
        const cant = parseFloat(get(d, "cantidad").replace(",", "."));
        if (isNaN(cant) || cant <= 0) throw new Error("cantidad inválida");
        const bod = normBodega(get(d, "cod_bodega"));
        if (bod !== "BOD-001" && bod !== "BOD-002") throw new Error("cod_bodega debe ser BOD-001 o BOD-002");

        let nombreSub = "", codSub = "", nombreMp = "", codMp = "";
        if (tipo === "MP") {
          codMp = get(d, "cod_mp_sistema");
          nombreMp = get(d, "nombre_mp");
          if (!codMp || get(d, "cod_subreceta")) throw new Error("MP: cod_mp lleno y cod_sub vacío");
        } else {
          codSub = get(d, "cod_subreceta");
          nombreSub = get(d, "nombre_subreceta");
          if (!codSub || get(d, "cod_mp_sistema")) throw new Error("SUB: cod_sub lleno y cod_mp vacío");
        }

        hojaMaster.appendRow([
          get(d, "nombre_receta"),
          get(d, "cod_receta"),
          get(d, "variedad_smart_menu"),
          nombreSub,
          codSub,
          nombreMp,
          codMp,
          cant,
          get(d, "unidad_base"),
          bod,
          pctADecimal(get(d, "merma_pct"), 0),
          get(d, "es_opcional") || "NO",
          pctADecimal(get(d, "pct_aplicacion"), 1),
          "",
          "",
          "",
        ]);
      } else {
        // Layout v1 (solo MP)
        hojaMaster.appendRow([
          get(d, "nombre_receta"),
          get(d, "cod_receta"),
          get(d, "variedad_smart_menu"),
          "",
          "",
          get(d, "nombre_mp"),
          get(d, "cod_mp_sistema"),
          get(d, "cantidad"),
          get(d, "unidad_base") || "gr",
          normBodega(get(d, "cod_bodega")) || "BOD-001",
          get(d, "merma_pct") || "0",
          get(d, "es_opcional") || "NO",
          get(d, "pct_aplicacion") || "1",
          "",
          "",
          "",
        ]);
      }
      marcarPromovido(hojaStaging, f.idx, idxEstado);
      promovidos++;
    } catch (e) {
      errores++;
      msgs.push(`Fila ${f.idx + 1}: ${e.message}`);
    }
  });

  let texto = `${promovidos} línea(s) → BD_RECETAS_DETALLE (${esV2 ? "v2" : "v1"}).`;
  if (errores) texto += `\n${errores} error(es):\n` + msgs.slice(0, 8).join("\n");
  alerta(errores ? "Recetas con avisos" : "✓ Recetas promovidas", texto);
}

// ═══════════════════════════════════════════════════════════════════════════════
// PROMOCIÓN — SUBRECETAS
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * BD_SUBRECETAS: nombre_subreceta | cod_subreceta | rendimiento_estandar |
 * unidad | activa | notas | (costos: agente)
 */
function promoverSubrecetasCab() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);

  const { hoja: hojaStaging, headers, filas } = leerHoja(staging, "STAGING_SUB_CAB");
  const hojaMaster = master.getSheetByName("BD_SUBRECETAS");
  if (!hojaMaster) { alerta("Error", "BD_SUBRECETAS no encontrada en el Master"); return; }

  const idxEstado = getColEstado(headers);
  const existentes = cargarCodigosSubMaster(master);
  const aprobados = filas.filter(f => get(f.data, "estado").toUpperCase() === "APROBADO");
  if (aprobados.length === 0) {
    alerta("Sin pendientes", "No hay cabeceras SUB con estado APROBADO.");
    return;
  }

  let promovidos = 0, dup = 0, err = 0;
  const msgs = [];

  aprobados.forEach(f => {
    const d = f.data;
    try {
      const cod = get(d, "cod_subreceta");
      if (!cod) throw new Error("cod_subreceta vacío");
      const rend = parseFloat(get(d, "rendimiento_estandar").replace(",", "."));
      if (isNaN(rend) || rend <= 0) throw new Error("rendimiento_estandar inválido");

      if (existentes.has(normCod(cod))) {
        dup++;
        marcarPromovido(hojaStaging, f.idx, idxEstado);
        return;
      }

      hojaMaster.appendRow([
        get(d, "nombre_subreceta"),
        cod,
        rend,
        get(d, "unidad") || "gr",
        get(d, "activa") || "SI",
        get(d, "notas"),
      ]);
      existentes.add(normCod(cod));
      marcarPromovido(hojaStaging, f.idx, idxEstado);
      promovidos++;
    } catch (e) {
      err++;
      msgs.push(`Fila ${f.idx + 1}: ${e.message}`);
    }
  });

  let t = `${promovidos} subreceta(s) → BD_SUBRECETAS. Duplicadas: ${dup}.`;
  if (err) t += `\nErrores: ${err}\n` + msgs.slice(0, 6).join("\n");
  if (promovidos > 0) {
    const aux = refrescarAuxSubrecetas();
    t += `\n\nListas aux actualizadas (${aux.sub} subrecetas, ${aux.union} padres en dropdown).`;
    t += "\nYa puede cargar el detalle en STAGING_SUB_DETALLE.";
  }
  alerta("✓ Cabecera subrecetas", t);
}

/**
 * Tras promover cabecera: reescribe _AUX_BD_SUBRECETAS, _AUX_SUB_HIJOS y
 * _AUX_SUB_PADRES_UNION en staging (dropdowns de STAGING_SUB_DETALLE).
 */
function refrescarAuxSubrecetas() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);
  const hojaMaster = master.getSheetByName("BD_SUBRECETAS");
  if (!hojaMaster) return { sub: 0, union: 0 };

  const datos = hojaMaster.getDataRange().getValues();
  const headers = datos[0].map(h => String(h).trim());
  const iNom = headers.indexOf("nombre_subreceta");
  const iCod = headers.indexOf("cod_subreceta");
  const iAct = headers.indexOf("activa");

  const auxSub = [["nombre_subreceta", "cod_subreceta"]];
  const vistosNom = {};
  const union = [];

  for (let i = 1; i < datos.length; i++) {
    const nom = String(datos[i][iNom] || "").trim();
    const cod = String(datos[i][iCod] || "").trim();
    const act = String(datos[i][iAct] != null ? datos[i][iAct] : "SI").trim().toUpperCase();
    if (!nom || !cod) continue;
    if (["NO", "N", "0"].indexOf(act) >= 0) continue;
    auxSub.push([nom, cod]);
    const key = nom.toLowerCase();
    if (!vistosNom[key]) {
      vistosNom[key] = true;
      union.push([nom]);
    }
  }

  try {
    const { headers: hCab, filas: filasCab } = leerHoja(staging, "STAGING_SUB_CAB");
    const iNomCab = hCab.indexOf("nombre_subreceta");
    filasCab.forEach(f => {
      const nom = String(f.data["nombre_subreceta"] || "").trim();
      if (!nom) return;
      const key = nom.toLowerCase();
      if (!vistosNom[key]) {
        vistosNom[key] = true;
        union.push([nom]);
      }
    });
  } catch (e) { /* STAGING_SUB_CAB opcional */ }

  union.sort((a, b) => a[0].localeCompare(b[0], "es"));

  escribirHojaAux(staging, "_AUX_BD_SUBRECETAS", auxSub);
  escribirHojaAux(staging, "_AUX_SUB_HIJOS", auxSub);
  escribirHojaAux(staging, "_AUX_SUB_PADRES_UNION", [["nombre_subreceta_padre"]].concat(union));

  SpreadsheetApp.flush();
  return { sub: auxSub.length - 1, union: union.length };
}

function escribirHojaAux(ss, nombre, filas) {
  let sh = ss.getSheetByName(nombre);
  if (!sh) sh = ss.insertSheet(nombre);
  sh.clear();
  if (filas.length && filas[0].length) {
    sh.getRange(1, 1, filas.length, filas[0].length).setValues(filas);
  }
  try { sh.hideSheet(); } catch (e) { /* ya oculta */ }
}

function refrescarAuxSubrecetasMenu() {
  const aux = refrescarAuxSubrecetas();
  alerta(
    "Listas aux subrecetas",
    `${aux.sub} subrecetas en _AUX_BD_SUBRECETAS.\n` +
    `${aux.union} nombres padre en _AUX_SUB_PADRES_UNION.\n\n` +
    "Dropdowns de STAGING_SUB_DETALLE actualizados."
  );
}

/**
 * BD_SUBRECETAS_DETALLE: nombre_subreceta | cod_subreceta_padre | nombre_subreceta_hijo |
 * cod_subreceta_hijo | nombre_mp | cod_mp_sistema | cantidad | unidad_base | cod_bodega | merma_pct
 */
function promoverSubrecetasDetalle() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master  = SpreadsheetApp.openById(MASTER_ID);

  const { hoja: hojaStaging, headers, filas } = leerHoja(staging, "STAGING_SUB_DETALLE");
  const hojaMaster = master.getSheetByName("BD_SUBRECETAS_DETALLE");
  if (!hojaMaster) { alerta("Error", "BD_SUBRECETAS_DETALLE no encontrada en el Master"); return; }

  const idxEstado = getColEstado(headers);
  const subsMaster = cargarCodigosSubMaster(master);
  const claves = clavesDetalleExistentes(master);
  const aprobados = filas.filter(f => get(f.data, "estado").toUpperCase() === "APROBADO");
  if (aprobados.length === 0) {
    alerta("Sin pendientes", "No hay detalle SUB con estado APROBADO.");
    return;
  }

  let promovidos = 0, dup = 0, err = 0;
  const msgs = [];

  aprobados.forEach(f => {
    const d = f.data;
    try {
      const padre = get(d, "cod_subreceta_padre");
      if (!padre) throw new Error("cod_subreceta_padre vacío");
      if (!subsMaster.has(normCod(padre))) {
        throw new Error(`padre ${padre} no existe en BD_SUBRECETAS (promover cabecera primero)`);
      }

      const tipo = get(d, "tipo_linea").toUpperCase();
      const cant = parseFloat(get(d, "cantidad").replace(",", "."));
      if (isNaN(cant) || cant <= 0) throw new Error("cantidad inválida");

      let codMp = "", codHijo = "", nomHijo = "", nomMp = "";
      let clave = "";
      if (tipo === "MP") {
        codMp = get(d, "cod_mp_sistema");
        nomMp = get(d, "nombre_mp");
        if (!codMp || get(d, "cod_subreceta_hijo")) throw new Error("MP: solo cod_mp_sistema");
        clave = `${normCod(padre)}|MP:${normCod(codMp)}`;
      } else if (tipo === "SUB") {
        codHijo = get(d, "cod_subreceta_hijo");
        nomHijo = get(d, "nombre_subreceta_hijo");
        if (!codHijo || get(d, "cod_mp_sistema")) throw new Error("SUB: solo cod_subreceta_hijo");
        clave = `${normCod(padre)}|SUB:${normCod(codHijo)}`;
      } else {
        throw new Error("tipo_linea debe ser MP o SUB");
      }

      if (claves.has(clave)) {
        dup++;
        marcarPromovido(hojaStaging, f.idx, idxEstado);
        return;
      }

      hojaMaster.appendRow([
        get(d, "nombre_subreceta_padre"),
        padre,
        nomHijo,
        codHijo,
        nomMp,
        codMp,
        cant,
        get(d, "unidad_base"),
        normBodega(get(d, "cod_bodega")) || "BOD-001",
        get(d, "merma_pct") || "0",
      ]);
      claves.add(clave);
      marcarPromovido(hojaStaging, f.idx, idxEstado);
      promovidos++;
    } catch (e) {
      err++;
      msgs.push(`Fila ${f.idx + 1}: ${e.message}`);
    }
  });

  let t = `${promovidos} línea(s) → BD_SUBRECETAS_DETALLE. Duplicadas: ${dup}.`;
  if (err) t += `\nErrores: ${err}\n` + msgs.slice(0, 8).join("\n");
  t += "\n\nSugerencia: ejecutar calcular_costo_subrecetas.py en el agente.";
  alerta("✓ Detalle subrecetas", t);
}

function promoverSubrecetas() {
  promoverSubrecetasCab();
  Utilities.sleep(500);
  promoverSubrecetasDetalle();
}

// ═══════════════════════════════════════════════════════════════════════════════
// TESTS
// ═══════════════════════════════════════════════════════════════════════════════

function datosPruebaProveedor() {
  return {
    "Razón Social": TEST_PREFIX + "Proveedor Demo S.A.",
    "RUC": "1799999999001",
    "¿Es proveedor de inventario?": "SI",
    "Correo electrónico": "demo@proveedor.com",
    "¿Activo?": "SI",
    "WhatsApp de contacto": "593999000001",
    "Dirección": "Av. Test 123, Cuenca",
    "Nombre del contacto": "Juan Demo",
    "Condición de pago": "CONTADO",
    "Días de crédito": "0",
    "Lead time (días)": "2",
    "Frecuencia de compra (días)": "7",
    "Días de pedido": "LUN, MIE, VIE",
    "Observaciones": "Fila de prueba — eliminar",
    "estado": "PENDIENTE",
    "cod_proveedor": "PROV-TEST",
  };
}

function datosPruebaMP() {
  return {
    "Nombre de la materia prima": TEST_PREFIX + "Ingrediente Demo",
    "Categoría": "COCINA",
    "Unidad base": "gr",
    "Bodega": "Cocina (BOD-001)",
    "Tipo de control": "PESO",
    "Dias de Seguridad": "3",
    "Activa": "SI",
    "estado": "PENDIENTE",
    "cod_mp_sistema": "999",
  };
}

function datosPruebaRecetaV2() {
  return {
    "nombre_receta": TEST_PREFIX + "Receta Demo",
    "cod_receta": "999",
    "variedad_smart_menu": "",
    "tipo_linea": "MP",
    "nombre_mp": "AJO",
    "cod_mp_sistema": "063",
    "nombre_subreceta": "",
    "cod_subreceta": "",
    "cantidad": "5",
    "unidad_base": "gr",
    "cod_bodega": "BOD-001",
    "merma_pct": "0",
    "es_opcional": "NO",
    "pct_aplicacion": "100",
    "estado": "PENDIENTE",
  };
}

function datosPruebaRecetaV1() {
  return {
    "nombre_receta": TEST_PREFIX + "Receta Demo",
    "cod_receta": "999",
    "variedad_smart_menu": "NORMAL",
    "nombre_mp": "AJO",
    "cod_mp_sistema": "063",
    "cantidad": "5",
    "es_opcional": "NO",
    "pct_aplicacion": "100",
    "merma_pct": "5",
    "estado": "PENDIENTE",
  };
}

function datosPruebaSubCab() {
  return {
    "nombre_subreceta": TEST_PREFIX + "Semi Demo",
    "cod_subreceta": "998",
    "rendimiento_estandar": "1000",
    "unidad": "gr",
    "activa": "SI",
    "notas": "Prueba staging",
    "estado": "PENDIENTE",
  };
}

function datosPruebaSubDet() {
  return {
    "nombre_subreceta_padre": TEST_PREFIX + "Semi Demo",
    "cod_subreceta_padre": "998",
    "tipo_linea": "MP",
    "nombre_mp": "AJO",
    "cod_mp_sistema": "063",
    "nombre_subreceta_hijo": "",
    "cod_subreceta_hijo": "",
    "cantidad": "50",
    "unidad_base": "gr",
    "cod_bodega": "BOD-001",
    "merma_pct": "0",
    "estado": "PENDIENTE",
  };
}

function datosPruebaProducto() {
  return {
    "nombre_producto": TEST_PREFIX + "Producto Demo",
    "cod_smart_menu": "TEST-999",
    "variedad_smart_menu": "NORMAL",
    "cod_receta": "999",
    "categoria_menu": "BAOS",
    "precio_venta": "9.99",
    "activo": "SI",
    "version": "1",
    "fecha_vigencia": "2026-01-01",
    "rendimiento": "1",
    "descarga_inventario": "SI",
    "estado": "PENDIENTE",
  };
}

function insertarDatosPrueba() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const resultados = [];

  const tieneRecetasV2 = !!staging.getSheetByName("STAGING_RECETAS") &&
    staging.getSheetByName("STAGING_RECETAS").getRange(1, 1, 1, 20).getValues()[0]
      .map(h => h.toString().trim()).indexOf("tipo_linea") !== -1;

  const inserciones = [
    { hoja: "STAGING_PROVEEDORES", datos: datosPruebaProveedor() },
    { hoja: "STAGING_MP", datos: datosPruebaMP() },
    { hoja: "STAGING_RECETAS", datos: tieneRecetasV2 ? datosPruebaRecetaV2() : datosPruebaRecetaV1() },
    { hoja: "STAGING_SUB_CAB", datos: datosPruebaSubCab() },
    { hoja: "STAGING_SUB_DETALLE", datos: datosPruebaSubDet() },
    { hoja: "STAGING_PRODUCTOS", datos: datosPruebaProducto() },
  ];

  inserciones.forEach(({ hoja: nombreHoja, datos }) => {
    try {
      const hoja = staging.getSheetByName(nombreHoja);
      if (!hoja) { resultados.push(`⊘ ${nombreHoja}: no existe (omitida)`); return; }
      const headers = hoja.getRange(1, 1, 1, hoja.getLastColumn()).getValues()[0]
        .map(h => h.toString().trim());
      const fila = headers.map(h => datos[h] ?? "");
      hoja.appendRow(fila);
      resultados.push(`✓ ${nombreHoja}: fila ${hoja.getLastRow()}`);
    } catch (e) {
      resultados.push(`❌ ${nombreHoja}: ${e.message}`);
    }
  });

  alerta("Datos de prueba insertados", resultados.join("\n"));
}

function aprobarFilasPrueba() {
  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const resultados = [];

  const config = [
    { hoja: "STAGING_PROVEEDORES", pk: "Razón Social", testVals: [TEST_PREFIX] },
    { hoja: "STAGING_MP", pk: "Nombre de la materia prima", testVals: [TEST_PREFIX] },
    { hoja: "STAGING_RECETAS", pk: "nombre_receta", testVals: [TEST_PREFIX] },
    { hoja: "STAGING_SUB_CAB", pk: "nombre_subreceta", testVals: [TEST_PREFIX] },
    { hoja: "STAGING_SUB_DETALLE", pk: "cod_subreceta_padre", testVals: ["998", TEST_PREFIX] },
    { hoja: "STAGING_PRODUCTOS", pk: "nombre_producto", testVals: [TEST_PREFIX, "TEST-999"] },
  ];

  config.forEach(({ hoja: nombreHoja, pk, testVals }) => {
    try {
      const hoja = staging.getSheetByName(nombreHoja);
      if (!hoja) { resultados.push(`⊘ ${nombreHoja}: no existe`); return; }
      const datos = hoja.getDataRange().getValues();
      const norm = s => s.toLowerCase().replace(/\s+/g, "")
        .normalize("NFD").replace(/[\u0300-\u036f]/g, "");
      const headers = datos[0].map(h => h.toString().trim());
      const idxPK = headers.findIndex(h => norm(h) === norm(pk));
      const idxEstado = headers.findIndex(h => h.toLowerCase() === "estado");
      if (idxEstado === -1) { resultados.push(`❌ ${nombreHoja}: sin columna estado`); return; }

      let aprobadas = 0;
      for (let i = datos.length - 1; i >= 1; i--) {
        const val = datos[i][idxPK >= 0 ? idxPK : 0]?.toString() ?? "";
        const match = testVals.some(t => val.indexOf(t) === 0 || val === t);
        if (match) {
          hoja.getRange(i + 1, idxEstado + 1).setValue("APROBADO");
          aprobadas++;
        }
      }
      resultados.push(`✓ ${nombreHoja}: ${aprobadas} → APROBADO`);
    } catch (e) {
      resultados.push(`❌ ${nombreHoja}: ${e.message}`);
    }
  });

  alerta("Filas aprobadas", resultados.join("\n"));
}

function promoverPrueba() {
  const resultados = [];
  const steps = [
    ["Proveedores", promoverProveedores],
    ["MPs", promoverMPs],
    ["Recetas", promoverRecetas],
    ["Sub cab", promoverSubrecetasCab],
    ["Sub det", promoverSubrecetasDetalle],
    ["Productos", promoverProductos],
  ];
  steps.forEach(([name, fn]) => {
    try { fn(); resultados.push(`✓ ${name}`); }
    catch (e) { resultados.push(`❌ ${name}: ${e.message}`); }
  });
  alerta("Resultado promoción", resultados.join("\n"));
}

function verificarEnMaster() {
  const master = SpreadsheetApp.openById(MASTER_ID);
  const resultados = [];

  const checks = [
    { hoja: "BD_PROV", col: 0, pref: TEST_PREFIX },
    { hoja: "BD_MP_SISTEMA", col: 0, pref: TEST_PREFIX },
    { hoja: "BD_RECETAS_DETALLE", col: 0, pref: TEST_PREFIX },
    { hoja: "BD_SUBRECETAS", col: 0, pref: TEST_PREFIX },
    { hoja: "BD_SUBRECETAS_DETALLE", col: 0, pref: TEST_PREFIX },
    { hoja: "BD_PRODUCTOS", col: 0, pref: TEST_PREFIX },
  ];

  checks.forEach(({ hoja: nombre, col, pref }) => {
    try {
      const hoja = master.getSheetByName(nombre);
      if (!hoja) { resultados.push(`❌ ${nombre}: no en Master`); return; }
      const datos = hoja.getDataRange().getValues();
      const found = datos.filter((f, i) => i > 0 && (f[col]?.toString().startsWith(pref) || f[col] === "998"));
      if (found.length > 0) {
        resultados.push(`✓ ${nombre}: ${found.length} fila(s) TEST`);
      } else {
        resultados.push(`⚠ ${nombre}: sin filas TEST`);
      }
    } catch (e) {
      resultados.push(`❌ ${nombre}: ${e.message}`);
    }
  });

  alerta("Verificación en Master", resultados.join("\n"));
}

function limpiarDatosPrueba() {
  const ui = SpreadsheetApp.getUi();
  const resp = ui.alert(
    "⚠ Confirmar",
    "Se eliminarán filas TEST_ / cod 998-999 del staging y del Master.\n¿Continuar?",
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  const staging = SpreadsheetApp.getActiveSpreadsheet();
  const master = SpreadsheetApp.openById(MASTER_ID);
  const resultados = [];

  const esTest = val => {
    const s = (val || "").toString();
    return s.startsWith(TEST_PREFIX) || s === "998" || s === "999" || s === "PROV-TEST" || s === "TEST-999";
  };

  const limpiar = (spreadsheet, configs, prefijo) => {
    configs.forEach(({ hoja: nombre, cols }) => {
      try {
        const hoja = spreadsheet.getSheetByName(nombre);
        if (!hoja) return;
        const datos = hoja.getDataRange().getValues();
        let eliminadas = 0;
        for (let i = datos.length - 1; i >= 1; i--) {
          const hit = cols.some(c => esTest(datos[i][c]));
          if (hit) { hoja.deleteRow(i + 1); eliminadas++; }
        }
        resultados.push(`✓ ${prefijo}${nombre}: ${eliminadas} fila(s)`);
      } catch (e) {
        resultados.push(`❌ ${nombre}: ${e.message}`);
      }
    });
  };

  limpiar(staging, [
    { hoja: "STAGING_PROVEEDORES", cols: [0, 2] },
    { hoja: "STAGING_MP", cols: [0] },
    { hoja: "STAGING_RECETAS", cols: [0] },
    { hoja: "STAGING_SUB_CAB", cols: [0, 1] },
    { hoja: "STAGING_SUB_DETALLE", cols: [0, 1] },
    { hoja: "STAGING_PRODUCTOS", cols: [0] },
  ], "");

  limpiar(master, [
    { hoja: "BD_PROV", cols: [0] },
    { hoja: "BD_MP_SISTEMA", cols: [0] },
    { hoja: "BD_RECETAS_DETALLE", cols: [0] },
    { hoja: "BD_SUBRECETAS", cols: [0, 1] },
    { hoja: "BD_SUBRECETAS_DETALLE", cols: [0, 1] },
    { hoja: "BD_PRODUCTOS", cols: [0] },
  ], "Master → ");

  alerta("Limpieza completada", resultados.join("\n"));
}

function testCompleto() {
  const ui = SpreadsheetApp.getUi();
  const resp = ui.alert(
    "▶ Test Completo",
    "Insertar → Aprobar → Promover (incl. subrecetas) → Verificar\n¿Continuar?",
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  const log = [`=== TEST COMPLETO ===`, `Fecha: ${new Date().toLocaleString()}`, ""];

  log.push("── Paso 1: Insertar ──");
  try { insertarDatosPrueba(); log.push("✓ OK"); } catch (e) { log.push(`❌ ${e.message}`); }
  Utilities.sleep(1000);

  log.push("── Paso 2: Aprobar ──");
  try { aprobarFilasPrueba(); log.push("✓ OK"); } catch (e) { log.push(`❌ ${e.message}`); }
  Utilities.sleep(1000);

  log.push("── Paso 3: Promover ──");
  try {
    promoverProveedores();
    promoverMPs();
    promoverRecetas();
    promoverSubrecetasCab();
    promoverSubrecetasDetalle();
    promoverProductos();
    log.push("✓ OK");
  } catch (e) { log.push(`❌ ${e.message}`); }
  Utilities.sleep(2000);

  log.push("── Paso 4: Verificar ──");
  const master = SpreadsheetApp.openById(MASTER_ID);
  let todoOK = true;
  [
    { hoja: "BD_PROV", col: 0 },
    { hoja: "BD_MP_SISTEMA", col: 0 },
    { hoja: "BD_RECETAS_DETALLE", col: 0 },
    { hoja: "BD_SUBRECETAS", col: 0 },
    { hoja: "BD_PRODUCTOS", col: 0 },
  ].forEach(({ hoja: nombre, col }) => {
    try {
      const hoja = master.getSheetByName(nombre);
      const datos = hoja.getDataRange().getValues();
      const found = datos.filter((f, i) => i > 0 && f[col]?.toString().startsWith(TEST_PREFIX));
      if (found.length > 0) log.push(`✓ ${nombre}`);
      else { log.push(`⚠ ${nombre}: NO encontrado`); todoOK = false; }
    } catch (e) { log.push(`❌ ${nombre}: ${e.message}`); todoOK = false; }
  });

  log.push("");
  log.push(todoOK ? "✅ PASOS OK" : "⚠ REVISAR");
  log.push("Ejecuta 'Limpiar datos de prueba' al terminar.");

  ui.alert("Reporte", log.join("\n"), ui.ButtonSet.OK);
}

// ═══════════════════════════════════════════════════════════════════════════════
// INGRESO MANUAL DE FACTURAS (pestaña INGRESO_FACTURA, menú 🍣 Tatami Facturas)
//
// Configuración (⚙️ Propiedades del script):
//   TATAMI_FACTURA_API_URL = https://tatami-agente-production.up.railway.app/api/factura_manual/enviar
//   TATAMI_FACTURA_SECRET  = valor de FACTURA_SHEETS_INGEST_SECRET en Railway/.env
//
var TATAMI_FACTURA_URL_CORRECTA = "https://tatami-agente-production.up.railway.app/api/factura_manual/enviar";

var HOJA_INGRESO = "INGRESO_FACTURA";
var HOJA_REGISTRO = "REGISTRO_FACTURAS";
var FILA_LINEAS = 7;
var MAX_LINEAS = 40;

/**
 * Ejecutar UNA VEZ desde el editor ▶ (sin diálogos — evita timeout de 6 min).
 * Revisa el Registro de ejecución: debe decir "Permisos externos: OK".
 */
function solicitarPermisosExternos() {
  var resp = UrlFetchApp.fetch("https://www.google.com", {
    method: "get",
    muteHttpExceptions: true,
    followRedirects: true,
  });
  Logger.log("Permisos externos: OK (HTTP " + resp.getResponseCode() + ")");
  return resp.getResponseCode();
}

/** Menú Tatami Facturas — usar desde el Sheets (sí tiene UI). */
function solicitarPermisosExternosMenu() {
  try {
    var code = solicitarPermisosExternos();
    SpreadsheetApp.getUi().alert(
      "Permisos OK",
      "El script ya puede llamar al agente (prueba HTTP " + code + ").\n\n" +
        "Verifica TATAMI_FACTURA_API_URL y TATAMI_FACTURA_SECRET en Propiedades del script.",
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (e) {
    SpreadsheetApp.getUi().alert("Error de autorización:\n\n" + String(e));
  }
}

function probarFactura() {
  aceptarFactura_(true);
}

/** Verifica URL, secret y que el servidor tenga proveedor 165 habilitado. */
function verificarConexionFactura() {
  var ui = SpreadsheetApp.getUi();
  var props = PropertiesService.getScriptProperties();
  var url = String(props.getProperty("TATAMI_FACTURA_API_URL") || "").trim();
  var secret = String(props.getProperty("TATAMI_FACTURA_SECRET") || "").trim();

  if (!url || !secret) {
    ui.alert(
      "Falta configuración",
      "Define en Propiedades del script:\n\n" +
        "TATAMI_FACTURA_API_URL\n" +
        TATAMI_FACTURA_URL_CORRECTA + "\n\n" +
        "TATAMI_FACTURA_SECRET\n" +
        "(mismo valor que FACTURA_SHEETS_INGEST_SECRET en Railway)",
      ui.ButtonSet.OK
    );
    return;
  }

  var pingUrl = url.replace(/\/enviar\/?$/i, "/ping");
  var resp;
  try {
    resp = UrlFetchApp.fetch(pingUrl, {
      method: "get",
      headers: { "X-Tatami-Factura-Secret": secret },
      muteHttpExceptions: true,
    });
  } catch (e) {
    ui.alert("No se pudo conectar:\n\nURL ping: " + pingUrl + "\n\n" + String(e));
    return;
  }

  var code = resp.getResponseCode();
  var body = {};
  try { body = JSON.parse(resp.getContentText()); } catch (e) {}

  if (code === 401) {
    ui.alert(
      "Secret incorrecto (HTTP 401)",
      "TATAMI_FACTURA_SECRET no coincide con Railway.\n\nURL: " + url,
      ui.ButtonSet.OK
    );
    return;
  }
  if (code !== 200) {
    ui.alert(
      "Servidor incorrecto o desactualizado (HTTP " + code + ")",
      "URL configurada:\n" + url + "\n\n" +
        "Debe ser:\n" + TATAMI_FACTURA_URL_CORRECTA + "\n\n" +
        "Respuesta: " + resp.getContentText().slice(0, 400),
      ui.ButtonSet.OK
    );
    return;
  }

  var provs = (body.proveedores_manuales || []).join(", ");
  var ok165 = (body.proveedores_manuales || []).indexOf("165") !== -1;
  ui.alert(
    ok165 ? "✅ Conexión OK" : "⚠ Servidor sin proveedor 165",
    "URL: " + url + "\n\nProveedores manuales: " + provs + "\n\n" +
      (ok165
        ? "Podés ingresar facturas de Inguil Lazo (165)."
        : "Cambiá TATAMI_FACTURA_API_URL a:\n" + TATAMI_FACTURA_URL_CORRECTA),
    ui.ButtonSet.OK
  );
}

function aceptarFactura() {
  aceptarFactura_(false);
}

function aceptarFactura_(modoPrueba) {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var hoja = ss.getSheetByName(HOJA_INGRESO);

  var proveedor = String(hoja.getRange("B2").getValue() || "").trim();
  var numFactura = String(hoja.getRange("B3").getValue() || "").trim();
  var fechaRaw = hoja.getRange("B4").getValue();

  if (!proveedor) { ui.alert("Falta elegir el Proveedor (B2)."); return; }
  if (!numFactura) { ui.alert("Falta el N° de factura (B3)."); return; }
  var fecha = formatearFecha_(fechaRaw);
  if (!fecha) { ui.alert("Fecha de factura inválida (B4). Usa formato fecha."); return; }

  var datos = hoja.getRange(FILA_LINEAS, 1, MAX_LINEAS, 3).getValues();
  var lineas = [];
  var problemas = [];
  for (var i = 0; i < datos.length; i++) {
    var desc = String(datos[i][0] || "").trim();
    var cant = datos[i][1];
    var costo = datos[i][2];
    if (!desc && cant === "" && costo === "") continue;
    if (!desc) { problemas.push("Fila " + (FILA_LINEAS + i) + ": sin descripción"); continue; }
    if (!(cant > 0)) { problemas.push("Fila " + (FILA_LINEAS + i) + ": cantidad inválida"); continue; }
    if (!(costo >= 0)) { problemas.push("Fila " + (FILA_LINEAS + i) + ": costo inválido"); continue; }
    lineas.push({ descripcion: desc, cantidad: Number(cant), costo_unitario: Number(costo) });
  }

  if (problemas.length) { ui.alert("Corrige antes de aceptar:\n\n" + problemas.join("\n")); return; }
  if (!lineas.length) { ui.alert("No hay líneas para ingresar."); return; }

  var total = lineas.reduce(function (s, l) { return s + l.cantidad * l.costo_unitario; }, 0);
  if (!modoPrueba) {
    var conf = ui.alert(
      "Confirmar ingreso",
      "Proveedor: " + proveedor + "\nFactura: " + numFactura + " (" + fecha + ")\n" +
      "Líneas: " + lineas.length + "\nTotal: $" + total.toFixed(2) + "\n\n¿Ingresar al inventario?",
      ui.ButtonSet.YES_NO
    );
    if (conf !== ui.Button.YES) return;
  }

  var usuario = Session.getActiveUser().getEmail() || "desconocido";
  var payload = {
    usuario: usuario,
    proveedor: proveedor,
    num_factura: numFactura,
    fecha_factura: fecha,
    idempotency_key: proveedor + "|" + numFactura,
    modo_prueba: !!modoPrueba,
    lineas: lineas,
  };

  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty("TATAMI_FACTURA_API_URL");
  var secret = props.getProperty("TATAMI_FACTURA_SECRET");
  if (!url || !secret) {
    ui.alert("Falta configurar TATAMI_FACTURA_API_URL / TATAMI_FACTURA_SECRET en Propiedades del script.");
    return;
  }

  var resp;
  try {
    resp = UrlFetchApp.fetch(url, {
      method: "post",
      contentType: "application/json; charset=utf-8",
      payload: JSON.stringify(payload),
      headers: { "X-Tatami-Factura-Secret": secret },
      muteHttpExceptions: true,
    });
  } catch (e) {
    var msg = String(e);
    if (msg.indexOf("UrlFetchApp") !== -1 || msg.indexOf("external_request") !== -1) {
      ui.alert(
        "Falta autorizar el script para conectar con el agente.\n\n" +
          "Menú 🍣 Tatami Facturas → «Autorizar conexión con agente»\n" +
          "(o en Apps Script ejecuta solicitarPermisosExternos y acepta permisos)\n\n" +
          msg
      );
    } else {
      ui.alert("No se pudo conectar con el agente:\n" + msg);
    }
    return;
  }

  var code = resp.getResponseCode();
  var body = {};
  try { body = JSON.parse(resp.getContentText()); } catch (e) {}

  if (code !== 200) {
    var detalle = (body && body.detail) ? JSON.stringify(body.detail) : resp.getContentText().slice(0, 300);
    ui.alert(
      "El agente rechazó la factura (HTTP " + code + "):\n\n" + detalle +
        "\n\n—\nURL usada:\n" + url +
        "\n\nSi falta proveedor 165: menú 🔍 Verificar URL y proveedores"
    );
    return;
  }

  if (modoPrueba) {
    var det = (body.lineas || []).map(function (l) {
      var extra = l.estado === "OK_PRUEBA"
        ? " → +" + l.entraria_stock + " " + (l.unidad_base || "") + " (MP " + l.cod_mp_sistema + ")"
        : " ⚠ " + l.estado;
      return "• " + l.descripcion + extra;
    }).join("\n");
    ui.alert(
      "🧪 PRUEBA — nada se ingresó al stock\n\n" +
      "Factura: " + numFactura + " | Total: $" + total.toFixed(2) + "\n\n" + det +
      "\n\n(La pestaña no se limpió; usa Aceptar cuando sea real.)"
    );
    return;
  }

  var trx = body.trx || "";
  registrarHistorial_(ss, trx, usuario, proveedor, numFactura, fecha, body.lineas || lineas);

  if (body.errores && body.errores > 0) {
    ui.alert(
      "Ingreso PARCIAL — TRX " + trx + "\n\n" +
      body.entradas + " líneas ingresadas, " + body.errores + " con error.\n" +
      "Revisa REGISTRO_FACTURAS (no se limpió la pestaña)."
    );
    return;
  }

  limpiarIngreso_(hoja);
  ui.alert("✅ Factura ingresada al inventario.\n\nCódigo de transacción: " + trx +
           "\nLíneas: " + body.entradas);
}

function registrarHistorial_(ss, trx, usuario, proveedor, numFactura, fecha, lineas) {
  var reg = ss.getSheetByName(HOJA_REGISTRO);
  var ahora = Utilities.formatDate(new Date(), "America/Guayaquil", "yyyy-MM-dd HH:mm:ss");
  var codProv = (proveedor.match(/^\d+/) || [""])[0];
  var filas = [];
  for (var i = 0; i < lineas.length; i++) {
    var l = lineas[i];
    filas.push([
      trx,
      ahora,
      usuario,
      codProv,
      proveedor,
      numFactura,
      fecha,
      l.descripcion,
      l.cantidad,
      l.costo_unitario,
      Math.round(l.cantidad * l.costo_unitario * 100) / 100,
      l.cod_mp_sistema || "",
      l.estado || "OK",
    ]);
  }
  if (filas.length) {
    reg.getRange(reg.getLastRow() + 1, 1, filas.length, filas[0].length).setValues(filas);
  }
}

function limpiarIngreso_(hoja) {
  hoja.getRange("B2:B4").clearContent();
  hoja.getRange(FILA_LINEAS, 1, MAX_LINEAS, 3).clearContent();
}

function formatearFecha_(v) {
  if (v instanceof Date && !isNaN(v)) {
    return Utilities.formatDate(v, "America/Guayaquil", "yyyy-MM-dd");
  }
  var s = String(v || "").trim();
  var m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (m) return m[3] + "-" + ("0" + m[2]).slice(-2) + "-" + ("0" + m[1]).slice(-2);
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  return "";
}

// ═══════════════════════════════════════════════════════════════════════════════
// TRASLADOS MASIVOS (pestaña INGRESO_TRASLADO, menú 📦 Tatami Traslados)
//
// Propiedades del script:
//   TATAMI_TRASLADO_API_URL = https://tatami-agente-production.up.railway.app/api/traslado_masivo/enviar
//   TATAMI_TRASLADO_SECRET  = TRASLADO_SHEETS_INGEST_SECRET en Railway
//
var TATAMI_TRASLADO_URL_CORRECTA = "https://tatami-agente-production.up.railway.app/api/traslado_masivo/enviar";
var HOJA_TRASLADO = "INGRESO_TRASLADO";
var HOJA_REG_TRASLADO = "REGISTRO_TRASLADOS";
var HOJA_CAT_TRASLADO = "CAT_TRASLADO";
var FILA_LINEAS_TRASLADO = 6;
var MAX_LINEAS_TRASLADO = 50;

/** Ya no filtra al cambiar B2: puede llenar bodegas y productos en cualquier orden. */
function onEdit(e) {
  // Sin accion automatica en INGRESO_TRASLADO (evita invalidar productos ya elegidos).
}

/** Escribe en H2:H los productos de CAT_TRASLADO filtrados por bodega origen (F1/B2). */
function actualizarListaProductosTraslado() {
  actualizarListaProductosTraslado_(true);
}

/** Vuelve a cargar en H todos los productos (sin filtrar por bodega). */
function restaurarListaCompletaTraslado() {
  restaurarListaCompletaTraslado_(true);
}

function restaurarListaCompletaTraslado_(mostrarAlerta) {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ing = ss.getSheetByName(HOJA_TRASLADO);
  var cat = ss.getSheetByName(HOJA_CAT_TRASLADO);
  if (!ing || !cat) {
    if (mostrarAlerta) ui.alert("Faltan hojas INGRESO_TRASLADO o CAT_TRASLADO. Ejecuta setup_ingreso_traslado_masivo.py");
    return;
  }
  var last = cat.getLastRow();
  if (last < 2) return;
  var data = cat.getRange(2, 2, last - 1, 1).getValues();
  var vistos = {};
  var list = [];
  for (var i = 0; i < data.length; i++) {
    var et = String(data[i][0] || "").trim();
    if (et && !vistos[et]) {
      vistos[et] = true;
      list.push([et]);
    }
  }
  ing.getRange("H2:H2000").clearContent();
  if (list.length) ing.getRange(2, 8, list.length, 1).setValues(list);
  if (mostrarAlerta) ui.alert("Lista completa: " + list.length + " productos.");
}

function actualizarListaProductosTraslado_(mostrarAlerta) {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ing = ss.getSheetByName(HOJA_TRASLADO);
  var cat = ss.getSheetByName(HOJA_CAT_TRASLADO);
  if (!ing) {
    if (mostrarAlerta) ui.alert("No existe " + HOJA_TRASLADO + ". Ejecuta: python setup_ingreso_traslado_masivo.py");
    return;
  }
  if (!cat) {
    if (mostrarAlerta) ui.alert("No existe " + HOJA_CAT_TRASLADO + ". Ejecuta: python setup_ingreso_traslado_masivo.py");
    return;
  }
  var bod = String(ing.getRange("F1").getValue() || "").trim();
  if (!bod) {
    var txt = String(ing.getRange("B2").getValue() || "");
    var m = txt.match(/BOD-\d{3}/i);
    bod = m ? m[0].toUpperCase() : "";
    if (bod) ing.getRange("F1").setValue(bod);
  }
  if (!bod) {
    if (mostrarAlerta) ui.alert("Elija primero la bodega origen en B2.");
    return;
  }
  var last = cat.getLastRow();
  if (last < 2) {
    if (mostrarAlerta) ui.alert("CAT_TRASLADO vacio. Ejecuta setup_ingreso_traslado_masivo.py");
    return;
  }
  var data = cat.getRange(2, 1, last - 1, 2).getValues();
  var list = [];
  for (var i = 0; i < data.length; i++) {
    if (String(data[i][0]).trim() === bod && data[i][1]) {
      list.push([String(data[i][1]).trim()]);
    }
  }
  ing.getRange("H2:H2000").clearContent();
  if (!list.length) {
    if (mostrarAlerta) ui.alert("Sin productos para " + bod + " en CAT_TRASLADO.");
    return;
  }
  ing.getRange(2, 8, list.length, 1).setValues(list);
  if (mostrarAlerta) ui.alert("Lista actualizada: " + list.length + " productos para " + bod);
}

function extraerBodega_(texto) {
  var m = String(texto || "").match(/BOD-\d{3}/i);
  return m ? m[0].toUpperCase() : "";
}

function verificarConexionTraslado() {
  var ui = SpreadsheetApp.getUi();
  var props = PropertiesService.getScriptProperties();
  var url = String(props.getProperty("TATAMI_TRASLADO_API_URL") || "").trim();
  var secret = String(props.getProperty("TATAMI_TRASLADO_SECRET") || "").trim();
  if (!url || !secret) {
    ui.alert(
      "Falta configuración",
      "Define en Propiedades del script:\n\n" +
        "TATAMI_TRASLADO_API_URL\n" + TATAMI_TRASLADO_URL_CORRECTA + "\n\n" +
        "TATAMI_TRASLADO_SECRET\n" +
        "(mismo valor que TRASLADO_SHEETS_INGEST_SECRET en Railway)",
      ui.ButtonSet.OK
    );
    return;
  }
  var pingUrl = url.replace(/\/enviar\/?$/i, "/ping");
  var resp;
  try {
    resp = UrlFetchApp.fetch(pingUrl, {
      method: "get",
      headers: { "X-Tatami-Traslado-Secret": secret },
      muteHttpExceptions: true,
    });
  } catch (e) {
    ui.alert("No se pudo conectar:\n\n" + pingUrl + "\n\n" + String(e));
    return;
  }
  var code = resp.getResponseCode();
  var body = {};
  try { body = JSON.parse(resp.getContentText()); } catch (e) {}
  if (code === 401) {
    ui.alert("Secret incorrecto (HTTP 401)", "Revisa TATAMI_TRASLADO_SECRET", ui.ButtonSet.OK);
    return;
  }
  if (code !== 200) {
    ui.alert("Error HTTP " + code, resp.getContentText().slice(0, 400), ui.ButtonSet.OK);
    return;
  }
  var pares = (body.pares_traslado || []).join(", ");
  ui.alert(
    "✅ Conexión OK",
    "Pares permitidos: " + pares + "\n\nCorreos autorizados: " + (body.emails_autorizados || 0),
    ui.ButtonSet.OK
  );
}

function probarTraslado() {
  aceptarTraslado_(true);
}

function aceptarTraslado() {
  aceptarTraslado_(false);
}

function aceptarTraslado_(modoPrueba) {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var hoja = ss.getSheetByName(HOJA_TRASLADO);
  if (!hoja) {
    ui.alert("No existe la hoja " + HOJA_TRASLADO + ". Ejecuta setup_ingreso_traslado_masivo.py");
    return;
  }

  var origenTxt = String(hoja.getRange("B2").getValue() || "").trim();
  var destinoTxt = String(hoja.getRange("B3").getValue() || "").trim();
  var bodegaOrigen = extraerBodega_(origenTxt);
  var bodegaDestino = extraerBodega_(destinoTxt);
  if (!bodegaOrigen) { ui.alert("Falta bodega origen (B2)."); return; }
  if (!bodegaDestino) { ui.alert("Falta bodega destino (B3)."); return; }
  if (bodegaOrigen === bodegaDestino) { ui.alert("Origen y destino no pueden ser iguales."); return; }

  var datos = hoja.getRange(FILA_LINEAS_TRASLADO, 1, MAX_LINEAS_TRASLADO, 2).getValues();
  var lineas = [];
  var problemas = [];
  for (var i = 0; i < datos.length; i++) {
    var prod = String(datos[i][0] || "").trim();
    var cant = datos[i][1];
    if (!prod && cant === "") continue;
    if (!prod) { problemas.push("Fila " + (FILA_LINEAS_TRASLADO + i) + ": sin producto"); continue; }
    if (!(cant > 0)) { problemas.push("Fila " + (FILA_LINEAS_TRASLADO + i) + ": cantidad inválida"); continue; }
    lineas.push({ producto: prod, cantidad: Number(cant) });
  }
  if (problemas.length) { ui.alert("Corrige antes de aceptar:\n\n" + problemas.join("\n")); return; }
  if (!lineas.length) { ui.alert("No hay líneas para trasladar."); return; }

  if (!modoPrueba) {
    var conf = ui.alert(
      "Confirmar traslado",
      "Origen: " + origenTxt + "\nDestino: " + destinoTxt + "\nLíneas: " + lineas.length +
        "\n\nSe permite stock negativo en origen.\n¿Registrar traslados?",
      ui.ButtonSet.YES_NO
    );
    if (conf !== ui.Button.YES) return;
  }

  var usuario = Session.getActiveUser().getEmail() || "desconocido";
  var payload = {
    usuario: usuario,
    bodega_origen: bodegaOrigen,
    bodega_destino: bodegaDestino,
    modo_prueba: !!modoPrueba,
    lineas: lineas,
  };

  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty("TATAMI_TRASLADO_API_URL");
  var secret = props.getProperty("TATAMI_TRASLADO_SECRET");
  if (!url || !secret) {
    ui.alert("Falta TATAMI_TRASLADO_API_URL / TATAMI_TRASLADO_SECRET en Propiedades del script.");
    return;
  }

  var resp;
  try {
    resp = UrlFetchApp.fetch(url, {
      method: "post",
      contentType: "application/json; charset=utf-8",
      payload: JSON.stringify(payload),
      headers: { "X-Tatami-Traslado-Secret": secret },
      muteHttpExceptions: true,
    });
  } catch (e) {
    ui.alert("No se pudo conectar con el agente:\n" + String(e));
    return;
  }

  var code = resp.getResponseCode();
  var body = {};
  try { body = JSON.parse(resp.getContentText()); } catch (e) {}

  if (code === 403) {
    ui.alert("Correo no autorizado:\n\n" + usuario + "\n\nPide que agreguen tu correo en TRASLADO_SHEETS_EMAILS.");
    return;
  }
  if (code !== 200) {
    var detalle = (body && body.detail) ? JSON.stringify(body.detail) : resp.getContentText().slice(0, 400);
    ui.alert("El agente rechazó el traslado (HTTP " + code + "):\n\n" + detalle);
    return;
  }

  if (modoPrueba) {
    var det = (body.lineas || []).map(function (l) {
      var extra = l.estado === "OK_PRUEBA"
        ? " → " + l.cantidad + " " + (l.unidad_base || "") + " (stock origen quedaría " + l.stock_origen_despues + ")"
        : " ⚠ " + l.estado + (l.detalle ? ": " + l.detalle : "");
      return "• " + (l.producto || l.nombre_mp) + extra;
    }).join("\n");
    ui.alert("🧪 PRUEBA — nada se movió\n\n" + det);
    return;
  }

  var trx = body.trx || "";
  registrarHistorialTraslado_(ss, trx, usuario, bodegaOrigen, bodegaDestino, body.lineas || lineas);

  if (body.errores && body.errores > 0) {
    ui.alert(
      "Traslado PARCIAL — " + trx + "\n\n" +
      body.traslados + " OK, " + body.errores + " con error.\nRevisa REGISTRO_TRASLADOS."
    );
    return;
  }

  limpiarTraslado_(hoja);
  ui.alert("✅ Traslados registrados.\n\nCódigo: " + trx + "\nLíneas: " + body.traslados);
}

function registrarHistorialTraslado_(ss, trx, usuario, origen, destino, lineas) {
  var reg = ss.getSheetByName(HOJA_REG_TRASLADO);
  if (!reg) return;
  var ahora = Utilities.formatDate(new Date(), "America/Guayaquil", "yyyy-MM-dd HH:mm:ss");
  var filas = [];
  for (var i = 0; i < lineas.length; i++) {
    var l = lineas[i];
    filas.push([
      trx,
      ahora,
      usuario,
      origen,
      destino,
      l.producto || l.nombre_mp || "",
      l.cod_mp_sistema || "",
      l.cantidad,
      l.unidad_base || "",
      l.lote_referencial || "",
      l.cod_mov || "",
      l.estado || "OK",
    ]);
  }
  if (filas.length) {
    var startRow = reg.getLastRow() + 1;
    reg.getRange(startRow, 1, filas.length, filas[0].length).setValues(filas);
  }
}

function limpiarTraslado_(hoja) {
  hoja.getRange("B2:B3").clearContent();
  hoja.getRange(FILA_LINEAS_TRASLADO, 1, MAX_LINEAS_TRASLADO, 2).clearContent();
}
