"""
setup_staging_proveedores.py
────────────────────────────
Configura formato, dropdown de estado y fórmula cod_proveedor
en el Sheets de staging de proveedores.

Uso:
    python setup_staging_proveedores.py
"""

import os
import logging
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]
CREDS_PATH = os.environ["GOOGLE_CREDENTIALS_PATH"]
STAGING_ID = "1TJu70BNG4i3it4y51Eg3YlDNswLkh1QGRt6v-qAyexU"


def get_creds() -> Credentials:
    return Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)


def col_letra(idx: int) -> str:
    """Convierte índice 0-based a letra de columna (A, B, ..., Z, AA, ...)"""
    result = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        result = chr(65 + r) + result
    return result


def configurar_staging(creds: Credentials) -> None:
    sheets = build("sheets", "v4", credentials=creds)

    # Obtener sheet_id
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=STAGING_ID).execute()
    sheet_id = spreadsheet["sheets"][0]["properties"]["sheetId"]

    # Leer encabezados actuales del Form
    result = sheets.spreadsheets().values().get(
        spreadsheetId=STAGING_ID,
        range="STAGING_PROVEEDORES!1:1"
    ).execute()
    headers = result.get("values", [[]])[0]
    n_cols = len(headers)
    log.info(f"Columnas Form: {n_cols} — {headers}")

    # Columnas de gestión al final
    col_estado   = n_cols      # 0-based
    col_cod_prov = n_cols + 1

    letra_estado   = col_letra(col_estado)
    letra_cod_prov = col_letra(col_cod_prov)
    # Razón Social es la 3ra columna (C) — después de Marca temporal y correo
    letra_razon = "C"

    log.info(f"estado → {letra_estado} | cod_proveedor → {letra_cod_prov}")

    # Encabezados de gestión
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"STAGING_PROVEEDORES!{letra_estado}1:{letra_cod_prov}1",
        valueInputOption="USER_ENTERED",
        body={"values": [["estado", "cod_proveedor"]]}
    ).execute()

    # Fórmula cod_proveedor
    formula_cod = (
        f'=ARRAYFORMULA(SI({letra_razon}2:{letra_razon}="";"";'
        f'"PROV-"&TEXTO(FILA({letra_razon}2:{letra_razon})-1;"000")))'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"STAGING_PROVEEDORES!{letra_cod_prov}2",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula_cod]]}
    ).execute()
    log.info("Fórmula cod_proveedor aplicada")

    total_cols = n_cols + 2
    requests_fmt = []

    # Freeze fila 1
    requests_fmt.append({"updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1}
        },
        "fields": "gridProperties.frozenRowCount"
    }})

    # Encabezado oscuro
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": total_cols
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

    # Dropdown estado
    requests_fmt.append({"setDataValidation": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": col_estado, "endColumnIndex": col_estado + 1
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

    # Formato condicional columna estado
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
                    "startColumnIndex": col_estado, "endColumnIndex": col_estado + 1
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

    # Fondo sutil en columnas de gestión
    requests_fmt.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 500,
            "startColumnIndex": col_estado, "endColumnIndex": col_cod_prov + 1
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.88},
        }},
        "fields": "userEnteredFormat(backgroundColor)"
    }})

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=STAGING_ID,
        body={"requests": requests_fmt}
    ).execute()
    log.info("Formato aplicado")


def main() -> None:
    creds = get_creds()
    configurar_staging(creds)

    url = f"https://docs.google.com/spreadsheets/d/{STAGING_ID}"
    print("\n" + "=" * 65)
    print("  OK  Staging Proveedores configurado")
    print(f"  URL: {url}")
    print()
    print("  Columnas de gestión agregadas al final:")
    print("  - estado        → PENDIENTE / APROBADO / RECHAZADO")
    print("  - cod_proveedor → PROV-001, PROV-002... automático")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
