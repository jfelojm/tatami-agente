"""

setup_bd_subrecetas.py

──────────────────────

Crea las pestañas BD_SUBRECETAS y BD_SUBRECETAS_DETALLE en el maestro Sheets.



BD_SUBRECETAS (cabecera):

  nombre_subreceta, cod_subreceta, rendimiento_estandar, unidad, activa, notas



BD_SUBRECETAS_DETALLE (por lote estándar del padre):

  nombre_subreceta, cod_subreceta_padre, nombre_subreceta_hijo, cod_subreceta_hijo,

  nombre_mp, cod_mp_sistema, cantidad, unidad_base, cod_bodega, merma_pct



Por fila: exactamente uno de cod_subreceta_hijo (semi hijo) o cod_mp_sistema (MP).



Uso:

  python setup_bd_subrecetas.py



No ejecutar en un maestro ya poblado salvo para alinear solo la fila 1 de encabezados.

"""



from __future__ import annotations



import logging

import os



from dotenv import load_dotenv
from googleapiclient.discovery import build
from google_credentials import google_credentials



load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

log = logging.getLogger(__name__)



SCOPES = [

    "https://www.googleapis.com/auth/spreadsheets",

    "https://www.googleapis.com/auth/drive",

]

MASTER_ID = os.environ["SPREADSHEET_ID"]



SHEET_CABECERA = "BD_SUBRECETAS"

SHEET_DETALLE = "BD_SUBRECETAS_DETALLE"



HEADERS_CABECERA = [

    "nombre_subreceta",

    "cod_subreceta",

    "rendimiento_estandar",

    "unidad",

    "activa",

    "notas",

    "costo_lote_estandar",

    "costo_unitario_estandar",

    "costo_calc_at",

]



HEADERS_DETALLE = [

    "nombre_subreceta",

    "cod_subreceta_padre",

    "nombre_subreceta_hijo",

    "cod_subreceta_hijo",

    "nombre_mp",

    "cod_mp_sistema",

    "cantidad",

    "unidad_base",

    "cod_bodega",

    "merma_pct",

]



_HDR_BG = {"red": 0.11, "green": 0.11, "blue": 0.10}

_HDR_FG = {"red": 0.95, "green": 0.88, "blue": 0.75}





def get_creds() -> Credentials:

    return google_credentials(SCOPES)





def crear_hoja_si_no_existe(sheets, spreadsheet_id: str, nombre: str, rows: int = 2000) -> int:

    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

    for s in spreadsheet["sheets"]:

        if s["properties"]["title"] == nombre:

            log.info("Hoja '%s' ya existe (sheetId=%s)", nombre, s["properties"]["sheetId"])

            return s["properties"]["sheetId"]



    sheets.spreadsheets().batchUpdate(

        spreadsheetId=spreadsheet_id,

        body={

            "requests": [

                {

                    "addSheet": {

                        "properties": {

                            "title": nombre,

                            "gridProperties": {"rowCount": rows, "columnCount": 26},

                        }

                    }

                }

            ]

        },

    ).execute()

    log.info("Hoja '%s' creada", nombre)



    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

    return next(

        s["properties"]["sheetId"]

        for s in spreadsheet["sheets"]

        if s["properties"]["title"] == nombre

    )





def _fmt_header(sheet_id: int, ncols: int) -> list:

    return [

        {

            "updateSheetProperties": {

                "properties": {

                    "sheetId": sheet_id,

                    "gridProperties": {"frozenRowCount": 1},

                },

                "fields": "gridProperties.frozenRowCount",

            }

        },

        {

            "repeatCell": {

                "range": {

                    "sheetId": sheet_id,

                    "startRowIndex": 0,

                    "endRowIndex": 1,

                    "startColumnIndex": 0,

                    "endColumnIndex": ncols,

                },

                "cell": {

                    "userEnteredFormat": {

                        "backgroundColor": _HDR_BG,

                        "textFormat": {

                            "foregroundColor": _HDR_FG,

                            "bold": True,

                            "fontSize": 10,

                        },

                        "horizontalAlignment": "CENTER",

                        "verticalAlignment": "MIDDLE",

                    }

                },

                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",

            }

        },

        {

            "updateDimensionProperties": {

                "range": {

                    "sheetId": sheet_id,

                    "dimension": "ROWS",

                    "startIndex": 0,

                    "endIndex": 1,

                },

                "properties": {"pixelSize": 34},

                "fields": "pixelSize",

            }

        },

    ]





