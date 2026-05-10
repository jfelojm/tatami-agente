"""
Replica en BD_MP_SISTEMA (Sheets) el consumo acumulado de mov_inventario (SALIDA_VENTA).

Para cada cod_mp_sistema: stock_actual_hoja -= sum(cantidad_mov).

IMPORTANTE: asume que el stock_actual en la hoja es el valor *previo* a esos movimientos.
Si descargo_inventario ya descontó en Sheets, volver a ejecutar esto restará dos veces.
"""

import os
import time
from collections import defaultdict

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def run():
    print("[1] Leyendo consumos desde Supabase...")
    r = supabase.table("mov_inventario").select(
        "cod_mp_sistema,cantidad_mov"
    ).eq("tipo_mov", "SALIDA_VENTA").execute()

    consumos = defaultdict(float)
    for m in r.data:
        consumos[m["cod_mp_sistema"]] += float(m["cantidad_mov"])
    print(f"  {len(consumos)} MPs con consumo registrado")

    print("[2] Abriendo BD_MP_SISTEMA...")
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any(c.strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break

    if header_row_idx is None:
        print("ERROR: no se encontró header cod_mp_sistema")
        return

    headers = values[header_row_idx]

    try:
        col_cod = headers.index("cod_mp_sistema")
        col_stock = headers.index("stock_actual")
    except ValueError as e:
        print(f"ERROR columna no encontrada: {e}")
        return

    print(
        f"  Headers en fila {header_row_idx + 1} | "
        f"cod_mp_sistema=col {col_cod + 1} | stock_actual=col {col_stock + 1}"
    )

    print("[3] Calculando nuevos stocks...")
    updates: list[tuple[int, int, float]] = []
    no_encontrados: list[str] = []

    data_rows = values[header_row_idx + 1 :]
    for i, row in enumerate(data_rows):
        if not any(c.strip() for c in row):
            continue
        cod = row[col_cod].strip() if col_cod < len(row) else ""
        if not cod or cod not in consumos:
            continue

        stock_actual_str = row[col_stock].strip() if col_stock < len(row) else "0"
        try:
            stock_actual = (
                float(stock_actual_str.replace(",", ".")) if stock_actual_str else 0.0
            )
        except ValueError:
            stock_actual = 0.0

        nuevo_stock = round(stock_actual - consumos[cod], 4)
        row_1based = header_row_idx + i + 2
        updates.append((row_1based, col_stock + 1, nuevo_stock))
        print(
            f"  {cod} | stock_actual={stock_actual} - "
            f"consumo={round(consumos[cod], 4)} = {nuevo_stock}"
        )

    cods_sheets = set()
    for i, row in enumerate(data_rows):
        if len(row) > col_cod:
            c = row[col_cod].strip()
            if c:
                cods_sheets.add(c)

    for cod in consumos:
        if cod not in cods_sheets:
            no_encontrados.append(cod)

    if no_encontrados:
        print(
            f"\n  WARN: {len(no_encontrados)} MPs no encontrados en Sheets: {no_encontrados}"
        )

    print(f"\n[4] Actualizando {len(updates)} celdas en Sheets...")
    if not updates:
        print("  Nada que actualizar.")
        return

    cell_updates = []
    for row_idx, col_idx, valor in updates:
        a1 = rowcol_to_a1(row_idx, col_idx)
        cell_updates.append({"range": a1, "values": [[valor]]})

    batch_size = 20
    for i in range(0, len(cell_updates), batch_size):
        lote = cell_updates[i : i + batch_size]
        ws.batch_update(lote, value_input_option=ValueInputOption.user_entered)
        print(f"  Lote {i // batch_size + 1}: {len(lote)} celdas actualizadas")
        if i + batch_size < len(cell_updates):
            time.sleep(2)

    print(f"\n  Completado. {len(updates)} MPs actualizados en Sheets.")


if __name__ == "__main__":
    run()
