"""
setup_staging_productos.py
──────────────────────────
Crea y configura la pestaña STAGING_PRODUCTOS en el Sheets de staging.
Mary llena este staging directamente — no hay Form.

Layout:
  A  nombre_producto
  B  cod_smart_menu       ← Mary copia desde Smart Menu
  C  variedad_smart_menu
  D  cod_receta           ← Mary copia desde STAGING_RECETAS o BD_RECETAS_DETALLE
  E  categoria_menu       ← dropdown categorías
  F  precio_venta
  G  activo               ← dropdown SI / NO
  H  version
  I  fecha_vigencia
  J  rendimiento
  K  descarga_inventario  ← dropdown SI / NO
  L  estado               ← dropdown PENDIENTE / APROBADO / RECHAZADO

Uso:
    python setup_staging_productos.py
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
SHEET_NAME = "STAGING_PRODUCTOS"

CATEGORIAS_MENU = [
    "APERITIVO/BAJATIVO", "BAOS", "BUCHANANS", "CERVEZAS",
    "CIGARROS Y VARIOS", "COCTAILS", "COFFEE", "COPEO VINOS",
    "ESPECIALES", "EXTRAS", "GIN", "MENU NIÑOS", "MOCKTAILS",
    "PLATOS FUERTES", "POSTRES", "RON", "SANDUCHES", "SOFT DRINKS",
    "TEQUILA", "TO SHARE", "VINOS", "VODKA", "WHISKY"
]


def get_creds() -> Credentials:
    return google_credentials(SCOPES)


def crear_hoja_si_no_existe(sheets, spreadsheet_id: str, nombre: str) -> int:
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in spreadsheet["sheets"]:
        if s["properties"]["title"] == nombre:
            log.info(f"Hoja '{nombre}' ya existe — se reconfigura")
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


def configurar_staging_productos(creds: Credentials) -> None:
    sheets = build("sheets", "v4", credentials=creds)
    sheet_id = crear_hoja_si_no_existe(sheets, STAGING_ID, SHEET_NAME)

    # ── Encabezados ──────────────────────────────────────────────────────────
    encabezados = [[
        "nombre_producto",      # A
        "cod_smart_menu",       # B
        "variedad_smart_menu",  # C
        "cod_receta",           # D
        "categoria_menu",       # E
        "precio_venta",         # F
        "activo",               # G
        "version",              # H
        "fecha_vigencia",       # I
        "rendimiento",          # J
        "descarga_inventario",  # K
        "estado",               # L
    ]]
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!A1:L1",
        valueInputOption="USER_ENTERED",
        body={"values": encabezados}
    ).execute()

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

    # Encabezado oscuro A1:L1
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 12
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

    # Alto encabezado
    requests_fmt.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 34},
        "fields": "pixelSize"
    }})

    # Fuente datos
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 0, "endColumnIndex": 12
        },
        "cell": {"userEnteredFormat": {
            "textFormat": {"fontSize": 9},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(textFormat,verticalAlignment)"
    }})

    # Fondo sutil columna L (estado)
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 11, "endColumnIndex": 12
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.88},
        }},
        "fields": "userEnteredFormat(backgroundColor)"
    }})

    # Ancho columnas
    anchos = [
        (0,  220),  # A nombre_producto
        (1,  110),  # B cod_smart_menu
        (2,  150),  # C variedad_smart_menu
        (3,  100),  # D cod_receta
        (4,  160),  # E categoria_menu
        (5,  90),   # F precio_venta
        (6,  70),   # G activo
        (7,  70),   # H version
        (8,  110),  # I fecha_vigencia
        (9,  90),   # J rendimiento
        (10, 140),  # K descarga_inventario
        (11, 110),  # L estado
    ]
    for col_idx, ancho in anchos:
        requests_fmt.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": col_idx, "endIndex": col_idx + 1},
            "properties": {"pixelSize": ancho},
            "fields": "pixelSize"
        }})

    # Dropdown categoria_menu (E)
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 4, "endColumnIndex": 5
        },
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": c} for c in CATEGORIAS_MENU]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    # Dropdown activo (G)
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
                    {"userEnteredValue": "SI"},
                    {"userEnteredValue": "NO"},
                ]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    # Dropdown descarga_inventario (K)
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 10, "endColumnIndex": 11
        },
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": "SI"},
                    {"userEnteredValue": "NO"},
                ]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    # Dropdown estado (L)
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": 11, "endColumnIndex": 12
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

    # Formato condicional columna L (estado)
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
                    "startColumnIndex": 11, "endColumnIndex": 12
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


def main() -> None:
    creds = get_creds()
    configurar_staging_productos(creds)

    url = f"https://docs.google.com/spreadsheets/d/{STAGING_ID}"
    print("\n" + "=" * 65)
    print("  OK  Staging Productos configurado")
    print(f"  URL: {url}")
    print()
    print("  FLUJO MARY:")
    print("  1. Producto ya existe en Smart Menu → tomar cod_smart_menu")
    print("  2. Receta ya aprobada → tomar cod_receta de STAGING_RECETAS")
    print("  3. Llenar fila en STAGING_PRODUCTOS")
    print("  4. Cambiar estado a APROBADO")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
