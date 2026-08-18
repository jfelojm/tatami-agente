"""
Implementa unificación lomo fino: MP 591 + merma_pct_ingreso 15% en catálogo.

Acciones:
  1. Columna merma_pct_ingreso en BD_ITEMS_PROV (si falta)
  2. Filas MP 591 en BD_MP_SISTEMA (BOD-001, BOD-005)
  3. Ítems ItalDeli 000339 y Pacheco 01008009 -> MP 591, merma 0.15
  4. BD_RECETAS_DETALLE: 047/552 -> 591
  5. Marca MPs legacy 047/552 activa=NO en BD_MP_SISTEMA

Uso:
  python implementar_lomo_fino_591.py --dry-run
  python implementar_lomo_fino_591.py --produccion
"""

from __future__ import annotations

import argparse
import copy
import os
import time

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from google_credentials import google_credentials
from recetas_detalle import cargar_bd_recetas_detalle, es_linea_mp
from staging_common import find_header_row, open_master

load_dotenv(override=True)

MP_NUEVO = "591"
NOMBRE_MP = "LOMO FINO DE RES"
MERMA_INGRESO = "0.15"
ITEMS_PROV = (
    ("068", "000339"),
    ("006", "01008009"),
)
MPS_LEGACY = ("047", "552")
BODEGAS = ("BOD-001", "BOD-005")

RECETAS_CAMBIO = {
    ("146", ""): 180,
    ("136", ""): 150,
    ("6", ""): 150,
    ("7", ""): 90,
    ("5", "BEEF CRUNCH (RES)"): 90,
    ("5", "LOMO PONZU"): 90,
    ("8", "LOMO"): 100,
    ("11", "LOMO"): 100,
    ("12", "LOMO"): 100,
}


def _invalidate_caches() -> None:
    import procesar_facturas_drive as pfd

    pfd._items_prov_cache = None
    pfd._invalidar_cache_layout_precio_items_prov()


def _ensure_column(ws, vals: list[list[str]], hi: int, col_name: str) -> int:
    headers = [(c or "").strip() for c in vals[hi]]
    if col_name in headers:
        return headers.index(col_name)
    new_col = len(headers)
    cell = rowcol_to_a1(hi + 1, new_col + 1)
    ws.update(
        range_name=cell,
        values=[[col_name]],
        value_input_option=ValueInputOption.user_entered,
    )
    print(f"  Columna nueva {col_name} en {ws.title} col {new_col + 1}")
    time.sleep(0.5)
    return new_col


def _plantilla_fila_mp(vals: list[list[str]], hi: int, cod_ref: str) -> dict | None:
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_mp_sistema")
    for row in vals[hi + 1 :]:
        if (row[icod] if icod < len(row) else "").strip() == cod_ref:
            return {headers[j]: (row[j] if j < len(row) else "") for j in range(len(headers))}
    return None


def configurar_mp_sistema(sh, *, dry_run: bool) -> int:
    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_mp_sistema")
    if hi is None:
        print("ERROR: header BD_MP_SISTEMA")
        return 1
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_mp_sistema")
    ibod = headers.index("cod_bodega")
    iact = headers.index("activa") if "activa" in headers else None
    plantilla = _plantilla_fila_mp(vals, hi, "047") or _plantilla_fila_mp(vals, hi, "552")
    if not plantilla:
        print("ERROR: no hay fila plantilla 047/552")
        return 1

    existentes = {
        (
            (row[icod] if icod < len(row) else "").strip(),
            (row[ibod] if ibod < len(row) else "").strip(),
        )
        for row in vals[hi + 1 :]
    }
    updates: list[dict] = []
    nuevas_filas: list[list[str]] = []

    for bod in BODEGAS:
        if (MP_NUEVO, bod) in existentes:
            print(f"  MP {MP_NUEVO} @ {bod} ya existe")
            continue
        fila = copy.deepcopy(plantilla)
        fila["cod_mp_sistema"] = MP_NUEVO
        fila["nombre_mp"] = NOMBRE_MP
        fila["cod_bodega"] = bod
        fila["nombre_bodega"] = "Cocina" if bod == "BOD-001" else "Bodega externa"
        fila["stock_actual"] = "0"
        fila["costo_unitario_ref"] = ""
        fila["par_level"] = ""
        fila["consumo_diario_calculado"] = ""
        if iact is not None:
            fila["activa"] = "SI"
        nuevas_filas.append([fila.get(h, "") for h in headers])
        print(f"  + MP {MP_NUEVO} @ {bod}")

    for cod_legacy in MPS_LEGACY:
        for r_idx, row in enumerate(vals[hi + 1 :], start=hi + 2):
            cod = (row[icod] if icod < len(row) else "").strip()
            if cod != cod_legacy:
                continue
            if iact is None:
                continue
            old = (row[iact] if iact < len(row) else "").strip()
            if old.upper() == "NO":
                continue
            cell = rowcol_to_a1(r_idx, iact + 1)
            updates.append({"range": cell, "values": [["NO"]]})
            print(f"  MP {cod_legacy} fila {r_idx}: activa -> NO")

    if dry_run:
        print(f"[DRY-RUN] {len(nuevas_filas)} filas nuevas, {len(updates)} desactivaciones")
        return 0
    if nuevas_filas:
        last_row = hi + 1
        for i, row_vals in enumerate(vals[hi + 1 :], start=hi + 2):
            if any((c or "").strip() for c in row_vals):
                last_row = i
        start = last_row + 1
        for offset, row_out in enumerate(nuevas_filas):
            ws.update(
                range_name=f"A{start + offset}",
                values=[row_out],
                value_input_option=ValueInputOption.user_entered,
            )
        time.sleep(0.5)
    if updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    return 0


