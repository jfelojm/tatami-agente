"""Revierte SALIDA_VENTA SUB-051 anteriores a la primera producción (backfill erróneo)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
COD = "SUB-051"
# Primera ENTRADA PRODUCCION_SUBRECETA registrada
CORTE = "2026-06-03T23:48:56"


def main() -> int:
    from supabase import create_client

    p = argparse.ArgumentParser(description=f"Revertir descargos {COD} pre-producción")
    p.add_argument("--produccion", action="store_true", help="Aplicar (default: dry-run)")
    args = p.parse_args()
    dry = not args.produccion

    sb = create_client(
        __import__("os").getenv("SUPABASE_URL"),
        __import__("os").getenv("SUPABASE_KEY"),
    )
    movs = (
        sb.table("mov_inventario")
        .select("cod_mov,fecha,tipo_mov,cantidad_mov,num_documento,observaciones")
        .eq("cod_mp_sistema", COD)
        .eq("tipo_mov", "SALIDA_VENTA")
        .lt("fecha", CORTE)
        .order("fecha")
        .execute()
        .data
        or []
    )

    print(f"{COD} SALIDA_VENTA antes de {CORTE}: {len(movs)} movimientos")
    total = 0.0
    for m in movs:
        c = float(m.get("cantidad_mov") or 0)
        total += c
        print(
            f"  {(m.get('fecha') or '')[:19]} | -{c:.0f} ml | "
            f"{m.get('num_documento')} | {(m.get('observaciones') or '')[:55]}"
        )
    print(f"Total a revertir: {total:.0f} ml")

    cod_movs = [m["cod_mov"] for m in movs if m.get("cod_mov")]
    if not cod_movs:
        print("Nada que revertir.")
        return 0

    if dry:
        print(f"\n[DRY RUN] Se eliminarían {len(cod_movs)} movimientos.")
        print("Stock esperado tras corrección: -540 +", f"{total:.0f} = {-540 + total:.0f} ml")
        return 0

    for i in range(0, len(cod_movs), 50):
        lote = cod_movs[i : i + 50]
        sb.table("mov_inventario").delete().in_("cod_mov", lote).execute()
    print(f"Eliminados {len(cod_movs)} movimientos.")

    print(f"Recalculando stock {COD}…")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "recalcular_stock_sheets.py"),
            "--produccion",
            "--cod-mp",
            COD,
        ],
        cwd=str(ROOT),
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
