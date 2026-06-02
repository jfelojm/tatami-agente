/**
 * =============================================================================
 * TATAMI — Script unificado (maestro de datos + plantilla CONTEO)
 * =============================================================================
 *
 * INSTALACIÓN (reemplazar todo el código del proyecto Apps Script):
 *   1. Abrir el libro en Google Sheets → Extensiones → Apps Script.
 *   2. Borrar los archivos .gs viejos (Code.gs, promover_…, conteo_…, etc.).
 *   3. Crear un solo archivo: tatami_maestro_unificado.gs → pegar TODO este archivo.
 *   4. Guardar (Ctrl+S) → cerrar → recargar el libro (F5).
 *   5. Deben aparecer dos menús: «Tatami» y «Conteo».
 *
 * BOTONES (dibujos) — asignar secuencia de comandos:
 *   • Promover materiales  → promoverPendientesAItemsProv
 *   • Simular promoción    → promoverPendientesAItemsProvSimular
 *   • Enviar conteo        → enviarConteoATatami   (solo en hoja plantilla CONTEO)
 *
 * CONTEO — Propiedades del script (⚙️ Ajustes del proyecto):
 *   TATAMI_CONTEO_API_URL  = https://tu-host/api/conteo/enviar
 *   TATAMI_CONTEO_SECRET   = mismo valor que CONTEO_SHEETS_INGEST_SECRET en .env
 *
 * Equivalente Python: promover_pendientes_a_items_prov.py
 * =============================================================================
 */

// ── Menús (único onOpen) ───────────────────────────────────────────────────

function onOpen() {
  tatamiAgregarMenu_();
  conteoAgregarMenu_();
}

function tatamiAgregarMenu_() {
  SpreadsheetApp.getUi()
    .createMenu('Tatami')
    .addItem('Promover pendientes → BD_ITEMS_PROV', 'promoverPendientesAItemsProv')
    .addItem('Simular promoción (sin escribir)', 'promoverPendientesAItemsProvSimular')
    .addToUi();
}

function conteoAgregarMenu_() {
  SpreadsheetApp.getUi()
    .createMenu('Conteo')
    .addItem('Enviar a Tatami', 'enviarConteoATatami')
    .addItem('Exportar JSON (respaldo)', 'exportarJsonConteo')
    .addToUi();
}

// =============================================================================
// PROMOVER BD_ITEMS_PENDIENTES → BD_ITEMS_PROV
// =============================================================================

var SHEET_PEND = 'BD_ITEMS_PENDIENTES';
var SHEET_PROV = 'BD_ITEMS_PROV';
var SHEET_MP = 'BD_MP_SISTEMA';

function promoverPendientesAItemsProv() {
  promoverPendientesAItemsProvCore_(false);
}

function promoverPendientesAItemsProvSimular() {
  promoverPendientesAItemsProvCore_(true);
}

function promoverPendientesAItemsProvCore_(dryRun) {
  var ui = SpreadsheetApp.getUi();
  var titulo = dryRun ? 'Simular promoción' : 'Promover a BD_ITEMS_PROV';
  var msg =
    'Procesa filas con estado PENDIENTE que tengan cod_mp_asignado, cod_proveedor y cod_item_xml.\n\n' +
    (dryRun
      ? 'Modo simulación: no escribe en las hojas.'
      : 'Las altas nuevas y los duplicados ya en catálogo quedarán en REGISTRADO.');
  var confirm = ui.alert(titulo, msg, ui.ButtonSet.YES_NO);
  if (confirm !== ui.Button.YES) {
    return;
  }

  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(15000)) {
    ui.alert(
      titulo,
      'Otra promoción está en curso. Espere unos segundos e intente de nuevo.',
      ui.ButtonSet.OK
    );
    return;
  }

  try {
    var res = ejecutarPromocionPendientes_(dryRun);
    var detalle = res.lineas.join('\n');
    if (detalle.length > 3500) {
      detalle = detalle.substring(0, 3500) + '\n… (ver Ejecuciones en Apps Script)';
    }
    ui.alert(
      titulo,
      res.resumen + (detalle ? '\n\n' + detalle : ''),
      ui.ButtonSet.OK
    );
    if (!dryRun && res.insertadas > 0) {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'Promoción lista: ' + res.insertadas + ' alta(s) en BD_ITEMS_PROV',
        'Tatami',
        8
      );
    }
  } catch (e) {
    ui.alert(titulo, 'Error: ' + e.message, ui.ButtonSet.OK);
    throw e;
  } finally {
    lock.releaseLock();
  }
}

