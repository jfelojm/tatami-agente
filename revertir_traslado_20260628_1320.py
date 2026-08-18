"""
Anula traslado masivo duplicado 2026-06-28 13:20 (idéntico al de 27-jun 22:52).

Uso:
  python revertir_traslado_20260628_1320.py --dry-run
  python revertir_traslado_20260628_1320.py --produccion
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

FECHA_INI = "2026-06-28T13:20:00"
FECHA_FIN = "2026-06-28T13:21:00"
REGISTRADO_POR = "SHEETS:topax56@gmail.com"
ETIQUETA = "REVERSION:TRA-20260628-1320-duplicado-272252"


def _salidas(sb) -> list[dict]:
    return (
        sb.table("mov_inventario")
        .select(
            "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "cod_bodega_origen,cod_bodega_destino,registrado_por,fecha"
        )
        .eq("tipo_mov", "TRASLADO_SALIDA")
        .eq("registrado_por", REGISTRADO_POR)
        .eq("cod_bodega_origen", "BOD-005")
        .gte("fecha", FECHA_INI)
        .lt("fecha", FECHA_FIN)
        .order("cod_mov")
        .execute()
        .data
        or []
    )


def _entradas(sb) -> list[dict]:
    return (
        sb.table("mov_inventario")
        .select(
            "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "cod_bodega_origen,cod_bodega_destino,registrado_por,fecha"
        )
        .eq("tipo_mov", "TRASLADO_ENTRADA")
        .eq("registrado_por", REGISTRADO_POR)
        .eq("cod_bodega_destino", "BOD-001")
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
    movs = _salidas(sb) + _entradas(sb)

    print("=" * 60)
    print(f"REVERTIR TRASLADO 28-jun 13:20 — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print("=" * 60)

    if not movs:
        print("No hay movimientos (ya revertido?).")
        return 0

    sal = [m for m in movs if m["tipo_mov"] == "TRASLADO_SALIDA"]
    ent = [m for m in movs if m["tipo_mov"] == "TRASLADO_ENTRADA"]
    mp059 = [m for m in sal if m.get("cod_mp_sistema") == "059"]
    print(f"Movimientos: {len(movs)} ({len(sal)} sal + {len(ent)} ent)")
    if mp059:
        print(f"MP 059 aceite sesamo: {mp059[0]['cantidad_mov']} ml")
    for m in movs:
        print(
            f"  {m['cod_mov']:36} {m['tipo_mov'][:16]:16} "
            f"MP {m['cod_mp_sistema']:8} qty={m['cantidad_mov']}"
        )

    cod_movs = [m["cod_mov"] for m in movs if m.get("cod_mov")]
    if args.dry_run:
        print(f"\n[dry-run] Se borrarian {len(cod_movs)} filas.")
        return 0

    sb.table("mov_inventario").delete().in_("cod_mov", cod_movs).execute()
    print(f"\nBorrados: {len(cod_movs)} ({ETIQUETA})")

    from recalcular_stock_sheets import build_stock_calculado, recalcular

    recalcular(dry_run=False)
    stock = build_stock_calculado()
    print(f"Stock MP 059 @ BOD-005: {stock.get(('059', 'BOD-005'), 0):.1f} ml")
    print(f"Stock MP 059 @ BOD-001: {stock.get(('059', 'BOD-001'), 0):.1f} ml")
    print("\nListo. Duplicado 28-jun 13:20 anulado; vigente queda 27-jun 22:52.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
