"""
Recalcula stock_actual en BD_MP_SISTEMA desde cero usando mov_inventario.

Formula por cod_mp:
  stock_actual = SUM(AJUSTE_POSITIVO) + SUM(ENTRADA)
               - SUM(SALIDA_VENTA) - SUM(AJUSTE_NEGATIVO)

Tambien actualiza costo_unitario_ref con el costo_unitario del ultimo
movimiento ENTRADA de cada MP (ordenado por fecha).

Es idempotente: se puede correr N veces y siempre da el mismo resultado.
"""

import os
import time
from collections import defaultdict
from dotenv import load_dotenv
from supabase import create_client
import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

TIPOS_SUMA = {"AJUSTE_POSITIVO", "ENTRADA"}
TIPOS_RESTA = {"SALIDA_VENTA", "AJUSTE_NEGATIVO"}


def paginar_todo(tabla, select):
    rows = []
    offset = 0
    while True:
        chunk = supabase.table(tabla).select(select).range(offset, offset + 999).execute().data
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def recalcular(dry_run=True):
    print("=" * 55)
    print(f"RECALCULAR STOCK BD_MP_SISTEMA - {'DRY RUN' if dry_run else 'PRODUCCION'}")
    print("=" * 55)

    print("\n[1] Leyendo mov_inventario completo desde Supabase...")
    movs = paginar_todo(
        "mov_inventario",
        "cod_mp_sistema,tipo_mov,cantidad_mov,costo_unitario,fecha,cod_mov"
    )
    print(f"    {len(movs)} movimientos totales")

    # Calcular stock por MP
    stock_calculado: dict[str, float] = defaultdict(float)
    # Ultimo costo ENTRADA por MP (para costo_unitario_ref)
    ultimo_costo: dict[str, tuple] = {}  # cod_mp -> (fecha, cod_mov, costo)

    for m in movs:
        cod = (m.get("cod_mp_sistema") or "").strip()
        if not cod:
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        cantidad = float(m.get("cantidad_mov") or 0)
        costo = float(m.get("costo_unitario") or 0)
        fecha = (m.get("fecha") or "").strip()
        cod_mov = (m.get("cod_mov") or "").strip()

        if tipo in TIPOS_SUMA:
            stock_calculado[cod] += cantidad
        elif tipo in TIPOS_RESTA:
            stock_calculado[cod] -= cantidad

        # Ultimo ENTRADA para costo_unitario_ref
        if tipo == "ENTRADA" and costo > 0:
            prev = ultimo_costo.get(cod)
            if prev is None or (fecha, cod_mov) > (prev[0], prev[1]):
                ultimo_costo[cod] = (fecha, cod_mov, costo)

    print(f"    {len(stock_calculado)} MPs con movimientos")
    print(f"    {len(ultimo_costo)} MPs con costo ENTRADA")

    print("\n[2] Leyendo BD_MP_SISTEMA en Sheets...")
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = next(
        (i for i, r in enumerate(values) if any(c.strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if header_row_idx is None:
        print("ERROR: no se encontro header cod_mp_sistema")
        return

    headers = [h.strip() for h in values[header_row_idx]]
    try:
        col_cod = headers.index("cod_mp_sistema")
        col_stock = headers.index("stock_actual") + 1
    except ValueError as e:
        print(f"ERROR columna no encontrada: {e}")
        return

    try:
        col_costo = headers.index("costo_unitario_ref") + 1
    except ValueError:
        col_costo = None
        print("  WARN: costo_unitario_ref no encontrada — solo se actualiza stock")

    print(f"    stock_actual=col {col_stock} | costo_unitario_ref=col {col_costo or 'N/A'}")

    print("\n[3] Preparando updates...")
    updates = []
    data_rows = values[header_row_idx + 1:]

    for i, row in enumerate(data_rows):
        if not any(c.strip() for c in row):
            continue
        cod = row[col_cod].strip() if col_cod < len(row) else ""
        if not cod:
            continue

        row_1based = header_row_idx + i + 2

        # Stock
        if cod in stock_calculado:
            nuevo_stock = round(stock_calculado[cod], 4)
            stock_anterior_str = row[col_stock - 1].strip() if col_stock - 1 < len(row) else "0"
            try:
                stock_anterior = float(stock_anterior_str.replace(",", ".")) if stock_anterior_str else 0.0
            except ValueError:
                stock_anterior = 0.0

            updates.append({
                "range": rowcol_to_a1(row_1based, col_stock),
                "values": [[nuevo_stock]],
            })
            if abs(nuevo_stock - stock_anterior) > 0.001:
                print(f"    {cod}: stock {stock_anterior} -> {nuevo_stock}")

        # Costo
        if col_costo and cod in ultimo_costo:
            _, _, costo = ultimo_costo[cod]
            updates.append({
                "range": rowcol_to_a1(row_1based, col_costo),
                "values": [[round(costo, 6)]],
            })

    print(f"\n    Total celdas a actualizar: {len(updates)}")

    if dry_run:
        print("\n    [DRY RUN] No se escribio nada. Corre con --produccion para aplicar.")
        return

    print("\n[4] Escribiendo en Sheets...")
    batch_size = 50
    for i in range(0, len(updates), batch_size):
        lote = updates[i: i + batch_size]
        ws.batch_update(lote, value_input_option=ValueInputOption.user_entered)
        print(f"    Lote {i // batch_size + 1}: {len(lote)} celdas")
        if i + batch_size < len(updates):
            time.sleep(1)

    print(f"\nCompletado. {len(updates)} celdas actualizadas en BD_MP_SISTEMA.")


if __name__ == "__main__":
    import sys
    DRY_RUN = "--produccion" not in sys.argv
    recalcular(dry_run=DRY_RUN)
