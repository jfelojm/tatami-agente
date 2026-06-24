"""
setup_staging_recetas.py  (versión v1 — solo MP)
─────────────────────────
Obsoleto para carta con subrecetas. Usar:

  python setup_staging_recetas_v2.py
  python promover_staging_recetas.py

Crea y configura la pestaña STAGING_RECETAS en el Sheets de staging.

Layout v1:
  A  nombre_receta
  B  cod_receta         ← Mary digita (referencia: celda S1 muestra el siguiente disponible)
  C  variedad_smart_menu
  D  nombre_mp          ← dropdown IMPORTRANGE desde BD_MP_SISTEMA
  E  cod_mp_sistema     ← VLOOKUP automático desde nombre_mp
  F  cantidad
  G  es_opcional        ← dropdown SI / NO
  H  pct_aplicacion
  I  merma_pct
  J  estado             ← dropdown PENDIENTE / APROBADO / RECHAZADO

Celda L1: referencia visual "Próximo cod_receta: 208"

Uso:
    python setup_staging_recetas.py
"""

import os
import logging
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google_credentials import google_credentials

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]
STAGING_ID = "1TJu70BNG4i3it4y51Eg3YlDNswLkh1QGRt6v-qAyexU"
MASTER_ID  = os.environ["SPREADSHEET_ID"]
SHEET_NAME = "STAGING_RECETAS"
ULTIMO_COD_RECETA = 207  # último cod_receta en BD_RECETAS_DETALLE


def get_creds() -> Credentials:
    return google_credentials(SCOPES)


def crear_hoja_si_no_existe(sheets, spreadsheet_id: str, nombre: str) -> int:
    """Crea la hoja si no existe y retorna su sheetId."""
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in spreadsheet["sheets"]:
        if s["properties"]["title"] == nombre:
            log.info(f"Hoja '{nombre}' ya existe")
            return s["properties"]["sheetId"]

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": nombre}}}]}
    ).execute()
    log.info(f"Hoja '{nombre}' creada")

    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return next(
        s["properties"]["sheetId"]
        for s in spreadsheet["sheets"]
        if s["properties"]["title"] == nombre
    )


def configurar_staging_recetas(creds: Credentials) -> None:
    sheets = build("sheets", "v4", credentials=creds)
    sheet_id = crear_hoja_si_no_existe(sheets, STAGING_ID, SHEET_NAME)

    # ── Encabezados ──────────────────────────────────────────────────────────
    encabezados = [[
        "nombre_receta",       # A
        "cod_receta",          # B — Mary digita
        "variedad_smart_menu", # C
        "nombre_mp",           # D — dropdown IMPORTRANGE
        "cod_mp_sistema",      # E — VLOOKUP automático
        "cantidad",            # F
        "es_opcional",         # G — dropdown SI/NO
        "pct_aplicacion",      # H
        "merma_pct",           # I
        "estado",              # J — dropdown PENDIENTE/APROBADO/RECHAZADO
    ]]
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!A1:J1",
        valueInputOption="USER_ENTERED",
        body={"values": encabezados}
    ).execute()

    # ── Celda de referencia L1: próximo cod_receta disponible ────────────────
    proximo = ULTIMO_COD_RECETA + 1
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!L1",
        valueInputOption="USER_ENTERED",
        body={"values": [[f"Próximo cod_receta disponible: {proximo}"]]}
    ).execute()

    # ── Fórmula VLOOKUP en E2 para cod_mp_sistema automático ─────────────────
    # Cuando Mary elige nombre_mp en D, E se completa solo
    formula_vlookup = (
        f'=ARRAYFORMULA(SI(D2:D="";"";'
        f'IFERROR(VLOOKUP(D2:D;'
        f'IMPORTRANGE("{MASTER_ID}";"BD_MP_SISTEMA!A:B");'
        f'2;0);"⚠ MP no encontrada")))'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!E2",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula_vlookup]]}
    ).execute()
    log.info("Fórmula VLOOKUP cod_mp_sistema aplicada")

    # ── Defaults en H (pct_aplicacion=100) e I (merma_pct=0) ────────────────
    formula_pct = '=ARRAYFORMULA(SI(A2:A="";"";SI(H2:H="";"100";H2:H)))'
    # Nota: los defaults se muestran como placeholder en el encabezado,
    # Mary puede dejar vacío y se asume 100/0 al procesar

    # ── Formato ───────────────────────────────────────────────────────────────
    requests_fmt = []

    # Freeze fila 1
    requests_fmt.append({"updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1}
        },
        "fields": "gridProperties.frozenRowCount"
    }})

    # Encabezado A1:J1 oscuro
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 10
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.11, "green": 0.11, "blue": 0.10},
            "textFormat": {
                "foregroundColor": {"red": 0.95, "green": 0.88, "blue": 0.75},
                "bold": True, "fontSize": 10
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})

    # Celda L1: referencia visual — fondo amarillo suave
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 11, "endColumnIndex": 14
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.70},
            "textFormat": {
                "foregroundColor": {"red": 0.30, "green": 0.20, "blue": 0.0},
                "bold": True, "fontSize": 10
            },
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"
    }})

    # Alto encabezado
    requests_fmt.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 34},
        "fields": "pixelSize"
    }})

    # Ancho columnas
    anchos = [
        (0, 200),  # A nombre_receta
        (1, 100),  # B cod_receta
        (2, 150),  # C variedad_smart_menu
        (3, 220),  # D nombre_mp
        (4, 110),  # E cod_mp_sistema
        (5, 80),   # F cantidad
        (6, 90),   # G es_opcional
        (7, 100),  # H pct_aplicacion
        (8, 90),   # I merma_pct
        (9, 110),  # J estado
        (11, 220), # L referencia
    ]
    for col_idx, ancho in anchos:
        requests_fmt.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": col_idx, "endIndex": col_idx + 1},
            "properties": {"pixelSize": ancho},
            "fields": "pixelSize"
        }})

    # Fuente datos
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 0, "endColumnIndex": 10
        },
        "cell": {"userEnteredFormat": {
            "textFormat": {"fontSize": 9},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(textFormat,verticalAlignment)"
    }})

    # Fondo sutil columna E (cod_mp_sistema — calculado) y J (estado)
    for col in [4, 9]:
        requests_fmt.append({"repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1, "endRowIndex": 500,
                "startColumnIndex": col, "endColumnIndex": col + 1
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.88},
            }},
            "fields": "userEnteredFormat(backgroundColor)"
        }})

    # Dropdown es_opcional (G)
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 6, "endColumnIndex": 7
        },
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": "NO"},
                    {"userEnteredValue": "SI"},
                ]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    # Dropdown estado (J)
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 9, "endColumnIndex": 10
        },
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": "PENDIENTE"},
                    {"userEnteredValue": "APROBADO"},
                    {"userEnteredValue": "RECHAZADO"},
                ]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    # Formato condicional columna J (estado)
    reglas = [
        ("PENDIENTE", {"red": 0.90, "green": 0.65, "blue": 0.10},
                      {"red": 0.10, "green": 0.07, "blue": 0.0}),
        ("APROBADO",  {"red": 0.20, "green": 0.56, "blue": 0.31},
                      {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
        ("RECHAZADO", {"red": 0.78, "green": 0.18, "blue": 0.18},
                      {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
    ]
    for i, (texto, bg, fg) in enumerate(reglas):
        requests_fmt.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": 9, "endColumnIndex": 10
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": texto}]
                    },
                    "format": {
                        "backgroundColor": bg,
                        "textFormat": {"foregroundColor": fg, "bold": True}
                    }
                }
            },
            "index": i
        }})

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=STAGING_ID,
        body={"requests": requests_fmt}
    ).execute()
    log.info("Formato aplicado")

    # ── Dropdown nombre_mp desde IMPORTRANGE ─────────────────────────────────
    # Nota: Google Sheets no soporta IMPORTRANGE directo en validación de datos.
    # La solución es crear una pestaña auxiliar oculta con la lista de MPs
    # y usarla como fuente del dropdown.
    crear_hoja_auxiliar_mp(sheets, sheet_id)


