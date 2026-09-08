"""
Configura el piso de botellas para órdenes de compra de barra.

La columna stock_minimo_botellas es una política comercial por MP y no modifica
par_level ni consumo_diario_calculado.

Uso:
  python configurar_stock_minimo_botellas.py --dry-run
  python configurar_stock_minimo_botellas.py --produccion
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from google_credentials import google_credentials

load_dotenv(override=True)

SHEET = "BD_MP_SISTEMA"
COL_MINIMO = "stock_minimo_botellas"
MINIMO_DEFAULT = 2

# MPs activas vendidas tanto por copa/copeo como por botella completa.
# Partager 219/220 quedan fuera mientras estén activa=NO.
MPS_COPEO_Y_BOTELLA = {
    "213": "Vino Garnacha Rosado",
    "233": "Vino JP Chenet Ice Edition",
    "298": "Vino rose Cono Sur",
    "310": "Vino tinto cero coma",
    "311": "Vino blanco cero coma",
    "V0ALC1": "Sangre de Toro Blanco",
    "V0ALC2": "Sangre de Toro Tinto",
}


def _abrir_maestro():
    import gspread

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _norm_cod(value: object) -> str:
    s = str(value or "").strip()
    if s.isdigit():
        return s.zfill(3)
    return s.upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--produccion", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indique --dry-run o --produccion")
        return 2

    sh = _abrir_maestro()
    ws = sh.worksheet(SHEET)
    values = ws.get_all_values()
    header_idx = next(
        i
        for i, row in enumerate(values)
        if "cod_mp_sistema" in [(c or "").strip() for c in row]
    )
    headers = [(c or "").strip() for c in values[header_idx]]

    if COL_MINIMO in headers:
        col_min = headers.index(COL_MINIMO) + 1
    else:
        col_min = len(headers) + 1
        print(f"+ Crear columna {COL_MINIMO} en {rowcol_to_a1(header_idx + 1, col_min)}")
        if args.produccion:
            ws.update_cell(header_idx + 1, col_min, COL_MINIMO)
        headers.append(COL_MINIMO)

    col_cod = headers.index("cod_mp_sistema")
    col_nombre = headers.index("nombre_mp") if "nombre_mp" in headers else None
    col_activa = headers.index("activa") if "activa" in headers else None
    updates: list[dict] = []
    encontrados: set[str] = set()

    for sheet_row, row in enumerate(values[header_idx + 1 :], start=header_idx + 2):
        cod = _norm_cod(row[col_cod] if col_cod < len(row) else "")
        if cod not in MPS_COPEO_Y_BOTELLA:
            continue
        nombre = (
            row[col_nombre]
            if col_nombre is not None and col_nombre < len(row)
            else MPS_COPEO_Y_BOTELLA[cod]
        )
        activa = (
            row[col_activa]
            if col_activa is not None and col_activa < len(row)
            else "SI"
        )
        encontrados.add(cod)
        print(
            f"  {cod} {nombre} | activa={activa or 'SI'} "
            f"-> {COL_MINIMO}={MINIMO_DEFAULT}"
        )
        updates.append(
            {
                "range": rowcol_to_a1(sheet_row, col_min),
                "values": [[MINIMO_DEFAULT]],
            }
        )

    faltantes = sorted(set(MPS_COPEO_Y_BOTELLA) - encontrados)
    if faltantes:
        print(f"WARN: MPs no encontrados en maestro: {faltantes}")

    if args.produccion and updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        print(f"\nOK: {len(updates)} fila(s) configuradas.")
    else:
        print(f"\nDRY-RUN: {len(updates)} fila(s); sin cambios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
