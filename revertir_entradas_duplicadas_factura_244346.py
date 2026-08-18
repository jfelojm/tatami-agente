"""
Revierte ENTRADAs duplicadas factura 001-042-000244346 (reimport 02-jul-2026).
Conserva la corrida original del 12-jun; borra la segunda por ITEM_XML sin normalizar.

Uso:
  python revertir_entradas_duplicadas_factura_244346.py --dry-run
  python revertir_entradas_duplicadas_factura_244346.py --produccion
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(override=True)

NUM_DOC = "001-042-000244346"
COD_MOVS = (
    "MOV-20260611-158-20260702124457700",
    "MOV-20260611-179-20260702124458561",
    "MOV-20260611-198-20260702124458143",
)
MPS = ("158", "179", "198")
BOD = "BOD-002"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from whatsapp_webhook import conectar_supabase
    from recalcular_stock_sheets import _clave_stock, build_stock_calculado

    sb = conectar_supabase()
    movs = (
        sb.table("mov_inventario")
        .select("*")
        .in_("cod_mov", list(COD_MOVS))
        .order("cod_mp_sistema")
        .execute()
        .data
        or []
    )

    print("=" * 70)
    print(f"REVERTIR ENTRADAS DUPLICADAS {NUM_DOC} — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print("=" * 70)

    if len(movs) != 3:
        print(f"Esperaba 3 movimientos, encontrados {len(movs)}")
        for m in movs:
            print(f"  {m.get('cod_mov')} MP{m.get('cod_mp_sistema')}")
        return 0 if not movs else 1

    antes = {cod: float(build_stock_calculado().get(_clave_stock(cod, BOD), 0)) for cod in MPS}
    print("\nStock antes:")
    for cod in MPS:
        print(f"  MP {cod} @ {BOD}: {antes[cod]:,.0f} ml")

    total_ml = 0.0
    for m in movs:
        q = float(m["cantidad_mov"] or 0)
        total_ml += q
        print(
            f"\n  BORRAR {m['cod_mov']} | MP {m['cod_mp_sistema']} | "
            f"+{q:,.0f} ml | ${float(m.get('costo_total') or 0):,.2f}"
        )
        print(f"    obs: {(m.get('observaciones') or '')[-55:]}")

    print(f"\nEfecto: −{total_ml:,.0f} ml total en {BOD}")
    for cod in MPS:
        q = sum(float(m["cantidad_mov"] or 0) for m in movs if m["cod_mp_sistema"] == cod)
        print(f"  MP {cod}: {antes[cod]:,.0f} → {antes[cod] - q:,.0f} ml")

    if args.dry_run:
        print("\n[dry-run] No se borraron filas.")
        return 0

    for cod in COD_MOVS:
        sb.table("mov_inventario").delete().eq("cod_mov", cod).execute()

    root = os.path.dirname(__file__) or "."
    for cod in MPS:
        subprocess.run(
            [sys.executable, "recalcular_stock_sheets.py", "--produccion", "--cod-mp", cod],
            cwd=root,
            check=True,
        )

    despues = {cod: float(build_stock_calculado().get(_clave_stock(cod, BOD), 0)) for cod in MPS}
    print("\nStock después:")
    for cod in MPS:
        print(f"  MP {cod} @ {BOD}: {despues[cod]:,.0f} ml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
