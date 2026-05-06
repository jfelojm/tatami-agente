import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1
from supabase import create_client

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _get_sheet():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0


def _daterange(start: str, end: str) -> list[str]:
    d1 = datetime.strptime(start, "%Y-%m-%d").date()
    d2 = datetime.strptime(end, "%Y-%m-%d").date()
    if d2 < d1:
        d1, d2 = d2, d1
    out = []
    d = d1
    while d <= d2:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _consumo_promedio_diario_por_mp(fecha_inicio: str, fecha_fin: str) -> dict[str, float]:
    """
    Calcula consumo diario promedio en base a mov_inventario SALIDA_VENTA.
    consumo_diario = total_salidas / numero_dias_en_rango
    """
    dias = _daterange(fecha_inicio, fecha_fin)
    if not dias:
        return {}

    # En Supabase, 'fecha' suele ser timestamp (sin tz). No se puede ilike.
    # Consultamos todo el rango [inicio, fin+1) y promediamos por nro de días.
    start_ts = f"{fecha_inicio}T00:00:00"
    end_ts = f"{(datetime.strptime(fecha_fin, '%Y-%m-%d').date() + timedelta(days=1)).strftime('%Y-%m-%d')}T00:00:00"

    resp = (
        supabase.table("mov_inventario")
        .select("cod_mp_sistema,cantidad_mov")
        .eq("tipo_mov", "SALIDA_VENTA")
        .gte("fecha", start_ts)
        .lt("fecha", end_ts)
        .execute()
    )

    total_por_mp: dict[str, float] = defaultdict(float)
    for m in resp.data:
        cod = (m.get("cod_mp_sistema") or "").strip()
        if not cod:
            continue
        total_por_mp[cod] += _safe_float(m.get("cantidad_mov"))

    n_dias = len(dias)
    return {cod: total / n_dias for cod, total in total_por_mp.items()}


def calcular_par_levels(dry_run: bool = False):
    """
    Actualiza BD_MP_SISTEMA en Sheets:
      - consumo_diario_calculado
      - par_level = consumo_diario_calculado * dias_cobertura (si existe) o * 7 por defecto

    Requiere columnas en BD_MP_SISTEMA:
      - cod_mp_sistema
      - stock_actual
      - par_level
      - consumo_diario_calculado
    """
    fecha_fin = date.today().strftime("%Y-%m-%d")
    dias_ventana = int(os.getenv("PAR_LEVEL_DIAS_VENTANA", "30") or "30")
    fecha_inicio = (date.today() - timedelta(days=dias_ventana - 1)).strftime("%Y-%m-%d")
    dias_cobertura = float(os.getenv("PAR_LEVEL_DIAS_COBERTURA", "7") or "7")

    print(f"Ventana consumo: {fecha_inicio} -> {fecha_fin} ({dias_ventana} dias)")
    print(f"Dias cobertura (par): {dias_cobertura}")

    print("[1] Calculando consumos desde Supabase...")
    consumo_diario = _consumo_promedio_diario_por_mp(fecha_inicio, fecha_fin)
    print(f"  MPs con consumo en ventana: {len(consumo_diario)}")

    print("[2] Leyendo BD_MP_SISTEMA...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any(c.strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break
    if header_row_idx is None:
        print("ERROR: no se encontró header cod_mp_sistema en BD_MP_SISTEMA")
        return

    headers = [h.strip() for h in values[header_row_idx]]
    try:
        col_cod = headers.index("cod_mp_sistema") + 1
        col_par = headers.index("par_level") + 1
        col_consumo = headers.index("consumo_diario_calculado") + 1
    except ValueError as e:
        print(f"  WARN columna no encontrada: {e}")
        return

    data_rows = values[header_row_idx + 1 :]
    updates = []

    print("[3] Calculando par levels...")
    for i, row in enumerate(data_rows):
        if not any(c.strip() for c in row):
            continue
        cod = row[col_cod - 1].strip() if len(row) >= col_cod else ""
        if not cod:
            continue
        cd = float(consumo_diario.get(cod, 0.0))
        par_level = round(cd * dias_cobertura, 4) if cd > 0 else 0.0
        row_1based = header_row_idx + i + 2

        if cd > 0:
            print(f"  {cod}: consumo_diario={round(cd,6)} par={par_level}")

        if not dry_run and par_level > 0:
            updates.append(
                {"range": rowcol_to_a1(row_1based, col_par), "values": [[par_level]]}
            )
            updates.append(
                {"range": rowcol_to_a1(row_1based, col_consumo), "values": [[cd]]}
            )

    print(f"[4] {'DRY RUN - no escribe' if dry_run else 'Escribiendo'}: {len(updates)} updates")
    if dry_run or not updates:
        return

    for i in range(0, len(updates), 50):
        ws.batch_update(updates[i : i + 50])
        time.sleep(1)

    print("Listo.")


if __name__ == "__main__":
    import sys
    from datetime import date

    DRY_RUN = "--dry-run" in sys.argv
    calcular_par_levels(dry_run=DRY_RUN)
