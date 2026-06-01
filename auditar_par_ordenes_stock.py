"""
Valida PAR global vs stock sumado en todas las bodegas (órdenes de compra / alertas).

Uso:
  python auditar_par_ordenes_stock.py
  python auditar_par_ordenes_stock.py --mp 566 176
  python auditar_par_ordenes_stock.py --tipo barra
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mp", nargs="*", help="Filtrar cod_mp")
    p.add_argument("--tipo", choices=("barra", "cocina", "todos"), default=None)
    args = p.parse_args()

    from inventario_stock_mp import agrupar_stock_par_por_mp, norm_mp
    from whatsapp_webhook import leer_bd_mp_sistema

    filt = {norm_mp(m) for m in args.mp} if args.mp else None
    agrupado = agrupar_stock_par_por_mp(leer_bd_mp_sistema())

    if args.tipo:
        from generar_ordenes_compra import cargar_stock_por_mp_bodega

        ordenes_mp = set(cargar_stock_por_mp_bodega(args.tipo).keys())
        print(f"MPs bajo PAR para órdenes ({args.tipo}): {len(ordenes_mp)}")
    else:
        ordenes_mp = None

    print("=" * 72)
    print("PAR (global) vs stock_total (suma bodegas activas)")
    print("=" * 72)

    n_bajo = 0
    for cod in sorted(agrupado.keys()):
        if filt and cod not in filt:
            continue
        info = agrupado[cod]
        if not info.get("bajo_par"):
            continue
        n_bajo += 1
        en_orden = "sí" if ordenes_mp is not None and cod in ordenes_mp else ("—" if ordenes_mp is None else "no catálogo")
        desglose = ", ".join(f"{b}={v}" for b, v in info["por_bodega"].items())
        print(
            f"  {cod} {info['nombre_mp'][:28]:28} | "
            f"stock_total={info['stock_total']} par={info['par_level']} "
            f"falta={info['cantidad_faltante']} | {desglose} | orden {args.tipo or ''}: {en_orden}"
        )

    # MPs con stock solo en 003 que antes parecían bajo par en 002 solo
    if filt:
        for cod in sorted(filt):
            info = agrupado.get(cod)
            if not info:
                print(f"  {cod}: sin filas en maestro")
                continue
            print(
                f"\nDetalle {cod}: total={info['stock_total']} par={info['par_level']} "
                f"bajo_par={info['bajo_par']}"
            )
            for b, v in info["por_bodega"].items():
                print(f"    {b}: {v}")

    print(f"\nMPs bajo PAR (global): {n_bajo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
