"""
Migra BD_RECETAS_DETALLE al layout con subrecetas.

Encabezado resultante:
  nombre_receta, cod_receta, variedad_smart_menu,
  nombre_subreceta, cod_subreceta, nombre_mp, cod_mp_sistema,
  cantidad, unidad_base, cod_bodega, merma_pct,
  es_opcional, pct_aplicacion

Uso:
  python migrate_bd_recetas_detalle.py
  python migrate_bd_recetas_detalle.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv
from googleapiclient.discovery import build
from gspread.utils import ValueInputOption
from google_credentials import google_credentials

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
MASTER_ID = os.environ["SPREADSHEET_ID"]
SHEET = "BD_RECETAS_DETALLE"

NEW_HEADERS = [
    "nombre_receta",
    "cod_receta",
    "variedad_smart_menu",
    "nombre_subreceta",
    "cod_subreceta",
    "nombre_mp",
    "cod_mp_sistema",
    "cantidad",
    "unidad_base",
    "cod_bodega",
    "merma_pct",
    "es_opcional",
    "pct_aplicacion",
]

BODEGAS_VALIDAS = ("BOD-001", "BOD-002", "BOD-003", "BOD-005")


def _norm_cod_mp(c: str) -> str:
    s = (c or "").strip()
    n = s.lstrip("0")
    return n if n else "0"


def _load_mp_lookup(sheets) -> dict[str, dict]:
    """cod_mp_norm -> {unidad_base, cod_bodega} (prioriza BOD-001, luego 002)."""
    resp = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=MASTER_ID, range="BD_MP_SISTEMA!A:L")
        .execute()
    )
    values = resp.get("values") or []
    hi = next(
        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if hi is None:
        return {}
    headers = [(c or "").strip() for c in values[hi]]
    try:
        ic = headers.index("cod_mp_sistema")
        iu = headers.index("unidad_base")
        ib = headers.index("cod_bodega")
    except ValueError:
        return {}

    prio = {"BOD-001": 0, "BOD-002": 1, "BOD-003": 2, "BOD-005": 3}
    out: dict[str, dict] = {}
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        cod = row[ic].strip() if ic < len(row) else ""
        if not cod:
            continue
        nk = _norm_cod_mp(cod)
        bod = (row[ib] if ib < len(row) else "").strip().upper()
        uni = (row[iu] if iu < len(row) else "").strip()
        prev = out.get(nk)
        if prev is None or prio.get(bod, 99) < prio.get(prev.get("cod_bodega", ""), 99):
            out[nk] = {"unidad_base": uni, "cod_bodega": bod}
    return out


def _header_idx(values: list[list[str]]) -> int | None:
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_receta" for c in row):
            return i
    return None


def _row_dict(headers: list[str], row: list[str]) -> dict[str, str]:
    return {
        headers[k]: (row[k] if k < len(row) else "").strip()
        for k in range(len(headers))
        if headers[k]
    }


def _migrate_row(old: dict[str, str], mp_lookup: dict[str, dict]) -> list[str]:
    cod_mp = old.get("cod_mp_sistema", "")
    nk = _norm_cod_mp(cod_mp)
    mp_info = mp_lookup.get(nk, {})
    merma = old.get("merma_pct", "") or "0"
    return [
        old.get("nombre_receta", ""),
        old.get("cod_receta", ""),
        old.get("variedad_smart_menu", ""),
        old.get("nombre_subreceta", ""),
        old.get("cod_subreceta", ""),  # vacío en filas existentes
        old.get("nombre_mp", ""),
        cod_mp,
        old.get("cantidad", ""),
        old.get("unidad_base", "") or mp_info.get("unidad_base", ""),
        old.get("cod_bodega", "") or mp_info.get("cod_bodega", ""),
        merma,
        old.get("es_opcional", "") or "NO",
        old.get("pct_aplicacion", "") or "1",
    ]


def migrate(dry_run: bool = False) -> None:
    creds = google_credentials(SCOPES)
    sheets = build("sheets", "v4", credentials=creds)

    resp = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=MASTER_ID, range=f"{SHEET}!A:Z")
        .execute()
    )
    values = resp.get("values") or []
    hi = _header_idx(values)
    if hi is None:
        raise SystemExit("No se encontró fila de cabecera con cod_receta")

    headers = [(c or "").strip() for c in values[hi]]
    if "cod_subreceta" in headers and headers.index("cod_subreceta") <= 5:
        print("BD_RECETAS_DETALLE ya parece migrada (existe cod_subreceta). Nada que hacer.")
        return

    mp_lookup = _load_mp_lookup(sheets)
    print(f"Lookup MP: {len(mp_lookup)} códigos desde BD_MP_SISTEMA")

    new_rows: list[list[str]] = [NEW_HEADERS]
    skipped = 0
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        old = _row_dict(headers, row)
        if not old.get("cod_receta"):
            skipped += 1
            continue
        new_rows.append(_migrate_row(old, mp_lookup))

    print(f"Filas de receta a escribir: {len(new_rows) - 1} (omitidas sin cod_receta: {skipped})")

    if dry_run:
        print("[DRY RUN] Primeras 3 filas migradas:")
        for r in new_rows[1:4]:
            print(" ", r)
        return

    # Limpiar hoja y escribir (más seguro que insertar columnas en 4k+ filas)
    meta = sheets.spreadsheets().get(spreadsheetId=MASTER_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in meta["sheets"]
        if s["properties"]["title"] == SHEET
    )

    sheets.spreadsheets().values().clear(
        spreadsheetId=MASTER_ID,
        range=f"{SHEET}!A:Z",
    ).execute()

    batch = 1500
    for i in range(0, len(new_rows), batch):
        chunk = new_rows[i : i + batch]
        start = i + 1
        end = start + len(chunk) - 1
        sheets.spreadsheets().values().update(
            spreadsheetId=MASTER_ID,
            range=f"{SHEET}!A{start}",
            valueInputOption="USER_ENTERED",
            body={"values": chunk},
        ).execute()
        print(f"  Escrito filas {start}-{end}")
        if i + batch < len(new_rows):
            time.sleep(1)

    # Formato encabezado + freeze
    ncols = len(NEW_HEADERS)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=MASTER_ID,
        body={
            "requests": [
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
                                "backgroundColor": {"red": 0.11, "green": 0.11, "blue": 0.10},
                                "textFormat": {
                                    "foregroundColor": {
                                        "red": 0.95,
                                        "green": 0.88,
                                        "blue": 0.75,
                                    },
                                    "bold": True,
                                },
                                "horizontalAlignment": "CENTER",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 5000,
                            "startColumnIndex": 9,
                            "endColumnIndex": 10,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [{"userEnteredValue": b} for b in BODEGAS_VALIDAS],
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                },
            ]
        },
    ).execute()

    print("OK — BD_RECETAS_DETALLE migrada.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
