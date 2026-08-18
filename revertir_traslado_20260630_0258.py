"""
Revierte el traslado 2026-06-30 02:58 UTC (SHEETS:topax56@gmail.com).

Uso:
  python revertir_traslado_20260630_0258.py --dry-run
  python revertir_traslado_20260630_0258.py --produccion
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

FECHA_INI = "2026-06-30T02:58"
FECHA_FIN = "2026-06-30T02:59"
REGISTRADO_POR = "SHEETS:topax56@gmail.com"
ETIQUETA = "REVERSION:TRA-20260630-0258-topax56"


def _movs_lote(sb) -> list[dict]:
    return (
        sb.table("mov_inventario")
        .select(
            "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "num_documento,registrado_por,fecha"
        )
        .in_("tipo_mov", ["TRASLADO_SALIDA", "TRASLADO_ENTRADA"])
        .eq("registrado_por", REGISTRADO_POR)
        .gte("fecha", FECHA_INI)
        .lt("fecha", FECHA_FIN)
        .order("cod_mov")
        .execute()
        .data
        or []
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from whatsapp_webhook import conectar_supabase

    sb = conectar_supabase()
    movs = _movs_lote(sb)

    print("=" * 60)
    print(f"REVERTIR TRASLADO — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print(f"Lote: {FECHA_INI} | {REGISTRADO_POR}")
    print("=" * 60)

    if not movs:
        print("No hay movimientos para ese lote (ya revertido?).")
        return 0

    sal = [m for m in movs if m["tipo_mov"] == "TRASLADO_SALIDA"]
    ent = [m for m in movs if m["tipo_mov"] == "TRASLADO_ENTRADA"]
    print(f"\nMovimientos: {len(movs)} ({len(sal)} salidas + {len(ent)} entradas)")
    for m in movs:
        print(
            f"  {m['cod_mov']:36} {m['tipo_mov'][:16]:16} "
            f"MP {m['cod_mp_sistema']:8} qty={m['cantidad_mov']}"
        )

    cod_movs = [m["cod_mov"] for m in movs if m.get("cod_mov")]
    if args.dry_run:
        print(f"\n[dry-run] Se borrarian {len(cod_movs)} filas en mov_inventario.")
        print("Luego: recalcular stock BD_MP_SISTEMA (una pasada).")
        return 0

    sb.table("mov_inventario").delete().in_("cod_mov", cod_movs).execute()
    print(f"\nBorrados: {len(cod_movs)} movimientos ({ETIQUETA})")

    try:
        from recalcular_stock_sheets import recalcular

        print("Recalculando stock en Sheets...")
        recalcular(dry_run=False)
        print("OK: stock actualizado.")
    except Exception as e:
        print(f"WARN: recalcular stock: {e}")
        return 1

    rest = _movs_lote(sb)
    if rest:
        print(f"ERROR: quedan {len(rest)} movimientos del lote.")
        return 1

    print("\nListo. Traslado 005->001 de ese lote anulado en Supabase y Sheets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
