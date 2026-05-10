"""
Test minimo: intenta escribir en BD_MP_SISTEMA y verifica que el cambio persiste.
Escribe un valor en stock_actual de la primera MP, lo verifica, luego restaura.
"""
import os
import time
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def run():
    print("Conectando a Sheets...")
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))
    ws = sh.worksheet("BD_MP_SISTEMA")
    print(f"  Hoja abierta: {ws.title} | {ws.row_count} filas x {ws.col_count} cols")

    values = ws.get_all_values()
    print(f"  Total filas con datos: {len(values)}")

    # Encontrar header
    header_row_idx = next(
        (i for i, r in enumerate(values) if any(c.strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if header_row_idx is None:
        print("ERROR: no se encontro header cod_mp_sistema")
        return

    headers = [h.strip() for h in values[header_row_idx]]
    print(f"  Headers en fila {header_row_idx + 1}: {headers[:12]}")

    try:
        col_cod = headers.index("cod_mp_sistema")
        col_stock = headers.index("stock_actual") + 1  # 1-based para Sheets
        print(f"  cod_mp_sistema=col_idx {col_cod} | stock_actual=col {col_stock} (1-based)")
    except ValueError as e:
        print(f"ERROR columna no encontrada: {e}")
        return

    # Primera fila de datos
    data_rows = values[header_row_idx + 1:]
    primera_fila = None
    primera_row_1based = None
    for i, row in enumerate(data_rows):
        if not any(c.strip() for c in row):
            continue
        cod = row[col_cod].strip() if col_cod < len(row) else ""
        if cod:
            primera_fila = row
            primera_row_1based = header_row_idx + i + 2
            break

    if not primera_fila:
        print("ERROR: no se encontro ninguna fila de datos")
        return

    cod_mp = primera_fila[col_cod].strip()
    stock_actual_str = primera_fila[col_stock - 1].strip() if col_stock - 1 < len(primera_fila) else ""
    print(f"\n  Primera MP: cod={cod_mp} | stock_actual='{stock_actual_str}' | fila={primera_row_1based}")

    # Leer celda directamente para confirmar
    celda_a1 = rowcol_to_a1(primera_row_1based, col_stock)
    val_directo = ws.cell(primera_row_1based, col_stock).value
    print(f"  Celda {celda_a1} leida directamente: '{val_directo}'")

    # Intentar escribir valor de prueba
    valor_prueba = 99999.1234
    print(f"\n  Escribiendo valor de prueba {valor_prueba} en {celda_a1}...")
    try:
        ws.update(range_name=celda_a1, values=[[valor_prueba]])
        print("  ws.update() ejecutado sin excepcion")
    except Exception as e:
        print(f"  ERROR en ws.update(): {e}")
        return

    time.sleep(2)

    # Verificar que se escribio
    val_post = ws.cell(primera_row_1based, col_stock).value
    print(f"  Valor despues de escribir: '{val_post}'")

    if str(valor_prueba) in str(val_post) or "99999" in str(val_post or ""):
        print("  ESCRITURA CONFIRMADA - el valor cambio en Sheets")
    else:
        print("  ESCRITURA FALLIDA - el valor NO cambio en Sheets")
        print(f"  Esperado: {valor_prueba} | Obtenido: {val_post}")

    # Restaurar valor original
    print(f"\n  Restaurando valor original '{stock_actual_str}'...")
    try:
        ws.update(range_name=celda_a1, values=[[stock_actual_str]])
        print("  Restaurado OK")
    except Exception as e:
        print(f"  ERROR restaurando: {e}")

if __name__ == "__main__":
    run()
