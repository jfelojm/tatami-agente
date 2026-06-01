import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

from config_sheets import cfg
from costo_mp_canonico import norm_mp
from descargo_subreceta import calcular_consumo_sub, norm_cod_sub
from recetas_detalle import es_linea_mp, es_linea_subreceta, norm_cod_receta
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
    es_linea_mp_detalle,
    es_linea_subreceta_hijo,
    orden_produccion,
)
from ventas_smartmenu import estado_documento_excluye_neto_operativo

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
    """Misma lógica que descargo_inventario.cargar_recetas: cabecera por fila con cod_receta."""
    sh = _get_sheet()
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    values = ws.get_all_values()
    header_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_receta" for c in row):
            header_idx = i
            break
    if header_idx is None:
        print("  ERROR: BD_RECETAS_DETALLE sin cabecera cod_receta")
        return []
    headers = [(c or "").strip() for c in values[header_idx]]
    if "cod_receta" not in headers:
        print("  ERROR: BD_RECETAS_DETALLE sin columna cod_receta")
        return []
    out: list[dict] = []
    for j in range(header_idx + 1, len(values)):
        row = values[j]
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        r = {
            headers[k]: (row[k] if k < len(row) else "").strip()
            for k in range(min(len(headers), len(row)))
            if headers[k]
        }
        cod_mp = (r.get("cod_mp_sistema") or "").strip()
        cod_sub = (r.get("cod_subreceta") or "").strip()
        if not cod_mp and not cod_sub:
            continue
        if cod_mp.startswith("#"):
            continue
        out.append(r)
    return out


def _cargar_mp_por_unidad_subreceta() -> dict[str, dict[str, float]]:
    """
    cod_sub (normalizado) -> cod_mp -> cantidad MP por 1 unidad de salida de la subreceta.
    Expande MPs directas y subrecetas hijas (anidadas), en orden topológico.
    """
    cab = cargar_bd_subrecetas()
    por_padre = agrupar_detalle_por_padre(cargar_bd_subrecetas_detalle())
    cab_all = {c: cab[c] for c in por_padre if c in cab}

    rend_por_sub: dict[str, float] = {}
    for cod, info in cab.items():
        nk = norm_cod_sub(cod)
        if nk:
            rend_por_sub[nk] = _safe_float(info.get("rendimiento_estandar"))

    try:
        orden = orden_produccion(cab_all, por_padre)
    except ValueError as e:
        print(f"  WARN orden subrecetas: {e}")
        orden = []
    restantes = sorted(set(por_padre) - set(orden))
    orden = orden + restantes

    out: dict[str, dict[str, float]] = {}
    for cod_sub in orden:
        nk = norm_cod_sub(cod_sub)
        if not nk:
            continue
        rend = rend_por_sub.get(nk, 0.0)
        if rend <= 0:
            continue
        mp_map: dict[str, float] = defaultdict(float)
        for ln in por_padre.get(cod_sub, []):
            cant = _safe_float(ln.get("cantidad"))
            if cant <= 0:
                continue
            if es_linea_mp_detalle(ln):
                mp = norm_mp(ln.get("cod_mp_sistema") or "")
                if not mp:
                    continue
                merma = _safe_float(ln.get("merma_pct"))
                mp_map[mp] += (cant / rend) * (1.0 + merma)
            elif es_linea_subreceta_hijo(ln):
                hijo = norm_cod_sub(ln.get("cod_subreceta_hijo") or "")
                hijo_map = out.get(hijo, {})
                if not hijo_map:
                    continue
                factor = cant / rend
                for mp, per_unit in hijo_map.items():
                    mp_map[mp] += factor * per_unit
        if mp_map:
            out[nk] = dict(mp_map)
    return out


