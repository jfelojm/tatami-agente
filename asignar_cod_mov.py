"""
asignar_cod_mov.py
------------------
Lee MOV_INVENTARIO de Google Sheets, asigna cod_mov a las filas que lo tienen vacío,
escribe de vuelta en Sheets y sube esas filas a Supabase.

Uso:
    python asignar_cod_mov.py           # dry-run (solo muestra qué haría)
    python asignar_cod_mov.py --commit  # ejecuta cambios reales
"""

import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
SPREADSHEET_ID      = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS_PATH   = os.getenv("GOOGLE_CREDENTIALS_PATH")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
HOJA_MOV            = "MOV_INVENTARIO"
FILA_HEADER         = 3   # fila donde están los nombres de columna (1-indexed)
FILA_DATOS_INICIO   = 4   # primera fila de datos

DRY_RUN = "--commit" not in sys.argv

# ── Columnas esperadas (en orden) ────────────────────────────────────────────
COLS = [
    "cod_mov", "fecha", "tipo_mov", "cod_mp_sistema", "nombre_mp",
    "cod_bodega_origen", "cod_bodega_destino", "cantidad_mov", "unidad_base",
    "costo_unitario", "costo_total", "origen_documento", "num_documento",
    "registrado_por", "observaciones"
]

def conectar_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def leer_hoja(wb):
    ws = wb.worksheet(HOJA_MOV)
    all_values = ws.get_all_values()
    
    # Header en fila FILA_HEADER (0-indexed: FILA_HEADER - 1)
    header_row = all_values[FILA_HEADER - 1]
    
    # Mapear nombre columna → índice
    col_idx = {h.strip(): i for i, h in enumerate(header_row) if h.strip()}
    
    # Verificar columnas críticas
    for c in ["cod_mov", "fecha", "tipo_mov", "cod_mp_sistema", "nombre_mp",
              "cod_bodega_origen", "cod_bodega_destino", "cantidad_mov", "unidad_base"]:
        if c not in col_idx:
            print(f"WARN: Columna '{c}' no encontrada en header. Header detectado: {header_row[:10]}")
    
    rows = []
    for i, row in enumerate(all_values[FILA_DATOS_INICIO - 1:], start=FILA_DATOS_INICIO):
        if not any(row):  # fila completamente vacía
            continue
        d = {}
        for col, idx in col_idx.items():
            d[col] = row[idx].strip() if idx < len(row) else ""
        d["_sheet_row"] = i
        rows.append(d)
    
    return ws, col_idx, rows

def generar_cod_mov(fecha_str, secuencia):
    """
    Genera cod_mov en formato MOV-YYYYMMDD-NNN.
    fecha_str puede ser DD/MM/YYYY o YYYY-MM-DD.
    """
    try:
        if "/" in fecha_str:
            dt = datetime.strptime(fecha_str, "%d/%m/%Y")
        else:
            dt = datetime.strptime(fecha_str[:10], "%Y-%m-%d")
        fecha_fmt = dt.strftime("%Y%m%d")
    except Exception:
        fecha_fmt = datetime.today().strftime("%Y%m%d")
    
    return f"MOV-{fecha_fmt}-{secuencia:03d}"

