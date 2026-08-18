"""
Revierte traslado duplicado MP 585 TRA-20260624185644 (24-jun).
Duplicaba la factura 025-104-000420508 (+1750 ya en BOD-001); salió de BOD-005 sin stock.

Uso:
  python revertir_traslado_vaina_585_20260624.py --dry-run
  python revertir_traslado_vaina_585_20260624.py --produccion
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

NUM_DOC = "TRA-20260624185644"
COD_MOVS = ("TRA-20260624185644-SAL", "TRA-20260624185644-ENT")
MP = "585"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from whatsapp_webhook import conectar_supabase
    from recalcular_stock_sheets import build_stock_calculado, _clave_stock

    sb = conectar_supabase()
    movs = (
        sb.table("mov_inventario")
        .select("*")
        .in_("cod_mov", list(COD_MOVS))
        .order("tipo_mov")
        .execute()
        .data
        or []
    )

    print("=" * 60)
    print(f"REVERTIR TRASLADO DUPLICADO VAINA CHINA — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print(f"Documento: {NUM_DOC}")
    print("=" * 60)

    if len(movs) != 2:
        print(f"Esperaba 2 movimientos, encontrados {len(movs)} (ya revertido?)")
        for m in movs:
            print(f"  {m.get('cod_mov')}")
        return 0 if not movs else 1

    antes = {b: float(build_stock_calculado().get(_clave_stock(MP, b), 0)) for b in ("BOD-001", "BOD-005")}
    print(f"\nStock MP {MP} antes:")
    print(f"  BOD-001: {antes['BOD-001']:+.1f} gr")
    print(f"  BOD-005: {antes['BOD-005']:+.1f} gr")

    for m in movs:
        print(
            f"\n  {m['cod_mov']} | {m['tipo_mov']} | "
            f"{m.get('cod_bodega_origen')} -> {m.get('cod_bodega_destino')} | {m['cantidad_mov']} gr"
        )
        print(f"  obs: {m.get('observaciones')}")

    print(f"\nEfecto esperado: BOD-001 -1750 gr, BOD-005 +1750 gr")
    print(f"  BOD-001: {antes['BOD-001'] - 1750:+.1f} gr")
    print(f"  BOD-005: {antes['BOD-005'] + 1750:+.1f} gr")

    if args.dry_run:
        print("\n[dry-run] Se borrarian 2 filas en mov_inventario.")
        return 0

    sb.table("mov_inventario").delete().in_("cod_mov", list(COD_MOVS)).execute()
    print("\nBorrados 2 movimientos (REVERSION:TRA-20260624185644-duplicado-vaina)")

    subprocess.run(
        [sys.executable, "recalcular_stock_sheets.py", "--produccion", "--cod-mp", MP],
        check=True,
    )

    despues = {b: float(build_stock_calculado().get(_clave_stock(MP, b), 0)) for b in ("BOD-001", "BOD-005")}
    print(f"\nStock MP {MP} despues:")
    print(f"  BOD-001: {despues['BOD-001']:+.1f} gr")
    print(f"  BOD-005: {despues['BOD-005']:+.1f} gr")
    print(f"  Total red: {despues['BOD-001'] + despues['BOD-005']:+.1f} gr")
    return 0


if __name__ == "__main__":
    sys.exit(main())
