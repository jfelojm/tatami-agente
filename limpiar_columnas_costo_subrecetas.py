"""
Elimina columnas duplicadas de costo en BD_SUBRECETAS (fila 1 repetía
costo_lote_estandar / costo_unitario_estandar / costo_calc_at).

La lectura del agente usa la primera columna (G,H,I); las copias viejas (M,N,O)
mostraban números inflados y confundían auditorías.

Uso:
  python limpiar_columnas_costo_subrecetas.py --dry-run
  python limpiar_columnas_costo_subrecetas.py --produccion
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from googleapiclient.discovery import build
from google_credentials import google_credentials

load_dotenv(override=True)

SHEET = "BD_SUBRECETAS"
COLS_COSTO = ("costo_lote_estandar", "costo_unitario_estandar", "costo_calc_at")


def _sheet_id(sheets, spreadsheet_id: str, nombre: str) -> int:
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == nombre:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Hoja no encontrada: {nombre}")


def columnas_duplicadas_a_borrar(headers: list[str]) -> list[int]:
    """Índices 0-based de columnas duplicadas (segunda aparición en adelante)."""
    visto: set[str] = set()
    borrar: list[int] = []
    for i, h in enumerate(headers):
        key = (h or "").strip()
        if key not in COLS_COSTO:
            continue
        if key in visto:
            borrar.append(i)
        else:
            visto.add(key)
    return sorted(borrar, reverse=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sheets = build("sheets", "v4", credentials=creds)
    sid = os.environ["SPREADSHEET_ID"]
    ws_id = _sheet_id(sheets, sid, SHEET)

    row1 = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=sid, range=f"{SHEET}!1:1")
        .execute()
        .get("values", [[]])[0]
    )
    headers = [(c or "").strip() for c in row1]
    dup = columnas_duplicadas_a_borrar(headers)
    if not dup:
        print("No hay columnas de costo duplicadas en fila 1.")
        return

    print(f"Columnas duplicadas a eliminar (0-based): {dup}")
    for i in sorted(dup):
        print(f"  col {i} ({chr(65 + i) if i < 26 else '...'}): {headers[i]!r}")

    if args.dry_run:
        print("[DRY-RUN] no se borró nada")
        return

    requests = []
    for col_idx in dup:
        requests.append(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": ws_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    }
                }
            }
        )
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sid, body={"requests": requests}
    ).execute()
    print(f"Eliminadas {len(dup)} columnas en {SHEET}.")


if __name__ == "__main__":
    main()