def parse_numero(val):
    """Convierte string a float, manejando comas decimales."""
    if val == "" or val is None:
        return None
    try:
        return float(str(val).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None

def main():
    print(f"{'[DRY-RUN]' if DRY_RUN else '[COMMIT]'} asignar_cod_mov.py\n")
    
    # ── Conectar ─────────────────────────────────────────────────────────────
    print("Conectando a Google Sheets...")
    wb = conectar_sheets()
    ws, col_idx, rows = leer_hoja(wb)
    print(f"  {len(rows)} filas de datos leídas desde {HOJA_MOV}\n")
    
    # ── Identificar filas sin cod_mov ────────────────────────────────────────
    sin_cod = [r for r in rows if not r.get("cod_mov", "").strip()]
    con_cod = [r for r in rows if r.get("cod_mov", "").strip()]
    
    print(f"  Filas con cod_mov:    {len(con_cod)}")
    print(f"  Filas SIN cod_mov:    {len(sin_cod)}")
    
    if not sin_cod:
        print("\nOK: Todas las filas ya tienen cod_mov. Nada que hacer.")
        return
    
    # ── Determinar siguiente secuencia ───────────────────────────────────────
    # Buscar el máximo número secuencial ya existente para cada fecha
    seq_por_fecha = {}
    for r in con_cod:
        cod = r["cod_mov"]
        # Formato: MOV-YYYYMMDD-NNN
        partes = cod.split("-")
        if len(partes) == 3:
            fecha_key = partes[1]
            try:
                n = int(partes[2])
                seq_por_fecha[fecha_key] = max(seq_por_fecha.get(fecha_key, 0), n)
            except ValueError:
                pass
    
    # ── Asignar cod_mov a filas sin código ───────────────────────────────────
    actualizaciones_sheets = []  # (sheet_row, col_idx_cod_mov, nuevo_cod)
    filas_supabase = []
    
    print(f"\nAsignando cod_mov a {len(sin_cod)} filas:")
    
    for r in sin_cod:
        fecha_str = r.get("fecha", "")
        
        # Determinar fecha key para secuencia
        dt = None
        try:
            if "/" in fecha_str:
                dt = datetime.strptime(fecha_str, "%d/%m/%Y")
            else:
                dt = datetime.strptime(fecha_str[:10], "%Y-%m-%d")
            fecha_key = dt.strftime("%Y%m%d")
        except Exception:
            dt = datetime.today()
            fecha_key = dt.strftime("%Y%m%d")
        
        seq_por_fecha[fecha_key] = seq_por_fecha.get(fecha_key, 0) + 1
        cod = generar_cod_mov(fecha_str, seq_por_fecha[fecha_key])
        
        print(f"  Fila {r['_sheet_row']:4d} | {r.get('nombre_mp','?'):35s} | {r.get('cantidad_mov','?'):>10} {r.get('unidad_base',''):4s} -> {cod}")
        
        # Para actualizar en Sheets: columna A (índice 0) es cod_mov
        col_letra_idx = col_idx.get("cod_mov", 0)  # índice 0-based
        actualizaciones_sheets.append({
            "row": r["_sheet_row"],
            "col": col_letra_idx + 1,  # gspread usa 1-indexed
            "value": cod
        })
        
        # Preparar fila para Supabase
        cantidad = parse_numero(r.get("cantidad_mov", ""))
        costo_u  = parse_numero(r.get("costo_unitario", ""))
        costo_t  = parse_numero(r.get("costo_total", ""))
        
        fila_sb = {
            "cod_mov":           cod,
            "fecha":             dt.strftime("%Y-%m-%dT00:00:00") if fecha_str else None,
            "tipo_mov":          r.get("tipo_mov", ""),
            "cod_mp_sistema":    r.get("cod_mp_sistema", "") or None,
            "nombre_mp":         r.get("nombre_mp", "") or None,
            "cod_bodega_origen": r.get("cod_bodega_origen", "") or None,
            "cod_bodega_destino":r.get("cod_bodega_destino", "") or None,
            "cantidad_mov":      cantidad,
            "unidad_base":       r.get("unidad_base", "") or None,
            "costo_unitario":    costo_u,
            "costo_total":       costo_t,
            "origen_documento":  r.get("origen_documento", "") or None,
            "num_documento":     r.get("num_documento", "") or None,
            "registrado_por":    r.get("registrado_por", "") or None,
            "observaciones":     r.get("observaciones", "") or None,
        }
        # Limpiar Nones en campos de texto vacíos
        filas_supabase.append(fila_sb)
    
    print(f"\nResumen: {len(actualizaciones_sheets)} cod_mov a escribir en Sheets")
    print(f"         {len(filas_supabase)} filas a insertar en Supabase\n")
    
    if DRY_RUN:
        print("DRY-RUN: no se escribio nada. Usa --commit para ejecutar.")
        return
    
    # ── Escribir cod_mov en Sheets (batch) ───────────────────────────────────
    print("Escribiendo cod_mov en Sheets...")
    updates = []
    for u in actualizaciones_sheets:
        cell = gspread.Cell(u["row"], u["col"], u["value"])
        updates.append(cell)
    
    ws.update_cells(updates, value_input_option="RAW")
    print(f"  OK: {len(updates)} celdas actualizadas en Sheets\n")
    
    # ── Insertar en Supabase ─────────────────────────────────────────────────
    print("Insertando en Supabase...")
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    BATCH = 50
    total_ok = 0
    for i in range(0, len(filas_supabase), BATCH):
        lote = filas_supabase[i:i+BATCH]
        res = sb.table("mov_inventario").insert(lote).execute()
        total_ok += len(lote)
        print(f"  Lote {i//BATCH + 1}: {len(lote)} filas insertadas")
    
    print(f"\nCompletado: {total_ok} filas en Supabase | {len(updates)} celdas en Sheets")

if __name__ == "__main__":
    main()
