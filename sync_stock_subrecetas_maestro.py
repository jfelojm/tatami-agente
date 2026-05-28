"""
Crea/actualiza filas pseudo-MP (SUB-xxx) en BD_MP_SISTEMA por subreceta activa.

Una fila por (cod_mp_sistema, cod_bodega) en BOD-001 y BOD-002 para descargo de ventas
y recalcular_stock_sheets / conteo.

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

from bodegas_config import BODEGAS_DESCARGO_VENTA, normalizar_cod_bodega
from descargo_subreceta import PREFIJO_PSEUDO_MP, cargar_metadata_subrecetas, pseudo_mp_cod
from numeros_sheets import parse_numero_sheets

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_MP = "BD_MP_SISTEMA"
BODEGAS_SEMI = sorted(BODEGAS_DESCARGO_VENTA)


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


def sync(*, dry_run: bool) -> dict[str, int]:
    subs = cargar_metadata_subrecetas()
    sh = _abrir_maestro()
    ws = sh.worksheet(SHEET_MP)
    headers, existentes, row_idx = _leer_bd_mp(ws)

    creadas = actualizadas = omitidas = 0
    updates: list[dict] = []
    nuevas: list[list] = []

    icod = headers.index("cod_mp_sistema") + 1
    inom = headers.index("nombre_mp") + 1
    iuni = headers.index("unidad_base") + 1
    icosto = headers.index("costo_unitario_ref") + 1

    for cod_sub, meta in sorted(subs.items(), key=lambda x: x[0]):
        cod_mp = pseudo_mp_cod(cod_sub)
        costo = parse_numero_sheets(meta.get("costo_unitario_estandar"), 0.0)
        nombre = meta.get("nombre_subreceta", "") or cod_sub
        unidad = meta.get("unidad", "gr") or "gr"

        for bod in BODEGAS_SEMI:
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

    # MPs SUB huérfanos (sub desactivada): no borrar; solo reportar
    subs_cod_mp = {pseudo_mp_cod(c) for c in subs}
    huerfanos = [
        k for k in existentes if k[0].upper().startswith(PREFIJO_PSEUDO_MP) and k[0] not in subs_cod_mp
    ]
    if huerfanos:
        print(f"  INFO: {len(huerfanos)} filas {PREFIJO_PSEUDO_MP}* sin sub activa (no se modifican)")

    print(f"  Subrecetas activas: {len(subs)}")
    print(f"  Filas a crear: {creadas} | a actualizar nombre/unidad/costo: {actualizadas}")

    if dry_run:
        print("  [DRY-RUN] sin escritura en Sheets")
        return {"subs": len(subs), "creadas": creadas, "actualizadas": actualizadas}

    if nuevas:
        ws.append_rows(nuevas, value_input_option=ValueInputOption.user_entered)
    if updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)

    return {"subs": len(subs), "creadas": creadas, "actualizadas": actualizadas}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)
    dry = args.dry_run or not args.produccion
    print(f"sync_stock_subrecetas_maestro — {'DRY-RUN' if dry else 'PRODUCCION'}")
    stats = sync(dry_run=dry)
    print(f"  OK: {stats}")
    if not dry:
        print("  Siguiente: DESCARGO_SUBRECETAS=1 en .env y descargo_inventario.py")


if __name__ == "__main__":
    main()
