"""
Crea/actualiza en el libro staging las hojas del ingreso manual de facturas:

  INGRESO_FACTURA   — una pantalla: proveedor, n° factura, fecha + líneas
                      (descripción dropdown por proveedor, cantidad, costo)
  REGISTRO_FACTURAS — historial permanente (una fila por línea, con TRX)
  CAT_FM            — catálogo oculto (ítems de proveedores manuales)

Re-ejecutable. El botón ACEPTAR vive en Apps Script
(scripts_apps_script/ingreso_factura_manual.gs).

Uso:
  python setup_ingreso_factura_manual.py
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from staging_common import (
    batch_format,
    crear_hoja_si_no_existe,
    dropdown_list,
    dropdown_range,
    header_style,
    hide_sheet,
    open_master,
    find_header_row,
    sheets_api,
    staging_spreadsheet_id,
)

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_INGRESO = "INGRESO_FACTURA"
SHEET_REGISTRO = "REGISTRO_FACTURAS"
SHEET_CAT = "CAT_FM"

PROVEEDORES_MANUALES = ["161", "164"]  # Sumba Chocho, Loja Lasso

FILA_LINEAS_INICIO = 7  # fila donde empiezan las líneas
MAX_LINEAS = 40

REGISTRO_HEADERS = [
    "trx",
    "fecha_hora",
    "usuario",
    "cod_proveedor",
    "proveedor",
    "num_factura",
    "fecha_factura",
    "descripcion",
    "cantidad",
    "costo_unitario",
    "total_linea",
    "cod_mp_sistema",
    "estado",
]


def _razones() -> dict[str, str]:
    ws = open_master().worksheet("BD_PROV")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_proveedor")
    h = [(c or "").strip() for c in vals[hi]]
    icod, iraz = h.index("cod_proveedor"), h.index("razon_social")
    out = {}
    for row in vals[hi + 1 :]:
        cod = (row[icod] if icod < len(row) else "").strip()
        if cod:
            out[cod] = (row[iraz] if iraz < len(row) else "").strip()
    return out


def filas_catalogo() -> list[list[str]]:
    """[cod_proveedor, descripcion, cod_mp] ordenado por proveedor."""
    from procesar_facturas_drive import cargar_bd_items_prov

    filas: list[list[str]] = []
    vistos: set[tuple[str, str]] = set()
    for it in cargar_bd_items_prov():
        cod_prov = (it.get("cod_proveedor") or "").strip()
        if cod_prov not in PROVEEDORES_MANUALES:
            continue
        if (it.get("activo") or "SI").strip().upper() == "NO":
            continue
        desc = (it.get("descripcion_proveedor") or it.get("descripcion") or "").strip()
        cod_mp = (it.get("cod_mp_sistema") or "").strip()
        if not desc or not cod_mp:
            continue
        clave = (cod_prov, desc.upper())
        if clave in vistos:
            continue
        vistos.add(clave)
        filas.append([cod_prov, desc, cod_mp])
    filas.sort(key=lambda r: (r[0], r[1].lower()))
    return filas


def configurar_cat(sheets, sid: str) -> None:
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_CAT)
    filas = filas_catalogo()
    sheets.spreadsheets().values().clear(spreadsheetId=sid, range=f"{SHEET_CAT}!A:C").execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_CAT}!A1",
        valueInputOption="RAW",
        body={"values": [["cod_proveedor", "descripcion", "cod_mp_sistema"]] + filas},
    ).execute()
    hide_sheet(sheets, sid, sheet_id)
    por_prov: dict[str, int] = {}
    for f in filas:
        por_prov[f[0]] = por_prov.get(f[0], 0) + 1
    log.info("CAT_FM: %d ítems %s", len(filas), por_prov)


def configurar_ingreso(sheets, sid: str) -> None:
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_INGRESO)
    razones = _razones()
    opciones_prov = [f"{c} — {razones.get(c, '')}" for c in PROVEEDORES_MANUALES]

    fin = FILA_LINEAS_INICIO + MAX_LINEAS - 1
    valores = [
        ["INGRESO MANUAL DE FACTURA", "", "", ""],
        ["Proveedor:", "", "", ""],
        ["N° factura:", "", "", ""],
        ["Fecha factura:", "", "", ""],
        ["", "", "", ""],
        ["DESCRIPCIÓN", "CANTIDAD", "COSTO UNITARIO", "TOTAL"],
    ]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!A1",
        valueInputOption="RAW",
        body={"values": valores},
    ).execute()

    # Helpers ocultos: F1 cod_proveedor de B2; H1 lista de ítems del proveedor
    # (la validación ONE_OF_RANGE no acepta fórmulas → el dropdown apunta a H)
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!F1",
        valueInputOption="USER_ENTERED",
        body={"values": [['=SI($B$2="";"";REGEXEXTRACT($B$2;"^\\d+"))']]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!H1",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [
                    f'=SI.ERROR(DESREF({SHEET_CAT}!$B$1;'
                    f"COINCIDIR($F$1;{SHEET_CAT}!$A:$A;0)-1;0;"
                    f'CONTAR.SI({SHEET_CAT}!$A:$A;$F$1);1);"")'
                ]
            ]
        },
    ).execute()
    # Total por línea (columna D)
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_INGRESO}!D{FILA_LINEAS_INICIO}",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [
                    f'=ARRAYFORMULA(SI($B{FILA_LINEAS_INICIO}:$B{fin}="";"";'
                    f"$B{FILA_LINEAS_INICIO}:$B{fin}*$C{FILA_LINEAS_INICIO}:$C{fin}))"
                ]
            ]
        },
    ).execute()

    reqs: list[dict] = []
    # Título y cabecera de líneas en negrita
    for r0, r1, c0, c1 in [(0, 1, 0, 4), (5, 6, 0, 4)]:
        reqs.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": r0,
                        "endRowIndex": r1,
                        "startColumnIndex": c0,
                        "endColumnIndex": c1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.13, "green": 0.13, "blue": 0.13},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            }
        )
    # Dropdown proveedor en B2
    dd_prov = dropdown_list(sheet_id, 1, opciones_prov, strict=True)
    dd_prov["setDataValidation"]["range"] = {
        "sheetId": sheet_id,
        "startRowIndex": 1,
        "endRowIndex": 2,
        "startColumnIndex": 1,
        "endColumnIndex": 2,
    }
    reqs.append(dd_prov)
    # Dropdown descripción (A7:A...) → rango fijo H (ítems del proveedor)
    dd_desc = dropdown_range(sheet_id, 0, f"{SHEET_INGRESO}!$H$1:$H$300")
    dd_desc["setDataValidation"]["range"] = {
        "sheetId": sheet_id,
        "startRowIndex": FILA_LINEAS_INICIO - 1,
        "endRowIndex": fin,
        "startColumnIndex": 0,
        "endColumnIndex": 1,
    }
    reqs.append(dd_desc)
    # Ocultar columnas F:H (helpers)
    reqs.append(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 5,
                    "endIndex": 8,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        }
    )
    # Columna A ancha
    reqs.append(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 320},
                "fields": "pixelSize",
            }
        }
    )
    batch_format(sheets, sid, reqs)
    log.info("%s configurada (líneas %d-%d)", SHEET_INGRESO, FILA_LINEAS_INICIO, fin)


def configurar_registro(sheets, sid: str) -> None:
    sheet_id = crear_hoja_si_no_existe(sheets, sid, SHEET_REGISTRO)
    # No borrar datos: solo asegurar headers
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{SHEET_REGISTRO}!A1",
        valueInputOption="RAW",
        body={"values": [REGISTRO_HEADERS]},
    ).execute()
    batch_format(sheets, sid, header_style(sheet_id, len(REGISTRO_HEADERS)))
    log.info("%s configurada (historial, no se limpia)", SHEET_REGISTRO)


def main() -> None:
    sheets = sheets_api()
    sid = staging_spreadsheet_id()
    configurar_cat(sheets, sid)
    configurar_ingreso(sheets, sid)
    configurar_registro(sheets, sid)
    print("\n" + "=" * 70)
    print("  OK  Ingreso manual de facturas")
    print(f"  Staging: https://docs.google.com/spreadsheets/d/{sid}")
    print(f"  Pestañas: {SHEET_INGRESO} (captura) | {SHEET_REGISTRO} (historial)")
    print()
    print("  Falta (una vez): pegar scripts_apps_script/ingreso_factura_manual.gs")
    print("  en Extensiones → Apps Script del libro staging y configurar")
    print("  TATAMI_FACTURA_API_URL y TATAMI_FACTURA_SECRET en Propiedades del script.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
