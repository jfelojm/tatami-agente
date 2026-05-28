"""
setup_vista_inventario.py
─────────────────────────
Script de ejecución ÚNICA.
Configura IMPORTRANGE desde BD_MP_SISTEMA en el Sheets "Tatami — Vista Inventario",
agrega columna Estado con fórmula, aplica formato condicional y permisos de solo lectura.

Uso:
    python setup_vista_inventario.py
"""

import os
import logging
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MASTER_ID    = os.environ["SPREADSHEET_ID"]
CREDS_PATH   = os.environ["GOOGLE_CREDENTIALS_PATH"]
VISTA_ID     = "1jLAOPuowy1DkDJq3ZWkTWsxdwCprhdo_YrrCJN1yRs0"
SHEET_ORIGEN = "BD_MP_SISTEMA"

# Columnas de BD_MP_SISTEMA (orden real en la hoja):
# A  cod_mp_sistema
# B  nombre_mp
# C  categoria
# D  unidad_base
# E  cod_bodega
# F  nombre_bodega
# G  tipo_control
# H  dias_seguridad
# I  costo_unitario_ref
# J  stock_actual
# K  par_level
# L  activa
#
# Vista layout:
# A  Materia Prima      <- col B
# B  Categoría          <- col C
# C  Bodega             <- col F
# D  Unidad             <- col D
# E  Stock Actual       <- col J
# F  Nivel Mínimo       <- col K
# G  Costo Unit ($/u)   <- col I
# H  Estado             <- fórmula local


def get_creds() -> Credentials:
    return Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)


def get_gc(creds: Credentials) -> gspread.Client:
    return gspread.Client(auth=creds)


def configurar_hoja(ws: gspread.Worksheet) -> None:
    """Escribe encabezados, fórmulas IMPORTRANGE y columna Estado."""

    # Fila 1: encabezados — nuevo orden de argumentos gspread 6+
    ws.update(
        values=[["Materia Prima", "Categoría", "Bodega", "Unidad",
                 "Stock Actual", "Nivel Mínimo", "Costo Unit. ($/u)", "Estado"]],
        range_name="A1:H1",
        value_input_option="USER_ENTERED"
    )

    # A2: IMPORTRANGE + QUERY
    formula_importrange = (
        f'=IFERROR(QUERY('
        f'IMPORTRANGE("{MASTER_ID}", "{SHEET_ORIGEN}!A:L"), '
        f'"SELECT Col2, Col3, Col6, Col4, Col10, Col11, Col9 '
        f'WHERE Col12 = \'SI\' AND Col1 <> \'cod_mp_sistema\' '
        f'ORDER BY Col6 ASC, Col2 ASC", 0), '
        f'"⚠ Autoriza IMPORTRANGE: clic en esta celda")'
    )
    ws.update(
        values=[[formula_importrange]],
        range_name="A2",
        value_input_option="USER_ENTERED"
    )

    # H2: Estado con ARRAYFORMULA
    formula_estado = (
        '=ARRAYFORMULA('
        'IF(A2:A="","", '
        'IF(E2:E<0,"⚠ NEGATIVO",'
        'IF(F2:F=0,"—",'
        'IF(E2:E/F2:F>=1,"✓ OK",'
        'IF(E2:E/F2:F>=0.5,"↓ BAJO","✗ CRÍTICO"))))))'
    )
    ws.update(
        values=[[formula_estado]],
        range_name="H2",
        value_input_option="USER_ENTERED"
    )

    log.info("Fórmulas IMPORTRANGE y Estado configuradas")


