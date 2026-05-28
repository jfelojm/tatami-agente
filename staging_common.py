"""
Utilidades compartidas: spreadsheet de staging vs maestro operativo.
"""

from __future__ import annotations

import os
from typing import Any

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Staging (formularios Mary); override con STAGING_SPREADSHEET_ID en .env
DEFAULT_STAGING_ID = "1TJu70BNG4i3it4y51Eg3YlDNswLkh1QGRt6v-qAyexU"


def staging_spreadsheet_id() -> str:
    return (os.getenv("STAGING_SPREADSHEET_ID") or DEFAULT_STAGING_ID).strip()


def master_spreadsheet_id() -> str:
    return os.environ["SPREADSHEET_ID"].strip()


def creds() -> Credentials:
    return Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
    )


def open_staging() -> gspread.Spreadsheet:
    return gspread.authorize(creds()).open_by_key(staging_spreadsheet_id())


def open_master() -> gspread.Spreadsheet:
    return gspread.authorize(creds()).open_by_key(master_spreadsheet_id())


def sheets_api():
    return build("sheets", "v4", credentials=creds())


def crear_hoja_si_no_existe(sheets, spreadsheet_id: str, nombre: str) -> int:
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in spreadsheet["sheets"]:
        if s["properties"]["title"] == nombre:
            return s["properties"]["sheetId"]
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": nombre}}}]},
    ).execute()
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return next(
        s["properties"]["sheetId"]
        for s in spreadsheet["sheets"]
        if s["properties"]["title"] == nombre
    )


def find_header_row(values: list[list[str]], marker: str) -> int | None:
    for i, row in enumerate(values):
        if any((c or "").strip() == marker for c in row):
            return i
    return None


def leer_filas_staging(
    ws: gspread.Worksheet,
    *,
    marker: str,
    min_cols: tuple[str, ...] = (),
) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    values = ws.get_all_values()
    hi = find_header_row(values, marker)
    if hi is None:
        return [], []
    headers = [(c or "").strip() for c in values[hi]]
    if min_cols and not all(any(h == c for h in headers) for c in min_cols):
        return [], []
    filas: list[tuple[int, dict[str, str]]] = []
    for i in range(hi + 1, len(values)):
        row = values[i]
        if not any((c or "").strip() for c in row):
            continue
        if row and str(row[0]).strip().startswith("["):
            continue
        d = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(len(headers))
            if headers[j]
        }
        filas.append((i + 1, d))
    return headers, filas


def escribir_aux_hoja(
    sheets,
    nombre_hoja: str,
    encabezado: list[str],
    filas: list[list],
    *,
    ocultar: bool = True,
) -> str:
    """Escribe datos estáticos en hoja aux del staging (sin IMPORTRANGE)."""
    staging_id = staging_spreadsheet_id()
    sid = crear_hoja_si_no_existe(sheets, staging_id, nombre_hoja)
    values = [encabezado] + filas
    sheets.spreadsheets().values().clear(
        spreadsheetId=staging_id,
        range=f"{nombre_hoja}!A:Z",
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=staging_id,
        range=f"{nombre_hoja}!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
    if ocultar:
        hide_sheet(sheets, staging_id, sid)
    return nombre_hoja


def cargar_filas_mp_maestro() -> list[list[str]]:
    """[nombre_mp, cod_mp_sistema] únicos, ordenados."""
    sh = open_master()
    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_mp_sistema")
    if hi is None:
        return []
    headers = [(c or "").strip() for c in vals[hi]]
    inom = headers.index("nombre_mp")
    icod = headers.index("cod_mp_sistema")
    vistos: set[str] = set()
    out: list[list[str]] = []
    for row in vals[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        if row and str(row[0]).strip().startswith("["):
            continue
        nom = (row[inom] if inom < len(row) else "").strip()
        cod = (row[icod] if icod < len(row) else "").strip()
        if not nom or nom in vistos:
            continue
        vistos.add(nom)
        out.append([nom, cod])
    out.sort(key=lambda r: r[0].lower())
    return out


def cargar_filas_subrecetas_maestro(*, solo_activas: bool = True) -> list[list[str]]:
    """[nombre_subreceta, cod_subreceta] desde BD_SUBRECETAS."""
    from subrecetas_detalle import cargar_bd_subrecetas

    cab = cargar_bd_subrecetas()
    out: list[list[str]] = []
    for cod in sorted(cab.keys(), key=lambda c: (int(c) if c.isdigit() else 9999, c)):
        info = cab[cod]
        if solo_activas and (info.get("activa") or "SI").strip().upper() not in (
            "SI",
            "S",
            "YES",
            "1",
        ):
            continue
        nom = (info.get("nombre_subreceta") or "").strip()
        if nom:
            out.append([nom, cod.strip()])
    return out


def proximo_cod_subreceta_desde_maestro() -> str:
    """Siguiente cod_subreceta numérico (3 dígitos) según BD_SUBRECETAS."""
    try:
        from subrecetas_detalle import cargar_bd_subrecetas

        cab = cargar_bd_subrecetas()
        mx = 0
        for k in cab:
            s = (k or "").strip()
            if s.isdigit():
                mx = max(mx, int(s))
        return str(mx + 1).zfill(3)
    except Exception:
        return "051"


def norm_cod(cod: str) -> str:
    s = (cod or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return str(int(s))
    return s


def pct_a_decimal(raw: str, default: float = 1.0) -> float:
    from numeros_sheets import parse_numero_sheets

    v = parse_numero_sheets(raw, default)
    if v <= 0:
        return default
    if v > 1.0:
        return v / 100.0
    return v


def batch_format(sheets, spreadsheet_id: str, requests: list[dict]) -> None:
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()


def hide_sheet(sheets, spreadsheet_id: str, sheet_id: int) -> None:
    batch_format(
        sheets,
        spreadsheet_id,
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "hidden": True},
                    "fields": "hidden",
                }
            }
        ],
    )


