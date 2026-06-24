"""
Espeja MPs de cocina en BOD-001 y BOD-005 para inventario físico.

Para cada MP en la unión cocina (001 ∪ 005, sin batches barra 051-054),
crea la fila faltante en la otra bodega copiando metadatos y calculando
stock/par/costo para esa bodega.

Uso:
  python sync_mp_cocina_bodegas.py --dry-run
  python sync_mp_cocina_bodegas.py --produccion
"""

from __future__ import annotations

import argparse
import os

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1

from auditar_mp_cocina_bodegas import BODEGAS_COCINA, SHEET, _leer_bd_mp, auditar
from bodegas_config import BODEGAS, normalizar_cod_bodega
from calcular_par_levels import consumo_diario_por_cod_mp
from dias_cobertura_par import resolver_dias_cobertura_mp
from google_credentials import google_credentials
from numeros_sheets import parse_numero_sheets
from recalcular_stock_sheets import _clave_stock, build_stock_calculado

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_COPIAR_COLS = (
    "nombre_mp",
    "categoria",
    "unidad_base",
    "tipo_control",
    "dias_seguridad",
    "activa",
)


def _metricas_bodega_cached(
    cod_mp: str,
    bod: str,
    costo_ref: float,
    stock_map: dict,
    consumo_map: dict,
) -> dict[str, float]:
    cd = float(consumo_map.get(cod_mp, 0.0))
    dias, _ = resolver_dias_cobertura_mp(cod_mp)
    par = round(cd * dias, 4) if cd > 0 else 0.0
    stock = round(stock_map.get(_clave_stock(cod_mp, bod), 0.0), 4)
    return {
        "costo_unitario_ref": costo_ref,
        "stock_actual": stock,
        "par_level": par,
        "consumo_diario_calculado": cd,
    }


def _fila_espejo_cached(
    headers: list[str],
    origen: dict,
    bod_dest: str,
    stock_map: dict,
    consumo_map: dict,
) -> list:
    bod_dest = normalizar_cod_bodega(bod_dest)
    info = BODEGAS.get(bod_dest)
    nombre_bod = info.nombre if info else bod_dest
    cod_mp = (origen.get("cod_mp_sistema") or "").strip()
    costo = parse_numero_sheets(origen.get("costo_unitario_ref"), 0.0)
    metricas = _metricas_bodega_cached(cod_mp, bod_dest, costo, stock_map, consumo_map)
    valores: dict = {
        "cod_mp_sistema": cod_mp,
        "cod_bodega": bod_dest,
        "nombre_bodega": nombre_bod,
        **{c: origen.get(c, "") for c in _COPIAR_COLS},
        **metricas,
    }
    return [valores.get(h, "") for h in headers]


def _fila_espejo(headers: list[str], origen: dict, bod_dest: str) -> list:
    stock_map = build_stock_calculado()
    consumo_map = consumo_diario_por_cod_mp(verbose=False)
    return _fila_espejo_cached(headers, origen, bod_dest, stock_map, consumo_map)


def sync(*, dry_run: bool) -> dict[str, int]:
    creds = google_credentials(SCOPES)
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet(SHEET)
    headers, filas = _leer_bd_mp(ws)
    r = auditar(filas)

    print("  Calculando stock y consumo (una vez)...")
    stock_map = build_stock_calculado()
    consumo_map = consumo_diario_por_cod_mp(verbose=False)

    nuevas: list[list] = []
    for cod in r["faltan_005"]:
        origen = filas.get((cod, "BOD-001"))
        if origen:
            nuevas.append(_fila_espejo_cached(headers, origen, "BOD-005", stock_map, consumo_map))
    for cod in r["faltan_001"]:
        origen = filas.get((cod, "BOD-005"))
        if origen:
            nuevas.append(_fila_espejo_cached(headers, origen, "BOD-001", stock_map, consumo_map))

    print(f"sync_mp_cocina_bodegas — {'DRY-RUN' if dry_run else 'PRODUCCION'}")
    print(f"  MPs en ambas (antes): {r['en_ambas']}")
    print(f"  Filas a crear: {len(nuevas)}")
    for cod in r["faltan_005"]:
        print(f"    + {cod} -> BOD-005")
    for cod in r["faltan_001"]:
        print(f"    + {cod} -> BOD-001")

    if dry_run or not nuevas:
        return {"creadas": len(nuevas)}

    ws.append_rows(nuevas, value_input_option=ValueInputOption.user_entered)
    print(f"  OK: {len(nuevas)} filas agregadas a {SHEET}")
    return {"creadas": len(nuevas)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    dry = not args.produccion or args.dry_run
    sync(dry_run=dry)


if __name__ == "__main__":
    main()
