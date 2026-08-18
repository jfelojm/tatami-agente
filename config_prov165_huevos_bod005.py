"""Proveedor 165 (Inguil Lazo / huevos): ingreso BOD-005."""
from __future__ import annotations

import os
import time

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from google_credentials import google_credentials
from staging_common import find_header_row

load_dotenv(override=True)

PROV = "165"
DESC = "HUEVOS"
BOD = "BOD-005"


def main() -> int:
    sh = gspread.authorize(
        google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    ).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_item_prov")
    if hi is None:
        print("ERROR: sin header BD_ITEMS_PROV")
        return 1
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_proveedor")
    idesc = next(
        i for i, k in enumerate(headers) if k.lower() in ("descripcion_proveedor", "descripcion")
    )
    ibod = headers.index("cod_bodega_destino")

    updated = 0
    for r_idx, row in enumerate(vals[hi + 1 :], start=hi + 2):
        prov = (row[icod] if icod < len(row) else "").strip()
        desc = (row[idesc] if idesc < len(row) else "").strip().upper()
        if prov != PROV:
            continue
        if "HUEVO" not in desc:
            continue
        old = (row[ibod] if ibod < len(row) else "").strip()
        cell = rowcol_to_a1(r_idx, ibod + 1)
        ws.update(
            range_name=cell,
            values=[[BOD]],
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"Fila {r_idx}: {desc} {old} -> {BOD}")
        updated += 1
        time.sleep(0.5)

    if not updated:
        print("No se encontro item Huevos proveedor 165")
        return 1

    import procesar_facturas_drive as pfd

    pfd._items_prov_cache = None
    pfd._invalidar_cache_layout_precio_items_prov()
    print("Cache BD_ITEMS_PROV invalidada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