function ejecutarPromocionPendientes_(dryRun) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var wsP = ss.getSheetByName(SHEET_PEND);
  var wsProv = ss.getSheetByName(SHEET_PROV);
  var wsMp = ss.getSheetByName(SHEET_MP);
  if (!wsP || !wsProv || !wsMp) {
    throw new Error(
      'Faltan hojas: ' +
        [wsP ? null : SHEET_PEND, wsProv ? null : SHEET_PROV, wsMp ? null : SHEET_MP]
          .filter(Boolean)
          .join(', ')
    );
  }

  var valsPend = wsP.getDataRange().getValues();
  var hiPend = findHeaderRow_(valsPend, 'clave_unica');
  if (hiPend < 0) {
    hiPend = findHeaderRow_(valsPend, 'cod_item_xml');
  }
  if (hiPend < 0) {
    throw new Error('No se encontró cabecera en ' + SHEET_PEND);
  }
  var headersPend = rowToHeaders_(valsPend[hiPend]);
  var idxEstado = headersPend.indexOf('estado');
  if (idxEstado < 0) {
    throw new Error('Columna estado no encontrada en ' + SHEET_PEND);
  }

  var valsProv = wsProv.getDataRange().getValues();
  var hiProv = findHeaderRow_(valsProv, 'cod_item_prov');
  if (hiProv < 0) {
    throw new Error('No se encontró cabecera cod_item_prov en ' + SHEET_PROV);
  }
  var headersProv = rowToHeaders_(valsProv[hiProv]);
  var itemsProv = loadItemsProv_(valsProv, hiProv, headersProv);

  var mpLookup = loadMpLookup_(wsMp.getDataRange().getValues());

  var insertadas = 0;
  var omitidas = 0;
  var erroresMp = 0;
  var incompletas = 0;
  var sinCodMp = 0;
  var filasAppend = [];
  var estadoUpdates = [];
  var lineas = [];

  for (var i = hiPend + 1; i < valsPend.length; i++) {
    var sheetRow = i + 1;
    var pend = rowToDict_(valsPend[i], headersPend);
    var estado = String(pend.estado || '')
      .trim()
      .toUpperCase();
    if (estado !== 'PENDIENTE') {
      continue;
    }

    var codMp = String(pend.cod_mp_asignado || '').trim();
    var codProv = String(pend.cod_proveedor || '').trim();
    var codXml = String(pend.cod_item_xml || '').trim();
    var razon = String(pend.razon_social || '').trim();
    var ruc = String(pend.ruc_proveedor || '').trim();

    if (!codMp || !codProv || !codXml) {
      if (!codMp && codProv && codXml) {
        sinCodMp++;
        if (sinCodMp <= 5) {
          lineas.push(
            'fila ' +
              sheetRow +
              ': falta cod_mp_asignado (columna vacía → "NO EN BD" en nombre MP)'
          );
        }
      } else if (codMp && (!codProv || !codXml)) {
        incompletas++;
        lineas.push(
          'fila ' +
            sheetRow +
            ': falta ' +
            (!codProv ? 'cod_proveedor' : 'cod_item_xml')
        );
      } else if (!codProv || !codXml) {
        incompletas++;
      }
      continue;
    }

    if (yaExisteEnProv_(itemsProv, codProv, codXml, razon, ruc)) {
      omitidas++;
      lineas.push(
        'fila ' + sheetRow + ': ya en catálogo (' + codProv + ' / ' + codXml + ')'
      );
      estadoUpdates.push({ row: sheetRow, col: idxEstado + 1, value: 'REGISTRADO' });
      continue;
    }

    var mp = mpLookup[codMp];
    if (!mp && codMp) {
      var codAlt = codMp.replace(/^0+/, '') || '0';
      mp = mpLookup[codAlt];
    }
    if (!mp) {
      erroresMp++;
      lineas.push('fila ' + sheetRow + ': cod_mp ' + codMp + ' no está en BD_MP_SISTEMA');
      continue;
    }

    var nueva = armarFilaProv_(headersProv, pend, mp);
    if (dryRun) {
      insertadas++;
      lineas.push(
        '[simulación] fila ' + sheetRow + ': ' + codProv + ' | ' + codXml + ' → ' + codMp
      );
      continue;
    }

    filasAppend.push(nueva);
    itemsProv.push(dictFromRow_(headersProv, nueva));
    estadoUpdates.push({ row: sheetRow, col: idxEstado + 1, value: 'REGISTRADO' });
    insertadas++;
    lineas.push('fila ' + sheetRow + ': alta ' + codProv + ' | ' + codXml + ' → ' + codMp);
  }

  if (!dryRun && filasAppend.length > 0) {
    var startRow = findNextRowItemsProv_(valsProv, hiProv, headersProv);
    var numFilas = filasAppend.length;
    var numCols = headersProv.length;
    wsProv.getRange(startRow, 1, numFilas, numCols).setValues(filasAppend);
    lineas.push(
      '→ BD_ITEMS_PROV: filas ' + startRow + '-' + (startRow + numFilas - 1)
    );
  }

  if (!dryRun && estadoUpdates.length > 0) {
    for (var u = 0; u < estadoUpdates.length; u++) {
      var up = estadoUpdates[u];
      wsP.getRange(up.row, up.col).setValue(up.value);
    }
  }

  var resumen =
    (dryRun ? 'Simulación — ' : '') +
    'insertadas=' +
    insertadas +
    ' duplicados_marcados=' +
    omitidas +
    ' sin_mp=' +
    erroresMp +
    ' sin_cod_mp=' +
    sinCodMp +
    ' incompletas=' +
    incompletas;

  if (
    insertadas === 0 &&
    omitidas === 0 &&
    erroresMp === 0 &&
    incompletas === 0 &&
    sinCodMp > 0
  ) {
    resumen +=
      '\n\nAsigne cod_mp_sistema en la columna cod_mp_asignado de cada fila PENDIENTE.';
  }

  return {
    resumen: resumen,
    insertadas: insertadas,
    omitidas: omitidas,
    erroresMp: erroresMp,
    incompletas: incompletas,
    sinCodMp: sinCodMp,
    lineas: lineas,
  };
}

