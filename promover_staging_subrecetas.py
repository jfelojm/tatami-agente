"""
Promueve STAGING_SUB_CAB / STAGING_SUB_DETALLE → BD_SUBRECETAS* en el maestro.

Uso:
  python promover_staging_subrecetas.py --dry-run
  python promover_staging_subrecetas.py --produccion
  python promover_staging_subrecetas.py --cab --produccion
  python promover_staging_subrecetas.py --detalle --produccion
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from bodegas_config import normalizar_cod_bodega
from staging_common import (
    leer_filas_staging,
    norm_cod,
    open_master,
    open_staging,
    refrescar_aux_subrecetas_en_staging,
)
from subrecetas_detalle import cargar_bd_subrecetas, cargar_bd_subrecetas_detalle

load_dotenv(override=True)

SHEET_CAB_ST = "STAGING_SUB_CAB"
SHEET_DET_ST = "STAGING_SUB_DETALLE"
SHEET_CAB = "BD_SUBRECETAS"
SHEET_DET = "BD_SUBRECETAS_DETALLE"

HEADERS_CAB = [
    "nombre_subreceta",
    "cod_subreceta",
    "rendimiento_estandar",
    "unidad",
    "activa",
    "notas",
]

HEADERS_DET = [
    "nombre_subreceta",
    "cod_subreceta_padre",
    "nombre_subreceta_hijo",
    "cod_subreceta_hijo",
    "nombre_mp",
    "cod_mp_sistema",
    "cantidad",
    "unidad_base",
    "cod_bodega",
    "merma_pct",
]


def _promover_cab(*, dry_run: bool) -> tuple[int, int, int]:
    st = open_staging()
    ma = open_master()
    ws = st.worksheet(SHEET_CAB_ST)
    headers, filas = leer_filas_staging(ws, marker="cod_subreceta", min_cols=("estado",))
    cab_exist = cargar_bd_subrecetas(ma)
    ws_out = ma.worksheet(SHEET_CAB)
    idx_est = headers.index("estado")

    ok = err = skip = 0
    for sheet_row, d in filas:
        if (d.get("estado") or "").upper() != "APROBADO":
            skip += 1
            continue
        cod = (d.get("cod_subreceta") or "").strip()
        if not cod:
            err += 1
            continue
        if norm_cod(cod) in {norm_cod(k) for k in cab_exist}:
            print(f"  SKIP cab fila {sheet_row}: cod {cod} ya existe")
            if not dry_run:
                ws.update(
                    rowcol_to_a1(sheet_row, idx_est + 1),
                    [["PROMOVIDO"]],
                    value_input_option=ValueInputOption.user_entered,
                )
            continue

        from numeros_sheets import parse_numero_sheets

        rend = parse_numero_sheets(d.get("rendimiento_estandar"), 0)
        if rend <= 0:
            err += 1
            print(f"  ERR fila {sheet_row}: rendimiento_estandar inválido")
            continue

        fila = [
            (d.get("nombre_subreceta") or "").strip(),
            cod,
            str(rend),
            (d.get("unidad") or "gr").strip(),
            (d.get("activa") or "SI").strip().upper() or "SI",
            (d.get("notas") or "").strip(),
        ]
        if dry_run:
            print(f"  [DRY] cab {cod} rend={rend}")
            ok += 1
            continue
        ws_out.append_row(fila, value_input_option=ValueInputOption.user_entered)
        ws.update(
            rowcol_to_a1(sheet_row, idx_est + 1),
            [["PROMOVIDO"]],
            value_input_option=ValueInputOption.user_entered,
        )
        cab_exist[cod] = d
        ok += 1
    if not dry_run and ok > 0:
        stats = refrescar_aux_subrecetas_en_staging()
        print(
            f"  Aux staging: {stats['subrecetas']} subs en listas, "
            f"{stats['padres_union']} nombres padre en dropdown"
        )
    print(f"Cabecera: ok={ok} skip={skip} err={err}")
    return ok, skip, err


def _claves_detalle(lineas: list[dict]) -> set[str]:
    out: set[str] = set()
    for ln in lineas:
        padre = norm_cod(ln.get("cod_subreceta_padre") or ln.get("cod_subreceta") or "")
        hijo = norm_cod(ln.get("cod_subreceta_hijo") or "")
        mp = norm_cod(ln.get("cod_mp_sistema") or "")
        if hijo:
            out.add(f"{padre}|SUB:{hijo}")
        elif mp:
            out.add(f"{padre}|MP:{mp}")
    return out


def _promover_detalle(*, dry_run: bool) -> tuple[int, int, int]:
    st = open_staging()
    ma = open_master()
    ws = st.worksheet(SHEET_DET_ST)
    headers, filas = leer_filas_staging(ws, marker="cod_subreceta_padre", min_cols=("tipo_linea", "estado"))
    cab = cargar_bd_subrecetas(ma)
    existentes = _claves_detalle(cargar_bd_subrecetas_detalle(ma))
    ws_out = ma.worksheet(SHEET_DET)
    idx_est = headers.index("estado")

    ok = err = skip = 0
    for sheet_row, d in filas:
        if (d.get("estado") or "").upper() != "APROBADO":
            skip += 1
            continue

        padre = (d.get("cod_subreceta_padre") or "").strip()
        if not padre:
            err += 1
            continue
        if norm_cod(padre) not in {norm_cod(k) for k in cab}:
            err += 1
            print(f"  ERR fila {sheet_row}: padre {padre} no está en BD_SUBRECETAS")
            continue

        tipo = (d.get("tipo_linea") or "").strip().upper()
        cod_mp = (d.get("cod_mp_sistema") or "").strip()
        cod_hijo = (d.get("cod_subreceta_hijo") or "").strip()
        if tipo == "MP":
            if not cod_mp or cod_hijo:
                err += 1
                continue
            clave = f"{norm_cod(padre)}|MP:{norm_cod(cod_mp)}"
        elif tipo == "SUB":
            if not cod_hijo or cod_mp:
                err += 1
                continue
            clave = f"{norm_cod(padre)}|SUB:{norm_cod(cod_hijo)}"
        else:
            err += 1
            continue

        from numeros_sheets import parse_numero_sheets

        cant = parse_numero_sheets(d.get("cantidad"), 0)
        if cant <= 0:
            err += 1
            continue

        if clave in existentes:
            print(f"  SKIP det fila {sheet_row}: duplicado {clave}")
            if not dry_run:
                ws.update(
                    rowcol_to_a1(sheet_row, idx_est + 1),
                    [["PROMOVIDO"]],
                    value_input_option=ValueInputOption.user_entered,
                )
            continue

        nombre_padre = (d.get("nombre_subreceta_padre") or cab.get(padre, {}).get("nombre_subreceta") or "").strip()
        fila = [
            nombre_padre,
            padre,
            (d.get("nombre_subreceta_hijo") or "").strip() if tipo == "SUB" else "",
            cod_hijo if tipo == "SUB" else "",
            (d.get("nombre_mp") or "").strip() if tipo == "MP" else "",
            cod_mp if tipo == "MP" else "",
            str(cant),
            (d.get("unidad_base") or "").strip(),
            normalizar_cod_bodega(d.get("cod_bodega")),
            (d.get("merma_pct") or "0").strip(),
        ]

        if dry_run:
            print(f"  [DRY] det {clave} cant={cant}")
            ok += 1
            continue

        ws_out.append_row(fila, value_input_option=ValueInputOption.user_entered)
        existentes.add(clave)
        ws.update(
            rowcol_to_a1(sheet_row, idx_est + 1),
            [["PROMOVIDO"]],
            value_input_option=ValueInputOption.user_entered,
        )
        ok += 1

    print(f"Detalle: ok={ok} skip={skip} err={err}")
    return ok, skip, err


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cab", action="store_true", help="Solo cabecera")
    p.add_argument("--detalle", action="store_true", help="Solo detalle")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument(
        "--refrescar-aux",
        action="store_true",
        help="Solo actualizar hojas aux en staging (dropdowns detalle)",
    )
    args = p.parse_args()
    if args.refrescar_aux:
        stats = refrescar_aux_subrecetas_en_staging()
        print(
            f"Aux staging: {stats['subrecetas']} subs, "
            f"{stats['padres_union']} padres en dropdown"
        )
        return
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)
    dry = args.dry_run or not args.produccion
    do_cab = args.cab or not args.detalle
    do_det = args.detalle or not args.cab

    if do_cab:
        _promover_cab(dry_run=dry)
    if do_det:
        _promover_detalle(dry_run=dry)
    if not dry:
        print("  Sugerencia: python calcular_costo_subrecetas.py --produccion")
        print("               python calcular_costo_recetas.py --produccion")


if __name__ == "__main__":
    main()
