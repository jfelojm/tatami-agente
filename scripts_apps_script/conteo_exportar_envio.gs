/**
 * Tatami — Exportar payload para POST / registrar-envio (conteo_fisico.py)
 *
 * Cómo usar:
 * 1. Crear la pestaña CONTEO con: python plantilla_conteo_sheets.py --produccion
 * 2. En este proyecto de Apps Script: pegar este archivo.
 * 3. Rellenar columna "conteo_fisico" (todas las filas con MP).
 * 4. Menú personalizado: Conteo > Exportar JSON (o ejecutar exportarJsonConteo desde el editor).
 *
 * El layout debe ser el de plantilla_conteo_sheets.py:
 *   B2 = ciclo_id  |  B3 = enviado_por  |  B4 = enviado_por_contacto  |  B5 = observaciones
 *   Fila 6 = cabeceras | datos desde fila 7
 */

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Conteo')
    .addItem('Exportar JSON (envío)', 'exportarJsonConteo')
    .addToUi();
}

function exportarJsonConteo() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getActiveSheet();
  var nombre = sh.getName();
  if (String(sh.getRange('A6').getDisplayValue() || '').trim() !== 'line_no') {
    SpreadsheetApp.getUi().alert(
      'Esta pestaña no parece la plantilla de conteo (A6 debería ser "line_no"). ' +
        'Abra la hoja creada por plantilla_conteo_sheets.py y vuelva a intentar.'
    );
    return;
  }

  var cicloId = String(sh.getRange('B2').getDisplayValue() || '').trim();
  if (!cicloId) {
    SpreadsheetApp.getUi().alert('Complete ciclo_id en celda B2');
    return;
  }

  var enviadoPor = sh.getRange('B3').getDisplayValue() || '';
  var enviadoContacto = sh.getRange('B4').getDisplayValue() || '';
  var observaciones = sh.getRange('B5').getDisplayValue() || '';

  var lastRow = sh.getLastRow();
  if (lastRow < 7) {
    SpreadsheetApp.getUi().alert('No hay filas de datos (desde fila 7)');
    return;
  }

  var range = sh.getRange(7, 1, lastRow, 8);
  var values = range.getValues();
  var lines = [];
  var errores = [];

  for (var i = 0; i < values.length; i++) {
    var row = values[i];
    var codMp = row[1] != null ? String(row[1]).trim() : '';
    if (!codMp) {
      continue;
    }

    var lineNo = row[0];
    var codBod = row[2] != null ? String(row[2]).trim() : '';
    var rawCf = row[6];
    var notas = row[7] != null ? String(row[7]).trim() : '';

    if (rawCf === '' || rawCf === null) {
      errores.push('Fila ' + (i + 7) + ' (' + codMp + '): conteo_fisico vacío');
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
    SpreadsheetApp.getUi().alert('Errores:\n' + errores.slice(0, 8).join('\n'));
    return;
  }
  if (lines.length === 0) {
    SpreadsheetApp.getUi().alert('No hay líneas válidas (¿cod_mp_sistema vacío en todas?)');
    return;
  }

  var payload = {
    spreadsheet_id: ss.getId(),
    sheet_name: nombre,
    enviado_por: enviadoPor,
    enviado_por_contacto: enviadoContacto,
    observaciones: observaciones,
    lines: lines,
  };

  var json = JSON.stringify(payload, null, 2);

  var html = HtmlService.createHtmlOutput(
    '<textarea style="width:95%;height:400px;font-family:monospace;font-size:11px;">' +
      json.replace(/</g, '&lt;').replace(/>/g, '&gt;') +
      '</textarea><p>Copie el JSON y guárdelo como payload.json</p>'
  ).setWidth(720).setHeight(520);

  SpreadsheetApp.getUi().showModalDialog(html, 'Payload conteo — copiar a archivo');
}
