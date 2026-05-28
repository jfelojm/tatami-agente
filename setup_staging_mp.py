"""
setup_staging_mp.py
────────────────────
Configura formato, dropdown de estado y fórmula cod_mp_sistema
en la pestaña STAGING_MP del Sheets de staging.

cod_mp_sistema continúa desde el último código del Master (567 → 568, 569...)
Formato: 3 dígitos con cero a la izquierda (568, 569, 570...)

Uso:
    python setup_staging_mp.py
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
MASTER_ID  = os.environ["SPREADSHEET_ID"]
SHEET_NAME = "STAGING_MP"


def _ultimo_cod_mp_desde_master(sheets) -> int:
    """
    Lee BD_MP_SISTEMA del Master y retorna el máximo cod_mp_sistema numérico.
    Si no encuentra ninguno, retorna 0.
    """
    res = sheets.spreadsheets().values().get(
        spreadsheetId=MASTER_ID,
        range="BD_MP_SISTEMA!A:A",
    ).execute()
    vals = res.get("values") or []
    max_cod = 0
    for row in vals:
        if not row:
            continue
        s = str(row[0] or "").strip()
        if not s or s.lower() == "cod_mp_sistema":
            continue
        if not s.isdigit():
            continue
        try:
            n = int(s)
        except ValueError:
            continue
        if n > max_cod:
            max_cod = n
    return max_cod


def get_creds() -> Credentials:
    return Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)


def col_letra(idx: int) -> str:
    result = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        result = chr(65 + r) + result
    return result


def configurar_staging_mp(creds: Credentials) -> None:
    sheets = build("sheets", "v4", credentials=creds)

    # Obtener sheet_id de STAGING_MP
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=STAGING_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in spreadsheet["sheets"]
        if s["properties"]["title"] == SHEET_NAME
    )

    # Leer encabezados actuales
    result = sheets.spreadsheets().values().get(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!1:1"
    ).execute()
    headers = result.get("values", [[]])[0]
    n_cols = len(headers)
    log.info(f"Columnas Form: {n_cols} — {headers}")

    # Columnas de gestión al final
    col_estado = n_cols
    col_cod_mp = n_cols + 1

    letra_estado = col_letra(col_estado)
    letra_cod_mp = col_letra(col_cod_mp)
    letra_nombre = "C"  # nombre_mp — 3ra columna después de Marca temporal y correo

    log.info(f"estado → {letra_estado} | cod_mp_sistema → {letra_cod_mp}")

    # Encabezados de gestión
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!{letra_estado}1:{letra_cod_mp}1",
        valueInputOption="USER_ENTERED",
        body={"values": [["estado", "cod_mp_sistema"]]}
    ).execute()

    ultimo = _ultimo_cod_mp_desde_master(sheets)
    # Fórmula cod_mp_sistema — continúa desde el último código en Master + 1
    # Genera 568, 569, 570... con formato 000
    siguiente = int(ultimo) + 1
    formula_cod = (
        f'=ARRAYFORMULA(SI({letra_nombre}2:{letra_nombre}="";"";'
        f'TEXTO({siguiente}+FILA({letra_nombre}2:{letra_nombre})-2;"000")))'
    )
    sheets.spreadsheets().values().update(
        spreadsheetId=STAGING_ID,
        range=f"{SHEET_NAME}!{letra_cod_mp}2",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula_cod]]}
    ).execute()
    log.info(f"Fórmula cod_mp_sistema aplicada — inicia en {siguiente}")

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
            "startColumnIndex": col_estado, "endColumnIndex": col_cod_mp + 1
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
    configurar_staging_mp(creds)

    url = f"https://docs.google.com/spreadsheets/d/{STAGING_ID}"
    print("\n" + "=" * 65)
    print("  OK  Staging MP configurado")
    print(f"  URL: {url}")
    print()
    print("  cod_mp_sistema inicia en (último Master + 1) (formato 000)")
    print("  estado → PENDIENTE / APROBADO / RECHAZADO")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
