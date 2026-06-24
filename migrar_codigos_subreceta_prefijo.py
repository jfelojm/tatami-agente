"""
Migra cod_subreceta numérico (051) → canónico SUB-051 en Sheets.

Hojas: BD_SUBRECETAS, BD_SUBRECETAS_DETALLE, BD_RECETAS_DETALLE.

Uso:
  python migrar_codigos_subreceta_prefijo.py --dry-run
  python migrar_codigos_subreceta_prefijo.py --produccion
"""

from __future__ import annotations

import argparse
import os
import re

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption

from codigos_subreceta import cod_sub_canonico
from google_credentials import google_credentials

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_RE_SUB = re.compile(r"^SUB-?\s*", re.I)


def _abrir():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _header_idx(values: list[list], col: str) -> tuple[int, list[str]]:
    for i, row in enumerate(values):
        headers = [(c or "").strip() for c in row]
        if col in headers:
            return i, headers
    raise ValueError(f"Columna {col} no encontrada")


def _to_canon(val: str) -> str:
    v = (val or "").strip()
    if not v or v.startswith("#"):
        return v
    if _RE_SUB.match(v):
        return cod_sub_canonico(v)
    if v.replace(".", "").isdigit():
        return cod_sub_canonico(v)
    return v


def migrar_hoja(ws, columnas: list[str], *, dry_run: bool) -> int:
    vals = ws.get_all_values()
    hi, headers = _header_idx(vals, columnas[0])
    cols_idx = [headers.index(c) for c in columnas]
    cambios = 0
    updates: list[dict] = []

    for ri, row in enumerate(vals[hi + 1 :], start=hi + 2):
        for ci, col_name in zip(cols_idx, columnas):
            if ci >= len(row):
                continue
            old = (row[ci] or "").strip()
            new = _to_canon(old)
            if new and new != old:
                cambios += 1
                updates.append(
                    {
                        "range": gspread.utils.rowcol_to_a1(ri, ci + 1),
                        "values": [[new]],
                    }
                )

    print(f"  {ws.title}: {cambios} celdas")
    if not dry_run and updates:
        for chunk_start in range(0, len(updates), 500):
            ws.batch_update(
                updates[chunk_start : chunk_start + 500],
                value_input_option=ValueInputOption.user_entered,
            )
    return cambios


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        raise SystemExit(2)
    dry = not args.produccion

    sh = _abrir()
    print(f"Migracion codigos a SUB-xxx ({'DRY-RUN' if dry else 'PRODUCCION'})")
    print(f"  Ejemplo: 051 -> {cod_sub_canonico('051')}")

    t = 0
    t += migrar_hoja(sh.worksheet("BD_SUBRECETAS"), ["cod_subreceta"], dry_run=dry)
    t += migrar_hoja(
        sh.worksheet("BD_SUBRECETAS_DETALLE"),
        ["cod_subreceta_padre", "cod_subreceta_hijo"],
        dry_run=dry,
    )
    t += migrar_hoja(sh.worksheet("BD_RECETAS_DETALLE"), ["cod_subreceta"], dry_run=dry)

    print(f"Total celdas: {t}")
    if dry:
        print("Luego: sync_stock_subrecetas_maestro.py --produccion")
        print("      calcular_costo_subrecetas.py --produccion")


if __name__ == "__main__":
    main()
