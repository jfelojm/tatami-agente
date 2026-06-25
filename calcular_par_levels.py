import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

from costo_mp_canonico import norm_mp
from dias_cobertura_par import (
    dias_cobertura_global_default,
    invalidar_cache_dias_cobertura,
    resolver_dias_cobertura_mp,
)
from descargo_subreceta import calcular_consumo_sub, pseudo_mp_cod
from recetas_detalle import es_linea_mp, es_linea_subreceta, norm_cod_receta
from subreceta_consumo_mp import cargar_mp_por_unidad_subreceta, explotar_subreceta_a_mp
from ventas_smartmenu import estado_documento_excluye_neto_operativo
from google_credentials import google_credentials

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _get_sheet():
    creds = google_credentials(SCOPES)
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


def consumo_diario_por_cod_mp(
    recetas: list[dict] | None = None, *, verbose: bool = False
) -> dict[str, float]:
    """Consumo diario por cod_mp_sistema (MPs y pseudo-MP SUB-*). Sin escritura en Sheets."""
    if recetas is None:
        recetas = _cargar_recetas_detalle()
    return calcular_consumo_diario(recetas, verbose=verbose)


def calcular_consumo_diario(recetas: list[dict], *, verbose: bool = True) -> dict[str, float]:
    if verbose:
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

    if verbose:
        print(f"  {len(todas_ventas)} ventas cargadas")

    mp_sub_unit = cargar_mp_por_unidad_subreceta()
    if verbose:
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
                sub = (ing.get("cod_subreceta") or "").strip()
                units_sub = calcular_consumo_sub(ing, cantidad)
                if units_sub > 0:
                    pseudo = pseudo_mp_cod(sub)
                    if pseudo:
                        consumo_total[pseudo] += units_sub
                    for mp, qty in explotar_subreceta_a_mp(
                        sub, units_sub, mp_por_unidad=mp_sub_unit
                    ).items():
                        consumo_total[mp] += qty

    dias_activos = len(fechas_activas)
    if verbose:
        print(f"  {dias_activos} dias activos | {len(consumo_total)} cod_mp con consumo")

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
      - par_level = consumo_diario_calculado × días cobertura
        (BD_MP_SISTEMA.dias_cobertura_par → frecuencia proveedor → BD_CONFIG; SUB-* config)

    Consumo: MPs directas en carta + pseudo-MP SUB-* + MPs de la cadena de subrecetas
    (explotar_subreceta_a_mp / BD_SUBRECETAS_DETALLE anidado).

    Requiere columnas en BD_MP_SISTEMA:
      - cod_mp_sistema
      - dias_cobertura_par (editable; no se sobrescribe al recalcular)
      - stock_actual
      - par_level
      - consumo_diario_calculado

    Nota: par_level y consumo_diario_calculado se escriben en todas las filas del MP.
    dias_cobertura_par lo editas tú en BD_MP_SISTEMA (mismo valor lógico por cod_mp).
    Para comparar stock vs PAR (órdenes de compra, alertas), usar inventario_stock_mp:
    stock_total = suma de stock_actual en todas las bodegas activas del MP.
    """
    invalidar_cache_dias_cobertura()
    print(f"Dias cobertura default (BD_CONFIG): {dias_cobertura_global_default()}")

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
    dias_por_mp: dict[str, float] = {}
    fuente_por_mp: dict[str, str] = {}
    for cod, cd in consumo_diario.items():
        consumo_por_mp[cod] = cd
        dias, fuente = resolver_dias_cobertura_mp(cod)
        dias_por_mp[cod] = dias
        fuente_por_mp[cod] = fuente
        par_por_mp[cod] = round(cd * dias, 4) if cd > 0 else 0.0

    print("[3] Calculando par levels (dias: BD_MP_SISTEMA -> frecuencia_compra -> config)...")
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
                f"dias={dias_por_mp[cod]} ({fuente_por_mp[cod]}) "
                f"par={par_por_mp[cod]}"
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