def configurar_items_prov(sh, *, dry_run: bool) -> int:
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_item_prov")
    if hi is None:
        print("ERROR: header BD_ITEMS_PROV")
        return 1
    headers = [(c or "").strip() for c in vals[hi]]
    if "merma_pct_ingreso" not in headers:
        if dry_run:
            print("  [DRY-RUN] crearia columna merma_pct_ingreso")
        else:
            _ensure_column(ws, vals, hi, "merma_pct_ingreso")
            vals = ws.get_all_values()
            hi = find_header_row(vals, "cod_item_prov")
            headers = [(c or "").strip() for c in vals[hi]]
    imerma = headers.index("merma_pct_ingreso") if "merma_pct_ingreso" in headers else None
    icodp = headers.index("cod_proveedor")
    iitem = headers.index("cod_item_prov")
    imp = headers.index("cod_mp_sistema")
    inom = headers.index("nombre_mp")

    updates = []
    for r_idx, row in enumerate(vals[hi + 1 :], start=hi + 2):
        prov = (row[icodp] if icodp < len(row) else "").strip()
        item = (row[iitem] if iitem < len(row) else "").strip()
        if (prov, item) not in ITEMS_PROV:
            continue
        cells = {
            imp: MP_NUEVO,
            inom: NOMBRE_MP,
        }
        if imerma is not None:
            cells[imerma] = MERMA_INGRESO
        for col, val in cells.items():
            updates.append(
                {"range": rowcol_to_a1(r_idx, col + 1), "values": [[val]]}
            )
        print(f"  item {prov}/{item} -> MP {MP_NUEVO} merma {MERMA_INGRESO}")

    if dry_run:
        print(f"[DRY-RUN] {len(updates)} celdas BD_ITEMS_PROV")
        return 0
    if updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    return 0


def migrar_recetas(sh, *, dry_run: bool) -> int:
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_receta")
    if hi is None:
        print("ERROR: header BD_RECETAS_DETALLE")
        return 1
    headers = [(c or "").strip() for c in vals[hi]]
    icod_r = headers.index("cod_receta")
    ivar = headers.index("variedad_smart_menu")
    imp = headers.index("cod_mp_sistema")
    inom = headers.index("nombre_mp")

    updates = []
    for r_idx, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod_mp = (row[imp] if imp < len(row) else "").strip()
        if cod_mp not in MPS_LEGACY:
            continue
        cod_r = (row[icod_r] if icod_r < len(row) else "").strip()
        var = (row[ivar] if ivar < len(row) else "").strip()
        key = (cod_r, var)
        if key not in RECETAS_CAMBIO and cod_mp in MPS_LEGACY:
            print(f"  WARN linea extra {cod_r}/{var} MP{cod_mp} fila {r_idx}")
        updates.append({"range": rowcol_to_a1(r_idx, imp + 1), "values": [[MP_NUEVO]]})
        updates.append({"range": rowcol_to_a1(r_idx, inom + 1), "values": [[NOMBRE_MP]]})
        print(f"  receta {cod_r} {var or '-'}: MP{cod_mp} -> {MP_NUEVO}")

    if dry_run:
        print(f"[DRY-RUN] {len(updates)} celdas recetas")
        return 0
    if updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    dry = args.dry_run
    sh = open_master()
    print("=== LOMO FINO MP 591 ===")
    print(f"Modo: {'DRY-RUN' if dry else 'PRODUCCION'}")

    rc = 0
    rc |= configurar_mp_sistema(sh, dry_run=dry)
    rc |= configurar_items_prov(sh, dry_run=dry)
    rc |= migrar_recetas(sh, dry_run=dry)

    if not dry:
        _invalidate_caches()
        print("Caches invalidadas.")
    print("Listo.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