def aplicar_formato(sh: gspread.Spreadsheet, ws: gspread.Worksheet) -> None:
    sheet_id = ws.id
    requests = []

    # 1. Freeze fila 1
    requests.append({"updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1}
        },
        "fields": "gridProperties.frozenRowCount"
    }})

    # 2. Encabezado fila 1
    requests.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 8
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.11, "green": 0.11, "blue": 0.10},
            "textFormat": {
                "foregroundColor": {"red": 0.95, "green": 0.88, "blue": 0.75},
                "bold": True,
                "fontSize": 10,
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})

    # 3. Alto fila encabezado
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 34},
        "fields": "pixelSize"
    }})

    # 4. Ancho de columnas
    anchos = [
        (0, 280), (1, 130), (2, 110), (3, 75),
        (4, 95),  (5, 105), (6, 115), (7, 105),
    ]
    for col_idx, ancho in anchos:
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": col_idx, "endIndex": col_idx + 1},
            "properties": {"pixelSize": ancho},
            "fields": "pixelSize"
        }})

    # 5. Fuente y alineación en datos A2:H700
    requests.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 700,
            "startColumnIndex": 0, "endColumnIndex": 8
        },
        "cell": {"userEnteredFormat": {
            "textFormat": {"fontSize": 9},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(textFormat,verticalAlignment)"
    }})

    # 6. Alineación centrada columnas numéricas E, F, G
    requests.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 700,
            "startColumnIndex": 4, "endColumnIndex": 7
        },
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat(horizontalAlignment)"
    }})

    # 7. Formato condicional columna H (Estado)
    def rango_estado():
        return {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 700,
            "startColumnIndex": 7, "endColumnIndex": 8
        }

    reglas = [
        ("OK",       {"red": 0.20, "green": 0.56, "blue": 0.31}, {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
        ("BAJO",     {"red": 0.90, "green": 0.65, "blue": 0.10}, {"red": 0.10, "green": 0.07, "blue": 0.0}),
        ("CRÍTICO",  {"red": 0.78, "green": 0.18, "blue": 0.18}, {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
        ("NEGATIVO", {"red": 0.50, "green": 0.05, "blue": 0.05}, {"red": 1.0,  "green": 0.85, "blue": 0.85}),
    ]
    for i, (texto, bg, fg) in enumerate(reglas):
        requests.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [rango_estado()],
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

    # 8. Alineación centrada columna H
    requests.append({"repeatCell": {
        "range": rango_estado(),
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat(horizontalAlignment)"
    }})

    sh.batch_update({"requests": requests})
    log.info("Formato aplicado")


def configurar_permisos(creds: Credentials, sheet_id: str) -> None:
    """Cualquiera con el link puede VER, no editar — usando Drive API v3."""
    drive = build("drive", "v3", credentials=creds)
    drive.permissions().create(
        fileId=sheet_id,
        body={"role": "reader", "type": "anyone"},
        fields="id"
    ).execute()
    log.info("Permisos: solo lectura para cualquiera con el link")


def guardar_en_env(sheet_id: str) -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        log.warning(f".env no encontrado — agrega manualmente: INVENTARIO_PUBLICO_ID={sheet_id}")
        return
    with open(env_path, "r", encoding="utf-8") as f:
        contenido = f.read()
    if "INVENTARIO_PUBLICO_ID" in contenido:
        log.info("INVENTARIO_PUBLICO_ID ya existe en .env")
        return
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f"\nINVENTARIO_PUBLICO_ID={sheet_id}\n")
    log.info("INVENTARIO_PUBLICO_ID guardado en .env")


def main() -> None:
    creds = get_creds()
    gc    = get_gc(creds)

    sh = gc.open_by_key(VISTA_ID)
    ws = sh.get_worksheet(0)
    ws.update_title("INVENTARIO")

    configurar_hoja(ws)
    aplicar_formato(sh, ws)
    configurar_permisos(creds, VISTA_ID)
    guardar_en_env(VISTA_ID)

    url = f"https://docs.google.com/spreadsheets/d/{VISTA_ID}"
    print("\n" + "=" * 65)
    print("  OK  Vista Inventario configurada")
    print(f"  URL: {url}")
    print()
    print("  PASO OBLIGATORIO:")
    print("  Abre el Sheets, clic en celda A2 y autoriza el IMPORTRANGE")
    print("  cuando Google lo solicite. Solo se hace una vez.")
    print()
    print("  COMPARTIR CON PERSONAL (solo lectura):")
    print(f"  {url}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