def crear_hoja_auxiliar_mp(sheets, sheet_id_recetas: int) -> None:
    """
    Crea pestaña oculta _AUX_MP con lista de MPs desde BD_MP_SISTEMA.
    Se usa como fuente del dropdown de nombre_mp en STAGING_RECETAS.
    """
    aux_name = "_AUX_MP"
    aux_sheet_id = crear_hoja_si_no_existe(sheets, STAGING_ID, aux_name)

    # Fórmula IMPORTRANGE en A1 — trae nombre_mp de MPs activas
    formula_aux = (
        f'=IFERROR(QUERY(IMPORTRANGE("{MASTER_ID}";"BD_MP_SISTEMA!A:L");'
        f'"SELECT Col1 WHERE Col1<>\'nombre_mp\' ORDER BY Col1 ASC";0);'
        f'"⚠ Autoriza IMPORTRANGE")'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{aux_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula_aux]]}
    ).execute()

    # Ocultar la hoja auxiliar
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=STAGING_ID,
        body={"requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": aux_sheet_id,
                    "hidden": True
                },
                "fields": "hidden"
            }
        }]}
    ).execute()
    log.info("Hoja auxiliar _AUX_MP creada y oculta")

    # Dropdown en columna D (nombre_mp) apuntando a _AUX_MP
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=STAGING_ID,
        body={"requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id_recetas,
                    "startRowIndex": 1, "endRowIndex": 500,
                    "startColumnIndex": 3, "endColumnIndex": 4
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue": f"={aux_name}!$A$1:$A$700"}]
                    },
                    "showCustomUi": True,
                    "strict": False
                }
            }
        }]}
    ).execute()
    log.info("Dropdown nombre_mp configurado desde _AUX_MP")


def main() -> None:
    creds = get_creds()
    configurar_staging_recetas(creds)

    url = f"https://docs.google.com/spreadsheets/d/{STAGING_ID}"
    print("\n" + "=" * 65)
    print("  OK  Staging Recetas configurado")
    print(f"  URL: {url}")
    print()
    print(f"  Próximo cod_receta disponible: {ULTIMO_COD_RECETA + 1}")
    print()
    print("  PASO MANUAL — autorizar IMPORTRANGE en _AUX_MP:")
    print("  1. Muestra la hoja _AUX_MP (clic derecho en pestañas → Mostrar)")
    print("  2. Clic en celda A1 → Permitir acceso")
    print("  3. Vuelve a ocultar la hoja")
    print("  Solo se hace una vez.")
    print()
    print("  FLUJO MARY:")
    print("  - Recibe receta de Jacky")
    print("  - En col B escribe cod_receta (ver referencia en L1)")
    print("  - En col D elige nombre_mp del dropdown (siempre actualizado)")
    print("  - Col E se completa automático con cod_mp_sistema")
    print("  - Llena cantidad, es_opcional, pct_aplicacion, merma_pct")
    print("  - Cambia estado a APROBADO cuando está lista")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