def _set_col_widths(sheet_id: int, widths: list[tuple[int, int]]) -> list:

    return [

        {

            "updateDimensionProperties": {

                "range": {

                    "sheetId": sheet_id,

                    "dimension": "COLUMNS",

                    "startIndex": col,

                    "endIndex": col + 1,

                },

                "properties": {"pixelSize": px},

                "fields": "pixelSize",

            }

        }

        for col, px in widths

    ]





def configurar_bd_subrecetas(sheets) -> int:

    sheet_id = crear_hoja_si_no_existe(sheets, MASTER_ID, SHEET_CABECERA)

    end_col = chr(ord("A") + len(HEADERS_CABECERA) - 1)

    sheets.spreadsheets().values().update(

        spreadsheetId=MASTER_ID,

        range=f"{SHEET_CABECERA}!A1:{end_col}1",

        valueInputOption="USER_ENTERED",

        body={"values": [HEADERS_CABECERA]},

    ).execute()



    sheets.spreadsheets().values().update(

        spreadsheetId=MASTER_ID,

        range=f"{SHEET_CABECERA}!L1",

        valueInputOption="USER_ENTERED",

        body={

            "values": [

                [

                    "Rendimiento estándar del lote (ej. 5200 gr kimchi, 6 uni choko). "

                    "Producción: regla de tres. Producción habitual BOD-005."

                ]

            ]

        },

    ).execute()



    requests = _fmt_header(sheet_id, len(HEADERS_CABECERA))

    requests += _set_col_widths(

        sheet_id,

        [(0, 240), (1, 110), (2, 140), (3, 80), (4, 70), (5, 280)],

    )

    requests.append(

        {

            "setDataValidation": {

                "range": {

                    "sheetId": sheet_id,

                    "startRowIndex": 1,

                    "endRowIndex": 2000,

                    "startColumnIndex": 4,

                    "endColumnIndex": 5,

                },

                "rule": {

                    "condition": {

                        "type": "ONE_OF_LIST",

                        "values": [{"userEnteredValue": "SI"}, {"userEnteredValue": "NO"}],

                    },

                    "showCustomUi": True,

                    "strict": False,

                },

            }

        }

    )

    sheets.spreadsheets().batchUpdate(

        spreadsheetId=MASTER_ID, body={"requests": requests}

    ).execute()

    log.info("%s: encabezados y formato listos", SHEET_CABECERA)

    return sheet_id





def configurar_bd_subrecetas_detalle(sheets) -> int:

    sheet_id = crear_hoja_si_no_existe(sheets, MASTER_ID, SHEET_DETALLE)

    end_col = chr(ord("A") + len(HEADERS_DETALLE) - 1)

    sheets.spreadsheets().values().update(

        spreadsheetId=MASTER_ID,

        range=f"{SHEET_DETALLE}!A1:{end_col}1",

        valueInputOption="USER_ENTERED",

        body={"values": [HEADERS_DETALLE]},

    ).execute()



    sheets.spreadsheets().values().update(

        spreadsheetId=MASTER_ID,

        range=f"{SHEET_DETALLE}!K1",

        valueInputOption="USER_ENTERED",

        body={

            "values": [

                [

                    "Cantidades por rendimiento_estandar del padre. "

                    "cod_subreceta_hijo O cod_mp_sistema (no ambos). "

                    "cod_bodega: BOD-001 cocina, BOD-002 barra, BOD-005 externa."

                ]

            ]

        },

    ).execute()



    requests = _fmt_header(sheet_id, len(HEADERS_DETALLE))

    requests += _set_col_widths(

        sheet_id,

        [

            (0, 200),

            (1, 90),

            (2, 180),

            (3, 90),

            (4, 180),

            (5, 90),

            (6, 90),

            (7, 80),

            (8, 100),

            (9, 80),

        ],

    )

    sheets.spreadsheets().batchUpdate(

        spreadsheetId=MASTER_ID, body={"requests": requests}

    ).execute()

    log.info("%s: encabezados y formato listos", SHEET_DETALLE)

    return sheet_id





def main() -> None:

    creds = get_creds()

    sheets = build("sheets", "v4", credentials=creds)

    configurar_bd_subrecetas(sheets)

    configurar_bd_subrecetas_detalle(sheets)

    url = f"https://docs.google.com/spreadsheets/d/{MASTER_ID}"

    print("\nOK — Pestañas creadas o actualizadas en el maestro:")

    print(f"  • {SHEET_CABECERA}: {', '.join(HEADERS_CABECERA)}")

    print(f"  • {SHEET_DETALLE}: {', '.join(HEADERS_DETALLE)}")

    print(f"\n{url}")





if __name__ == "__main__":

    main()


