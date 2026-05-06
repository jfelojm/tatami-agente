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


def _cargar_recetas_detalle() -> list[dict]:
    sh = _get_sheet()
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    values = ws.get_all_values()
    headers = values[2]
    rows = values[4:]
    out: list[dict] = []
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[i].strip(): row[i].strip()
            for i in range(min(len(headers), len(row)))
            if headers[i].strip()
        }
        out.append(r)
    return out


def calcular_consumo_diario(recetas: list[dict]) -> dict[str, float]:
    print("  Leyendo hist_ventas desde Supabase...")

    todas_ventas: list[dict] = []
    offset = 0
    while True:
        r = (
            supabase.table("hist_ventas")
            .select("cod_receta,variedad_smart_menu,cantidad_vendida,fecha")
            .eq("estado_match", "PROCESADO")
            .range(offset, offset + 999)
            .execute()
        )
        if not r.data:
            break
        todas_ventas.extend(r.data)
        if len(r.data) < 1000:
            break
        offset += 1000

    print(f"  {len(todas_ventas)} ventas cargadas")

    lookup_recetas: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ing in recetas:
        cod_r = ing.get("cod_receta", "").strip()
        var = ing.get("variedad_smart_menu", "").strip().upper()
        lookup_recetas[(cod_r, var)].append(ing)

    consumo_total: dict[str, float] = defaultdict(float)
    fechas_activas: set[str] = set()

    for venta in todas_ventas:
        cod_r = (venta.get("cod_receta") or "").strip()
        variedad = (venta.get("variedad_smart_menu") or "").strip().upper()
        cantidad = _safe_float(venta.get("cantidad_vendida") or 0)
        fecha = (venta.get("fecha") or "").strip()

        if not cod_r:
            continue
        if fecha:
            fechas_activas.add(fecha)

        ingredientes = (
            lookup_recetas.get((cod_r, variedad))
            or lookup_recetas.get((cod_r, ""))
            or []
        )

        for ing in ingredientes:
            cod_mp = ing.get("cod_mp_sistema", "").strip()
            if not cod_mp or cod_mp.startswith("#"):
                continue
            gramaje = _safe_float(ing.get("cantidad", 0))
            pct_aplicacion = _safe_float(ing.get("pct_aplicacion", 1) or 1) or 1.0
            merma_pct = _safe_float(ing.get("merma_pct", 0) or 0)
            consumo_total[cod_mp] += (
                cantidad * gramaje * pct_aplicacion * (1 + merma_pct)
            )

    dias_activos = len(fechas_activas)
    print(f"  {dias_activos} dias activos | {len(consumo_total)} MPs con consumo")

    papa = consumo_total.get("120", 0)
    print(
        f"  DEBUG papa(120): consumo_total={papa:.0f}g | "
        f"diario={round(papa/dias_activos,2) if dias_activos else 0}g"
    )

    print("  DEBUG desglose papa(120) por receta:")
    consumo_por_receta: dict[str, float] = defaultdict(float)
    for venta in todas_ventas:
        cod_r = (venta.get("cod_receta") or "").strip()
        variedad = (venta.get("variedad_smart_menu") or "").strip().upper()
        cantidad = _safe_float(venta.get("cantidad_vendida") or 0)
        if not cod_r or cantidad <= 0:
            continue

        ingredientes = (
            lookup_recetas.get((cod_r, variedad))
            or lookup_recetas.get((cod_r, ""))
            or []
        )
        for ing in ingredientes:
            if (ing.get("cod_mp_sistema", "") or "").strip() == "120":
                gramaje = _safe_float(ing.get("cantidad", 0))
                if gramaje:
                    consumo_por_receta[cod_r] += cantidad * gramaje

    for cod_r, consumo in sorted(
        consumo_por_receta.items(), key=lambda x: -x[1]
    ):
        print(f"    receta={cod_r} consumo={consumo:.0f}g")

    if dias_activos == 0:
        return {}

    return {cod_mp: round(total / dias_activos, 4) for cod_mp, total in consumo_total.items()}


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
    dias_cobertura = float(os.getenv("PAR_LEVEL_DIAS_COBERTURA", "7") or "7")

    print(f"Dias cobertura (par): {dias_cobertura}")

    print("[1] Cargando recetas (BD_RECETAS_DETALLE) y calculando consumos...")
    recetas = _cargar_recetas_detalle()
    consumo_diario = calcular_consumo_diario(recetas)
    print(f"  MPs con consumo diario calculado: {len(consumo_diario)}")

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