def dropdown_list(
    sheet_id: int,
    col: int,
    values: list[str],
    *,
    start_row: int = 1,
    end_row: int = 500,
    strict: bool = True,
) -> dict:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "showCustomUi": True,
                "strict": strict,
            },
        }
    }


def dropdown_range(
    sheet_id: int,
    col: int,
    range_a1: str,
    *,
    start_row: int = 1,
    end_row: int = 500,
) -> dict:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_RANGE",
                    "values": [{"userEnteredValue": f"={range_a1}"}],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }


def header_style(sheet_id: int, n_cols: int, *, row_height: int = 34) -> list[dict]:
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
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
                    "endColumnIndex": n_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.11, "green": 0.11, "blue": 0.10},
                        "textFormat": {
                            "foregroundColor": {"red": 0.95, "green": 0.88, "blue": 0.75},
                            "bold": True,
                            "fontSize": 10,
                        },
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
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
                "properties": {"pixelSize": row_height},
                "fields": "pixelSize",
            }
        },
    ]


def estado_conditional(sheet_id: int, col_estado: int) -> list[dict]:
    reglas = [
        ("PENDIENTE", {"red": 0.90, "green": 0.65, "blue": 0.10}, {"red": 0.10, "green": 0.07, "blue": 0.0}),
        ("APROBADO", {"red": 0.20, "green": 0.56, "blue": 0.31}, {"red": 1.0, "green": 1.0, "blue": 1.0}),
        ("PROMOVIDO", {"red": 0.45, "green": 0.65, "blue": 0.85}, {"red": 1.0, "green": 1.0, "blue": 1.0}),
        ("RECHAZADO", {"red": 0.78, "green": 0.18, "blue": 0.18}, {"red": 1.0, "green": 1.0, "blue": 1.0}),
    ]
    out: list[dict] = []
    for i, (texto, bg, fg) in enumerate(reglas):
        out.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": 500,
                                "startColumnIndex": col_estado,
                                "endColumnIndex": col_estado + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_CONTAINS",
                                "values": [{"userEnteredValue": texto}],
                            },
                            "format": {
                                "backgroundColor": bg,
                                "textFormat": {"foregroundColor": fg, "bold": True},
                            },
                        },
                    },
                    "index": i,
                }
            }
        )
    return out


AUX_SUB_BD = "_AUX_BD_SUBRECETAS"
AUX_SUB_HIJOS = "_AUX_SUB_HIJOS"
AUX_MP_SUB = "_AUX_MP_SUB"
AUX_PADRES_UNION = "_AUX_SUB_PADRES_UNION"
SHEET_STAGING_SUB_CAB = "STAGING_SUB_CAB"


def _cargar_nombres_staging_sub_cab() -> list[str]:
    """Nombres en STAGING_SUB_CAB (incluye filas aún no promovidas)."""
    try:
        ws = open_staging().worksheet(SHEET_STAGING_SUB_CAB)
    except Exception:
        return []
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_subreceta")
    if hi is None:
        return []
    headers = [(c or "").strip() for c in vals[hi]]
    try:
        inom = headers.index("nombre_subreceta")
    except ValueError:
        return []
    vistos: set[str] = set()
    out: list[str] = []
    for row in vals[hi + 1 :]:
        nom = (row[inom] if inom < len(row) else "").strip()
        if not nom:
            continue
        key = nom.lower()
        if key in vistos:
            continue
        vistos.add(key)
        out.append(nom)
    out.sort(key=str.lower)
    return out


def _construir_padres_union(
    filas_master: list[list[str]], nombres_staging: list[str]
) -> list[list[str]]:
    """Lista única de nombres padre para dropdown en STAGING_SUB_DETALLE."""
    vistos: set[str] = set()
    union: list[list[str]] = []
    for nom, _cod in filas_master:
        key = nom.lower()
        if key in vistos:
            continue
        vistos.add(key)
        union.append([nom])
    for nom in nombres_staging:
        key = nom.lower()
        if key in vistos:
            continue
        vistos.add(key)
        union.append([nom])
    union.sort(key=lambda r: r[0].lower())
    return union


def refrescar_aux_subrecetas_en_staging(*, incluir_mp: bool = False) -> dict[str, int]:
    """
    Reescribe hojas aux del staging tras promover cabecera (o manualmente).
    Actualiza dropdowns de STAGING_SUB_DETALLE (padre, hijo SUB).
    """
    sheets = sheets_api()
    filas_sub = cargar_filas_subrecetas_maestro(solo_activas=True)
    header_sub = ["nombre_subreceta", "cod_subreceta"]
    for nombre in (AUX_SUB_BD, AUX_SUB_HIJOS):
        escribir_aux_hoja(sheets, nombre, header_sub, filas_sub, ocultar=True)

    union = _construir_padres_union(filas_sub, _cargar_nombres_staging_sub_cab())
    escribir_aux_hoja(
        sheets,
        AUX_PADRES_UNION,
        ["nombre_subreceta_padre"],
        union,
        ocultar=True,
    )

    n_mp = 0
    if incluir_mp:
        filas_mp = cargar_filas_mp_maestro()
        escribir_aux_hoja(
            sheets, AUX_MP_SUB, ["nombre_mp", "cod_mp_sistema"], filas_mp, ocultar=True
        )
        n_mp = len(filas_mp)

    return {
        "subrecetas": len(filas_sub),
        "padres_union": len(union),
        "mp": n_mp,
    }
