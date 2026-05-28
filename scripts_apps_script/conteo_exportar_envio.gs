/**
 * Tatami — Conteo físico desde Google Sheets
 *
 * DEPRECADO como instalación separada: usar tatami_maestro_unificado.gs (un solo archivo).
 *
 * Configuración (Proyecto Apps Script → ⚙️ Ajustes del proyecto → Propiedades del script):
 *   TATAMI_CONTEO_API_URL  = URL completa del endpoint, ej:
 *       https://tu-servidor.com/api/conteo/enviar
 *   TATAMI_CONTEO_SECRET   = mismo valor que CONTEO_SHEETS_INGEST_SECRET en el servidor (.env)
 *
 * Uso operativo:
 * 1) Plantilla CONTEO (python plantilla_conteo_sheets.py --produccion …).
 * 2) Pegar este .gs en Extensiones → Apps Script, guardar, recargar el libro.
 * 3) Rellenar columna conteo_fisico (G) en todas las filas con MP.
 * 4) Menú Conteo → Enviar a Tatami (recomendado) o Exportar JSON (respaldo / soporte).
 *
 * Layout (plantilla_conteo_sheets.py):
 *   B2 = ciclo_id  |  B3 = enviado_por  |  B4 = enviado_por_contacto  |  B5 = observaciones
 *   Fila 6 = cabeceras | datos desde fila 7
 *
 * Requisito columna B (cod_mp_sistema): en Sheets debe ser Texto plano (Formato → Número → Texto sin formato)
 * para conservar ceros a la izquierda (p. ej. "001"). Si la columna es número, la celda pasa a ser 1 y el JSON
 * enviará "1", que no coincide con Supabase — no compensar con padStart en Apps Script; corregir el formato de la hoja.
 *
 * Inventario cíclico: cada ciclo tiene su UUID en B2 (año/semana/bodega se definen al crear el ciclo
 * en backend). Varios conteos en el tiempo = varios ciclos o nuevas secuencias de envío según Tatami.
 */

/** Llamada desde onOpen() en promover_pendientes_items_prov.gs (menú unificado). */
function conteoAgregarMenu_() {
  SpreadsheetApp.getUi()
    .createMenu('Conteo')
    .addItem('Enviar a Tatami', 'enviarConteoATatami')
    .addItem('Exportar JSON (respaldo)', 'exportarJsonConteo')
    .addToUi();
}

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
  /** Columna B = cod_mp_sistema (1-based). Leer con getValue() celda a celda para string fiel a la celda. */
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
      errores.push('Fila ' + (i + 7) + ' (' + codMp + '): conteo_fisico no es número');
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
