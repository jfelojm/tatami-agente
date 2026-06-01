"""Exporta MPs bajo PAR sin fila en BD_ITEMS_PROV (barra / BOD-002)."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> int:
    from generar_ordenes_compra import (
        TIPO_A_BODEGA,
        cargar_items_prov_por_mp,
        cargar_proveedores_por_tipo,
        cargar_stock_por_mp_bodega,
    )

    tipo = "barra"
    bodega = TIPO_A_BODEGA["BARRA"]
    prov = cargar_proveedores_por_tipo(tipo)
    bajo = cargar_stock_por_mp_bodega(tipo)
    items = cargar_items_prov_por_mp(prov, bodega)

    rows: list[dict] = []
    for cod, mp in sorted(bajo.items(), key=lambda x: float(x[1].get("cantidad_base", 0)), reverse=True):
        if cod in items:
            continue
        por = mp.get("stock_por_bodega") or {}
        desglose = "; ".join(f"{b}:{v}" for b, v in sorted(por.items()))
        rows.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": mp["nombre_mp"],
                "unidad_base": mp["unidad_base"],
                "stock_total": mp["stock_actual"],
                "par_level": mp["par_level"],
                "cantidad_faltante": mp["cantidad_base"],
                "stock_por_bodega": desglose,
                "motivo": "Sin BD_ITEMS_PROV: proveedor Barra + cod_bodega_destino BOD-002",
            }
        )

    out = Path("logs/mp_bajo_par_sin_catalogo_barra.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"Archivo: {out.resolve()}")
    print(f"Total: {len(rows)} MPs bajo PAR sin catálogo (de {len(bajo)} bajo PAR)")
    print()
    for r in rows[:35]:
        print(
            f"  {r['cod_mp_sistema']:>4} {r['nombre_mp'][:34]:34} "
            f"stk={r['stock_total']} par={r['par_level']} falta={r['cantidad_faltante']}"
        )
    if len(rows) > 35:
        print(f"  ... y {len(rows) - 35} más en el CSV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
