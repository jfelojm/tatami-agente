"""Valorización inventario por bodega: stock desde mov_inventario + costo BD_MP_SISTEMA."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from bodegas_config import normalizar_cod_bodega
from recalcular_stock_sheets import _clave_stock, build_stock_calculado
from google_credentials import google_credentials

load_dotenv(override=True)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _parse_num(v) -> float:
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, 0.0)


def cargar_maestro_mp() -> list[dict]:
    creds = google_credentials(SCOPES)
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()
    hi = next(
        (i for i, r in enumerate(values) if any((c or "").strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if hi is None:
        return []
    headers = [(c or "").strip() for c in values[hi]]
    out: list[dict] = []
    for row in values[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        d = {headers[j]: (row[j] if j < len(row) else "").strip() for j in range(len(headers))}
        if not d.get("cod_mp_sistema"):
            continue
        out.append(d)
    return out


def valorizar_bodega(
    cod_bodega: str,
    stock_map: dict[tuple[str, str], float],
    maestro: list[dict],
    *,
    incluir_negativos: bool = False,
) -> dict:
    bod = normalizar_cod_bodega(cod_bodega)
    # Índice maestro por (mp, bodega)
    por_clave: dict[tuple[str, str], dict] = {}
    for r in maestro:
        k = _clave_stock(r.get("cod_mp_sistema", ""), r.get("cod_bodega", ""))
        por_clave[k] = r

    total = 0.0
    n_items = 0
    n_sin_costo = 0
    stock_sin_costo = 0.0
    negativos_val = 0.0

    # Todas las claves con stock mov o fila en maestro para esa bodega
    claves: set[tuple[str, str]] = set()
    for k, qty in stock_map.items():
        if k[1] == bod and abs(qty) > 1e-9:
            claves.add(k)
    for k in por_clave:
        if k[1] == bod:
            claves.add(k)

    for k in sorted(claves):
        cod_mp, _ = k
        stock = float(stock_map.get(k, 0.0))
        info = por_clave.get(k, {})
        costo = _parse_num(info.get("costo_unitario_ref"))
        nombre = info.get("nombre_mp") or f"MP {cod_mp}"

        if abs(stock) < 1e-9:
            continue

        stock_val = stock if incluir_negativos else max(stock, 0.0)
        if stock < 0 and not incluir_negativos:
            negativos_val += stock * costo if costo > 0 else 0.0

        if costo <= 0:
            n_sin_costo += 1
            stock_sin_costo += abs(stock)
            continue

        val = stock_val * costo
        total += val
        n_items += 1

    return {
        "cod_bodega": bod,
        "valor_usd": round(total, 2),
        "items_valorizados": n_items,
        "items_sin_costo": n_sin_costo,
        "stock_abs_sin_costo": round(stock_sin_costo, 2),
        "nota_negativos": "Stock negativo contado como 0 en valor" if not incluir_negativos else "Incluye negativos",
    }


def main() -> None:
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Valorización inventario — fecha {hoy}")
    print("Stock: suma mov_inventario | Costo: costo_unitario_ref BD_MP_SISTEMA\n")

    maestro = cargar_maestro_mp()
    stock_map = build_stock_calculado()

    for bod, etiqueta in (("BOD-002", "Barra"), ("BOD-003", "Consignación")):
        r = valorizar_bodega(bod, stock_map, maestro)
        print(f"=== {etiqueta} ({bod}) ===")
        print(f"  Valor inventario:  USD {r['valor_usd']:,.2f}")
        print(f"  MPs valorizados:   {r['items_valorizados']}")
        if r["items_sin_costo"]:
            print(
                f"  MPs sin costo ref: {r['items_sin_costo']} "
                f"(stock abs sin valorar: {r['stock_abs_sin_costo']})"
            )
        print(f"  ({r['nota_negativos']})")
        print()

    b2 = valorizar_bodega("BOD-002", stock_map, maestro)
    b3 = valorizar_bodega("BOD-003", stock_map, maestro)
    print(f"TOTAL barra + consignación: USD {b2['valor_usd'] + b3['valor_usd']:,.2f}")


if __name__ == "__main__":
    main()