def calcular_consumo_diario(recetas: list[dict]) -> dict[str, float]:
    print("  Leyendo hist_ventas desde Supabase...")

    sel = "cod_receta,variedad_smart_menu,cantidad_vendida,fecha"
    try:
        supabase.table("hist_ventas").select("estado_documento").limit(1).execute()
        sel += ",estado_documento"
    except Exception:
        pass

    todas_ventas: list[dict] = []
    offset = 0
    while True:
        r = (
            supabase.table("hist_ventas")
            .select(sel)
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

    mp_sub_unit = _cargar_mp_por_unidad_subreceta()
    print(f"  {len(mp_sub_unit)} subrecetas con MPs expandidas")

    # Key = (cod_receta normalizado, variedad normalizada) para empatar con hist_ventas,
    # que suele venir con ceros a la izquierda (ej. "007") mientras BD_RECETAS_DETALLE usa "7".
    lookup_recetas: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ing in recetas:
        cod_r = norm_cod_receta(ing.get("cod_receta", ""))
        var = ing.get("variedad_smart_menu", "").strip().upper()
        lookup_recetas[(cod_r, var)].append(ing)

    consumo_total: dict[str, float] = defaultdict(float)
    fechas_activas: set[str] = set()

    for venta in todas_ventas:
        if estado_documento_excluye_neto_operativo(venta.get("estado_documento")):
            continue
        cod_r = norm_cod_receta(venta.get("cod_receta") or "")
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
            if es_linea_mp(ing):
                cod_mp = norm_mp(ing.get("cod_mp_sistema", ""))
                if not cod_mp or cod_mp.startswith("#"):
                    continue
                gramaje = _safe_float(ing.get("cantidad", 0))
                pct_aplicacion = _safe_float(ing.get("pct_aplicacion", 1) or 1) or 1.0
                merma_pct = _safe_float(ing.get("merma_pct", 0) or 0)
                consumo_total[cod_mp] += (
                    cantidad * gramaje * pct_aplicacion * (1 + merma_pct)
                )
            elif es_linea_subreceta(ing):
                sub = norm_cod_sub(ing.get("cod_subreceta") or "")
                mp_map = mp_sub_unit.get(sub, {})
                if not mp_map:
                    continue
                units_sub = calcular_consumo_sub(ing, cantidad)
                if units_sub <= 0:
                    continue
                for mp, per_unit in mp_map.items():
                    consumo_total[mp] += units_sub * per_unit

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
        if estado_documento_excluye_neto_operativo(venta.get("estado_documento")):
            continue
        cod_r = norm_cod_receta(venta.get("cod_receta") or "")
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
            if norm_mp(ing.get("cod_mp_sistema", "")) == "120":
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

    Nota: par_level y consumo_diario_calculado se escriben en todas las filas del MP.
    Para comparar stock vs PAR (órdenes de compra, alertas), usar inventario_stock_mp:
    stock_total = suma de stock_actual en todas las bodegas activas del MP.
    """
    dias_cobertura = float(
        cfg("par_level_dias_cobertura", os.getenv("PAR_LEVEL_DIAS_COBERTURA", "7") or "7")
    )

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

    # PAR y consumo son globales por cod_mp (no por bodega)
    par_por_mp: dict[str, float] = {}
    consumo_por_mp: dict[str, float] = {}
    for cod, cd in consumo_diario.items():
        consumo_por_mp[cod] = cd
        par_por_mp[cod] = round(cd * dias_cobertura, 4) if cd > 0 else 0.0

    print("[3] Calculando par levels (global por cod_mp)...")
    for i, row in enumerate(data_rows):
        if not any(c.strip() for c in row):
            continue
        cod = norm_mp(row[col_cod - 1].strip() if len(row) >= col_cod else "")
        if not cod:
            continue
        cd = float(consumo_por_mp.get(cod, 0.0))
        par_level = float(par_por_mp.get(cod, 0.0))
        row_1based = header_row_idx + i + 2

        if not dry_run:
            updates.append(
                {"range": rowcol_to_a1(row_1based, col_par), "values": [[par_level]]}
            )
            updates.append(
                {"range": rowcol_to_a1(row_1based, col_consumo), "values": [[cd]]}
            )

    for cod in sorted(consumo_por_mp.keys()):
        if consumo_por_mp[cod] > 0:
            print(
                f"  {cod}: consumo_diario={round(consumo_por_mp[cod], 6)} "
                f"par={par_por_mp[cod]} (todas las filas MP×bodega)"
            )

    print(f"[4] {'DRY RUN - no escribe' if dry_run else 'Escribiendo'}: {len(updates)} updates")
    if dry_run or not updates:
        return

    for i in range(0, len(updates), 50):
        ws.batch_update(
            updates[i : i + 50],
            value_input_option=ValueInputOption.user_entered,
        )
        time.sleep(1)

    print("Listo.")


if __name__ == "__main__":
    import sys
    from datetime import date

    DRY_RUN = "--dry-run" in sys.argv
    calcular_par_levels(dry_run=DRY_RUN)
