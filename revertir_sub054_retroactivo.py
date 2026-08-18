"""Revierte salidas SUB-054 retroactivas 29-may a 02-jun (backfill erróneo)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)
ROOT = Path(__file__).resolve().parent
COD = "SUB-054"
DESDE = "2026-05-29"
HASTA = "2026-06-02"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--produccion", action="store_true")
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
        .gte("fecha", DESDE + "T00:00:00")
        .lte("fecha", HASTA + "T23:59:59")
        .order("fecha")
        .execute()
        .data
        or []
    )

    print(f"SUB-054 SALIDA_VENTA {DESDE} -> {HASTA}: {len(movs)} movimientos")
    total = 0.0
    for m in movs:
        c = float(m.get("cantidad_mov") or 0)
        total += c
        print(
            f"  {(m.get('fecha') or '')[:10]} | -{c:.0f} ml | "
            f"{m.get('num_documento')} | {(m.get('observaciones') or '')[:50]}"
        )
    print(f"Total a revertir: {total:.0f} ml")

    cod_movs = [m["cod_mov"] for m in movs if m.get("cod_mov")]
    if not cod_movs:
        print("Nada que revertir.")
        return 0

    if dry:
        print(f"\n[DRY RUN] Se eliminarían {len(cod_movs)} movimientos.")
        return 0

    for i in range(0, len(cod_movs), 50):
        lote = cod_movs[i : i + 50]
        sb.table("mov_inventario").delete().in_("cod_mov", lote).execute()
    print(f"Eliminados {len(cod_movs)} movimientos.")

    print("Recalculando stock SUB-054…")
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