function normalizarCodProveedorParaMatch_(cod) {
  return String(cod || '')
    .trim()
    .replace(/^'/, '')
    .replace(/\s+/g, '');
}

function rucNormalizado_(ruc) {
  var digits = String(ruc || '')
    .trim()
    .replace(/^'/, '')
    .replace(/\D/g, '');
  if (!digits) {
    return '';
  }
  while (digits.length < 13) {
    digits = '0' + digits;
  }
  return digits;
}

function aplicaStripSufijoOrden_(razon, ruc, codProv) {
  var u = String(razon || '')
    .trim()
    .toUpperCase();
  if (u.indexOf('COLEMUN') >= 0) {
    return true;
  }
  if (rucNormalizado_(ruc) === '0992613092001') {
    return true;
  }
  return normalizarCodProveedorParaMatch_(codProv) === '123';
}

function normalizarCodItemParaMatch_(cod, razon, ruc, codProv) {
  var s = String(cod || '')
    .trim()
    .replace(/^'/, '')
    .replace(/\s+/g, '');
  if (aplicaStripSufijoOrden_(razon, ruc, codProv)) {
    s = s.replace(/-\d+$/, '');
  }
  s = s.replace(/^0+/, '');
  return s;
}

function yaExisteEnProv_(items, codProv, codXml, razon, ruc) {
  var want = normalizarCodItemParaMatch_(codXml, razon, ruc, codProv);
  var provNorm = normalizarCodProveedorParaMatch_(codProv);
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    if (normalizarCodProveedorParaMatch_(it.cod_proveedor) !== provNorm) {
      continue;
    }
    var got = normalizarCodItemParaMatch_(it.cod_item_prov, razon, ruc, codProv);
    if (got === want) {
      return true;
    }
  }
  return false;
}

function findHeaderRow_(values, marker) {
  for (var i = 0; i < values.length; i++) {
    var row = values[i];
    for (var j = 0; j < row.length; j++) {
      if (String(row[j] || '').trim() === marker) {
        return i;
      }
    }
  }
  return -1;
}

function rowToHeaders_(row) {
  var out = [];
  for (var i = 0; i < row.length; i++) {
    out.push(String(row[i] || '').trim());
  }
  return out;
}

function rowToDict_(row, headers) {
  var d = {};
  for (var j = 0; j < headers.length; j++) {
    if (!headers[j]) {
      continue;
    }
    d[headers[j]] = j < row.length ? String(row[j] == null ? '' : row[j]).trim() : '';
  }
  return d;
}

function dictFromRow_(headers, row) {
  var d = {};
  for (var j = 0; j < headers.length; j++) {
    d[headers[j]] = row[j];
  }
  return d;
}

function findNextRowItemsProv_(values, hi, headers) {
  var icItem = headers.indexOf('cod_item_prov');
  var icProv = headers.indexOf('cod_proveedor');
  var icMp = headers.indexOf('cod_mp_sistema');
  var lastDataIdx = hi;
  for (var i = hi + 1; i < values.length; i++) {
    var row = values[i];
    if (!row) {
      continue;
    }
    if (String(row[0] || '').trim().indexOf('[') === 0) {
      continue;
    }
    if (filaTieneDatosCatalogo_(row, icItem, icProv, icMp)) {
      lastDataIdx = i;
    }
  }
  return lastDataIdx + 2;
}

function filaTieneDatosCatalogo_(row, icItem, icProv, icMp) {
  if (icItem >= 0 && icItem < row.length && String(row[icItem] || '').trim()) {
    return true;
  }
  if (icProv >= 0 && icProv < row.length && String(row[icProv] || '').trim()) {
    return true;
  }
  if (icMp >= 0 && icMp < row.length && String(row[icMp] || '').trim()) {
    return true;
  }
  return false;
}

function loadItemsProv_(values, hi, headers) {
  var out = [];
  for (var i = hi + 2; i < values.length; i++) {
    var row = values[i];
    if (!row || !row.some(function (c) {
      return String(c || '').trim();
    })) {
      continue;
    }
    if (String(row[0] || '').trim().indexOf('[') === 0) {
      continue;
    }
    out.push(rowToDict_(row, headers));
  }
  return out;
}

function loadMpLookup_(values) {
  var hi = findHeaderRow_(values, 'cod_mp_sistema');
  if (hi < 0) {
    return {};
  }
  var headers = rowToHeaders_(values[hi]);
  var ic = headers.indexOf('cod_mp_sistema');
  var inom = headers.indexOf('nombre_mp');
  var iu = headers.indexOf('unidad_base');
  if (ic < 0) {
    return {};
  }
  var out = {};
  for (var i = hi + 1; i < values.length; i++) {
    var row = values[i];
    if (!row || String(row[0] || '').trim().indexOf('[') === 0) {
      continue;
    }
    var cod = ic < row.length ? String(row[ic] || '').trim() : '';
    if (!cod) {
      continue;
    }
    var mpInfo = {
      nombre_mp: inom >= 0 && inom < row.length ? String(row[inom] || '').trim() : '',
      unidad_base: iu >= 0 && iu < row.length ? String(row[iu] || '').trim() : '',
    };
    out[cod] = mpInfo;
    var codSinCeros = cod.replace(/^0+/, '') || '0';
    if (codSinCeros !== cod && !out[codSinCeros]) {
      out[codSinCeros] = mpInfo;
    }
  }
  return out;
}

function armarFilaProv_(headersProv, pend, mp) {
  var codMp = String(pend.cod_mp_asignado || '').trim();
  var ub = String(mp.unidad_base || '').trim();
  var codProv = String(pend.cod_proveedor || '').trim();
  var razon = String(pend.razon_social || '').trim();
  var ruc = String(pend.ruc_proveedor || '').trim();
  var codXml = String(pend.cod_item_xml || '').trim();
  var codCatalogo = normalizarCodItemParaMatch_(codXml, razon, ruc, codProv);
  var valores = {
    cod_item_prov: codCatalogo,
    cod_proveedor: codProv,
    cod_mp_sistema: codMp,
    descripcion_proveedor: String(pend.descripcion_xml || '').trim(),
    activo: 'SI',
    factor_conversion: '1',
    nombre_mp: String(mp.nombre_mp || '').trim(),
    unidad_base_sistema: ub,
    unidad_compra: ub,
  };
  var fila = [];
  for (var h = 0; h < headersProv.length; h++) {
    var name = headersProv[h];
    fila.push(valores.hasOwnProperty(name) ? valores[name] : '');
  }
  return fila;
}

// =============================================================================
// CONTEO FÍSICO (plantilla CONTEO / CONTEO_BARRA)
// =============================================================================

/**
 * @returns {{ok:true, payload:Object, cicloId:string}|{ok:false, message:string}}
 */
function buildConteoPayloadFromActiveSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getActiveSheet();
  var nombre = sh.getName();

  if (String(sh.getRange('A6').getDisplayValue() || '').trim() !== 'line_no') {
    return {
      ok: false,
      message:
        'Esta pestaña no parece la plantilla de conteo (A6 debería ser "line_no"). ' +
        'Abra la hoja CONTEO generada por plantilla_conteo_sheets.py.',
    };
  }

  var cicloId = String(sh.getRange('B2').getDisplayValue() || '').trim();
  if (!cicloId) {
    return { ok: false, message: 'Complete ciclo_id en celda B2' };
  }

  var enviadoPor = sh.getRange('B3').getDisplayValue() || '';
  var enviadoContacto = sh.getRange('B4').getDisplayValue() || '';
  var observaciones = sh.getRange('B5').getDisplayValue() || '';

  var lastRow = sh.getLastRow();
  if (lastRow < 7) {
    return { ok: false, message: 'No hay filas de datos (desde fila 7)' };
  }

  var numRows = lastRow - 7 + 1;
  var range = sh.getRange(7, 1, numRows, 8);
  var values = range.getValues();
  var lines = [];
  var errores = [];
  var COL_COD_MP = 2;

  for (var i = 0; i < values.length; i++) {
    var sheetRow = i + 7;
    var row = values[i];
    var rawCodMp = sh.getRange(sheetRow, COL_COD_MP).getValue();
    var codMp = rawCodMp == null ? '' : String(rawCodMp).trim();
    if (!codMp) {
      continue;
    }

    var lineNo = row[0];
    var codBod = row[2] != null ? String(row[2]).trim() : '';
    var rawCf = row[6];
    var notas = row[7] != null ? String(row[7]).trim() : '';

    if (rawCf === '' || rawCf === null) {
      continue;
    }

    var conteoStr = String(rawCf).replace(/\s/g, '').replace(',', '.');
    var conteoNum = parseFloat(conteoStr);
    if (isNaN(conteoNum)) {
      errores.push('Fila ' + sheetRow + ' (' + codMp + '): conteo_fisico no es número');
      continue;
    }

    var obj = {
      cod_mp_sistema: codMp,
      cod_bodega: codBod,
      conteo_fisico: conteoNum,
    };
    if (lineNo !== '' && lineNo != null) {
      obj.line_no = typeof lineNo === 'number' ? lineNo : parseInt(String(lineNo), 10);
    }
    if (notas) {
      obj.notas = notas;
    }
    lines.push(obj);
  }

  if (errores.length > 0) {
    return { ok: false, message: 'Errores:\n' + errores.slice(0, 12).join('\n') };
  }
  if (lines.length === 0) {
    return { ok: false, message: 'No hay líneas válidas (¿cod_mp_sistema vacío en todas?)' };
  }

  var payload = {
    ciclo_id: cicloId,
    spreadsheet_id: ss.getId(),
    sheet_name: nombre,
    enviado_por: enviadoPor,
    enviado_por_contacto: enviadoContacto,
    observaciones: observaciones,
    lines: lines,
  };

  return { ok: true, payload: payload, cicloId: cicloId };
}

function exportarJsonConteo() {
  var built = buildConteoPayloadFromActiveSheet_();
  if (!built.ok) {
    SpreadsheetApp.getUi().alert(built.message);
    return;
  }

  var json = JSON.stringify(built.payload, null, 2);
  var html = HtmlService.createHtmlOutput(
    '<textarea style="width:95%;height:400px;font-family:monospace;font-size:11px;">' +
      json.replace(/</g, '&lt;').replace(/>/g, '&gt;') +
      '</textarea><p>Copie el JSON y guárdelo como payload.json (respaldo)</p>'
  ).setWidth(720).setHeight(520);

  SpreadsheetApp.getUi().showModalDialog(html, 'Payload conteo — copiar a archivo');
}

function enviarConteoATatami() {
  var props = PropertiesService.getScriptProperties();
  var url = (props.getProperty('TATAMI_CONTEO_API_URL') || '').trim();
  var secret = (props.getProperty('TATAMI_CONTEO_SECRET') || '').trim();

  if (!url || !secret) {
    SpreadsheetApp.getUi().alert(
      'Falta configuración en Propiedades del script:\n' +
        '- TATAMI_CONTEO_API_URL\n' +
        '- TATAMI_CONTEO_SECRET\n\n' +
        'Editor → ⚙️ Ajustes del proyecto → Propiedades del script'
    );
    return;
  }

  var built = buildConteoPayloadFromActiveSheet_();
  if (!built.ok) {
    SpreadsheetApp.getUi().alert(built.message);
    return;
  }

  var payloadStr = JSON.stringify(built.payload);

  try {
    var resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json; charset=utf-8',
      payload: payloadStr,
      headers: { 'X-Tatami-Conteo-Secret': secret },
      muteHttpExceptions: true,
    });

    var code = resp.getResponseCode();
    var body = resp.getContentText() || '';

    if (code >= 200 && code < 300) {
      SpreadsheetApp.getUi().alert('Enviado correctamente.\n\nRespuesta (resumen):\n' + body.substring(0, 900));
      return;
    }

    SpreadsheetApp.getUi().alert(
      'El servidor respondió error HTTP ' +
        code +
        '.\n\n' +
        body.substring(0, 1200) +
        '\n\nSi es 401, revise TATAMI_CONTEO_SECRET vs CONTEO_SHEETS_INGEST_SECRET del servidor.'
    );
  } catch (e) {
    SpreadsheetApp.getUi().alert('Error de red o URL: ' + String(e));
  }
}
