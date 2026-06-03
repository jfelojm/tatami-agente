"""
Crea/actualiza filas pseudo-MP (SUB-xxx) en BD_MP_SISTEMA por subreceta activa.

Una fila por (cod_mp_sistema, cod_bodega) solo en bodegas del detalle de producción
(BD_SUBRECETAS_DETALLE), típicamente BOD-002 barra o BOD-001 cocina.
Elimina filas SUB-* en bodegas que ya no aplican.
Stock inicial 0 si la fila es nueva (para ver entradas/descargos en maestro).

Uso:
  python sync_stock_subrecetas_maestro.py --dry-run
  python sync_stock_subrecetas_maestro.py --produccion
"""

from __future__ import annotations

import argparse
import os
import sys

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

from bodegas_config import normalizar_cod_bodega
from codigos_subreceta import cod_sub_canonico
from descargo_subreceta import PREFIJO_PSEUDO_MP, cargar_metadata_subrecetas, pseudo_mp_cod
from numeros_sheets import parse_numero_sheets
from subrecetas_bodegas_stock import mapa_bodegas_todas_subs
from subrecetas_detalle import agrupar_detalle_por_padre, cargar_bd_subrecetas_detalle

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_MP = "BD_MP_SISTEMA"
def _abrir_maestro():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _leer_bd_mp(ws) -> tuple[list[str], dict[tuple[str, str], dict], dict[tuple[str, str], int]]:
    """headers, (cod_mp, bod) -> row dict, (cod_mp, bod) -> sheet row 1-based."""
    values = ws.get_all_values()
    hi = next(
        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if hi is None:
        raise RuntimeError("BD_MP_SISTEMA sin columna cod_mp_sistema")
    headers = [(c or "").strip() for c in values[hi]]
    filas: dict[tuple[str, str], dict] = {}
    row_idx: dict[tuple[str, str], int] = {}
    for i, row in enumerate(values[hi + 1 :], start=hi + 2):
        if not any((c or "").strip() for c in row):
            continue
        d = {
            headers[j]: (row[j] if j < len(row) else "").strip()
            for j in range(min(len(headers), len(row)))
            if headers[j]
        }
        cod = (d.get("cod_mp_sistema") or "").strip()
        bod = normalizar_cod_bodega(d.get("cod_bodega"))
        if cod and bod:
            filas[(cod, bod)] = d
            row_idx[(cod, bod)] = i
    return headers, filas, row_idx


def _fila_nueva(headers: list[str], *, cod_mp: str, bod: str, meta: dict) -> list:
    """Construye fila alineada a headers; columnas desconocidas vacías."""
    nombre = meta.get("nombre_subreceta", "") or cod_mp
    unidad = meta.get("unidad", "gr") or "gr"
    costo = parse_numero_sheets(meta.get("costo_unitario_estandar"), 0.0)
    valores = {
        "cod_mp_sistema": cod_mp,
        "nombre_mp": nombre,
        "unidad_base": unidad,
        "cod_bodega": bod,
        "stock_actual": "0",
        "costo_unitario_ref": str(costo) if costo else "0",
        "par_level": "0",
        "consumo_diario_calculado": "0",
    }
    return [valores.get(h, "") for h in headers]


def sync(*, dry_run: bool, solo_cod: list[str] | None = None) -> dict[str, int]:
    sh = _abrir_maestro()
    subs = cargar_metadata_subrecetas()
    por_padre = agrupar_detalle_por_padre(cargar_bd_subrecetas_detalle(sh))
    bodegas_map = mapa_bodegas_todas_subs(subs, por_padre=por_padre, sh=sh)

    ws = sh.worksheet(SHEET_MP)
    headers, existentes, row_idx = _leer_bd_mp(ws)

    creadas = actualizadas = sin_bodega = eliminadas = 0
    updates: list[dict] = []
    nuevas: list[list] = []
    filas_borrar: list[int] = []

    icod = headers.index("cod_mp_sistema") + 1
    inom = headers.index("nombre_mp") + 1
    iuni = headers.index("unidad_base") + 1
    icosto = headers.index("costo_unitario_ref") + 1

    targets_canon = {cod_sub_canonico(c) for c in solo_cod} if solo_cod else None

    for cod_sub, meta in sorted(subs.items(), key=lambda x: x[0]):
        if targets_canon and cod_sub not in targets_canon:
            continue
        bods = sorted(bodegas_map.get(cod_sub) or ())
        if not bods:
            sin_bodega += 1
            continue

        cod_mp = pseudo_mp_cod(cod_sub)
        costo = parse_numero_sheets(meta.get("costo_unitario_estandar"), 0.0)
        nombre = meta.get("nombre_subreceta", "") or cod_sub
        unidad = meta.get("unidad", "gr") or "gr"

        for bod in bods:
            key = (cod_mp, bod)
            if key in existentes:
                row_n = row_idx[key]
                updates.append(
                    {
                        "range": gspread.utils.rowcol_to_a1(row_n, inom),
                        "values": [[nombre]],
                    }
                )
                updates.append(
                    {
                        "range": rowcol_to_a1(row_n, iuni),
                        "values": [[unidad]],
                    }
                )
                updates.append(
                    {
                        "range": rowcol_to_a1(row_n, icosto),
                        "values": [[costo]],
                    }
                )
                actualizadas += 1
            else:
                nuevas.append(_fila_nueva(headers, cod_mp=cod_mp, bod=bod, meta=meta))
                creadas += 1

        allowed = set(bods)
        for (cmp, bod), rn in list(row_idx.items()):
            if cmp != cod_mp or bod in allowed:
                continue
            if targets_canon and cod_sub not in targets_canon:
                continue
            filas_borrar.append(rn)
            eliminadas += 1

    # MPs SUB huérfanos (sub desactivada): no borrar; solo reportar
    subs_cod_mp = {pseudo_mp_cod(c) for c in subs}
    huerfanos = [
        k for k in existentes if k[0].upper().startswith(PREFIJO_PSEUDO_MP) and k[0] not in subs_cod_mp
    ]
    if huerfanos:
        print(f"  INFO: {len(huerfanos)} filas {PREFIJO_PSEUDO_MP}* sin sub activa (no se modifican)")

    print(f"  Subrecetas activas: {len(subs)}")
    print(f"  Sin bodega en detalle producción: {sin_bodega}")
    print(f"  Filas a crear: {creadas} | actualizar: {actualizadas} | eliminar bodega extra: {eliminadas}")
    if solo_cod:
        print(f"  Filtro cod: {solo_cod}")

    if dry_run:
        print("  [DRY-RUN] sin escritura en Sheets")
        return {
            "subs": len(subs),
            "creadas": creadas,
            "actualizadas": actualizadas,
            "eliminadas": eliminadas,
        }

    if nuevas:
        ws.append_rows(nuevas, value_input_option=ValueInputOption.user_entered)
    if updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    if filas_borrar:
        for rn in sorted(set(filas_borrar), reverse=True):
            ws.delete_rows(rn)

    return {
        "subs": len(subs),
        "creadas": creadas,
        "actualizadas": actualizadas,
        "eliminadas": eliminadas,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--cod", nargs="*", help="Solo estas subrecetas (ej. 051 052)")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)
    dry = args.dry_run or not args.produccion
    print(f"sync_stock_subrecetas_maestro — {'DRY-RUN' if dry else 'PRODUCCION'}")
    stats = sync(dry_run=dry, solo_cod=args.cod or None)
    print(f"  OK: {stats}")
    if not dry:
        print("  Siguiente: DESCARGO_SUBRECETAS=1 en .env y descargo_inventario.py")


if __name__ == "__main__":
    main()
