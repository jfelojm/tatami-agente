"""
Promueve filas APROBADO de STAGING_RECETAS (v2) → BD_RECETAS_DETALLE en el maestro.

Solo filas con estado APROBADO. Tras insertar marca PROMOVIDO en staging.

Uso:
  python promover_staging_recetas.py --dry-run
  python promover_staging_recetas.py --produccion
  python promover_staging_recetas.py --hoja STAGING_RECETAS_V2 --produccion
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from bodegas_config import bodega_permite_descargo_venta, normalizar_cod_bodega
from recetas_detalle import (
    HEADERS_BD_RECETAS_DETALLE,
    cargar_bd_recetas_detalle,
    clave_plato,
    es_linea_mp,
    es_linea_subreceta,
    norm_cod_receta,
)
from staging_common import (
    leer_filas_staging,
    norm_cod,
    open_master,
    open_staging,
    pct_a_decimal,
)

load_dotenv(override=True)

DEFAULT_HOJA = "STAGING_RECETAS"


def _clave_linea(cod_receta: str, variedad: str, tipo: str, cod_mp: str, cod_sub: str) -> str:
    v = (variedad or "").strip().upper()
    if tipo == "SUB":
        return f"{norm_cod_receta(cod_receta)}|{v}|SUB:{norm_cod(cod_sub)}"
    return f"{norm_cod_receta(cod_receta)}|{v}|MP:{norm_cod(cod_mp)}"


def _indices_existentes(lineas: list[dict]) -> set[str]:
    out: set[str] = set()
    for ln in lineas:
        cod = (ln.get("cod_receta") or "").strip()
        var = (ln.get("variedad_smart_menu") or "").strip().upper()
        if es_linea_subreceta(ln):
            out.add(_clave_linea(cod, var, "SUB", "", ln.get("cod_subreceta") or ""))
        elif es_linea_mp(ln):
            out.add(_clave_linea(cod, var, "MP", ln.get("cod_mp_sistema") or "", ""))
    return out


def _validar_fila(d: dict) -> tuple[bool, str, dict]:
    estado = (d.get("estado") or "").strip().upper()
    if estado != "APROBADO":
        return False, "no_aprobado", {}

    cod_receta = (d.get("cod_receta") or "").strip()
    if not cod_receta:
        return False, "sin_cod_receta", {}

    tipo = (d.get("tipo_linea") or "").strip().upper()
    if tipo not in ("MP", "SUB"):
        return False, "tipo_linea_invalido", {}

    from numeros_sheets import parse_numero_sheets

    cant = parse_numero_sheets(d.get("cantidad"), 0)
    if cant <= 0:
        return False, "cantidad_invalida", {}

    cod_mp = (d.get("cod_mp_sistema") or "").strip()
    cod_sub = (d.get("cod_subreceta") or "").strip()
    if tipo == "MP":
        if not cod_mp or cod_sub:
            return False, "mp_requiere_solo_cod_mp", {}
    else:
        if not cod_sub or cod_mp:
            return False, "sub_requiere_solo_cod_sub", {}

    bod = normalizar_cod_bodega(d.get("cod_bodega"))
    if not bodega_permite_descargo_venta(bod):
        return False, f"bodega_no_descargo:{bod}", {}

    merma = pct_a_decimal(d.get("merma_pct"), 0.0)
    if merma > 1:
        merma = merma / 100.0  # si dejaron 5 = 5%
    pct = pct_a_decimal(d.get("pct_aplicacion"), 1.0)
    opc = (d.get("es_opcional") or "NO").strip().upper() or "NO"

    row_out = {
        "nombre_receta": (d.get("nombre_receta") or "").strip(),
        "cod_receta": cod_receta,
        "variedad_smart_menu": (d.get("variedad_smart_menu") or "").strip(),
        "nombre_subreceta": (d.get("nombre_subreceta") or "").strip() if tipo == "SUB" else "",
        "cod_subreceta": cod_sub if tipo == "SUB" else "",
        "nombre_mp": (d.get("nombre_mp") or "").strip() if tipo == "MP" else "",
        "cod_mp_sistema": cod_mp if tipo == "MP" else "",
        "cantidad": str(cant),
        "unidad_base": (d.get("unidad_base") or "").strip(),
        "cod_bodega": bod,
        "merma_pct": str(merma),
        "es_opcional": opc if opc in ("SI", "NO") else "NO",
        "pct_aplicacion": str(pct if pct <= 1 else pct / 100),
    }
    return True, "", row_out


def _fila_a_lista(row: dict) -> list[str]:
    return [row.get(h, "") for h in HEADERS_BD_RECETAS_DETALLE]


def run(*, hoja: str, dry_run: bool) -> int:
    st = open_staging()
    ma = open_master()
    try:
        ws = st.worksheet(hoja)
    except Exception:
        print(f"ERROR: no existe hoja {hoja} en staging. Ejecuta setup_staging_recetas_v2.py")
        return 2

    headers, filas = leer_filas_staging(ws, marker="cod_receta", min_cols=("tipo_linea", "estado"))
    if not headers:
        print("ERROR: cabecera inválida (cod_receta, tipo_linea, estado)")
        return 2

    try:
        idx_estado = headers.index("estado")
    except ValueError:
        print("ERROR: falta columna estado")
        return 2

    existentes = _indices_existentes(cargar_bd_recetas_detalle(ma))
    ws_det = ma.worksheet("BD_RECETAS_DETALLE")

    ok = dup = err = skip = 0
    errores: list[str] = []

    for sheet_row, d in filas:
        valido, motivo, row_out = _validar_fila(d)
        if not valido:
            if motivo == "no_aprobado":
                skip += 1
            else:
                err += 1
                errores.append(f"fila {sheet_row}: {motivo}")
            continue

        tipo = (d.get("tipo_linea") or "").strip().upper()
        clave = _clave_linea(
            row_out["cod_receta"],
            row_out["variedad_smart_menu"],
            tipo,
            row_out.get("cod_mp_sistema") or "",
            row_out.get("cod_subreceta") or "",
        )
        if clave in existentes:
            dup += 1
            print(f"  SKIP fila {sheet_row}: duplicado en maestro ({clave})")
            if not dry_run:
                ws.update(
                    rowcol_to_a1(sheet_row, idx_estado + 1),
                    [["PROMOVIDO"]],
                    value_input_option=ValueInputOption.user_entered,
                )
            continue

        if dry_run:
            print(
                f"  [DRY] fila {sheet_row}: {tipo} plato {clave_plato(row_out['cod_receta'], row_out['variedad_smart_menu'])} "
                f"cant={row_out['cantidad']} {row_out['unidad_base']}"
            )
            ok += 1
            continue

        ws_det.append_row(
            _fila_a_lista(row_out),
            value_input_option=ValueInputOption.user_entered,
        )
        existentes.add(clave)
        ws.update(
            rowcol_to_a1(sheet_row, idx_estado + 1),
            [["PROMOVIDO"]],
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"  OK fila {sheet_row} → BD_RECETAS_DETALLE ({tipo})")
        ok += 1

    print(
        f"\nResumen: promovidas={ok} duplicadas={dup} omitidas_no_aprobado={skip} "
        f"errores={err} dry_run={dry_run}"
    )
    if errores:
        print("Detalle errores:")
        for e in errores[:30]:
            print(f"  {e}")
        if len(errores) > 30:
            print(f"  ... +{len(errores) - 30} más")

    if ok and not dry_run:
        print("  Sugerencia: python calcular_costo_recetas.py --produccion")

    return 0 if err == 0 else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hoja", default=DEFAULT_HOJA)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)
    sys.exit(run(hoja=args.hoja, dry_run=args.dry_run or not args.produccion))


if __name__ == "__main__":
    main()
