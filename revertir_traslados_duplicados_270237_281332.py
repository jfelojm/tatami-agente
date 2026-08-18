"""
Anula traslados masivos duplicados desde Sheets (topax56@gmail.com):

  1. 2026-06-27 02:37 — lote con pimiento amarillo (587), 21 ítems
  2. 2026-06-28 13:32 — reenvío exacto 12 min después del lote 13:20, 22 ítems

Uso:
  python revertir_traslados_duplicados_270237_281332.py --dry-run
  python revertir_traslados_duplicados_270237_281332.py --produccion
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

REGISTRADO_POR = "SHEETS:topax56@gmail.com"
ETIQUETA = "REVERSION:TRA-duplicados-270237-281332"

LOTES = (
    ("2026-06-27 02:37 (pimiento amarillo)", "2026-06-27T02:37:00", "2026-06-27T02:38:00"),
    ("2026-06-28 13:32 (duplicado)", "2026-06-28T13:32:00", "2026-06-28T13:33:00"),
)


def _movs_lote(sb, fecha_ini: str, fecha_fin: str) -> list[dict]:
    return (
        sb.table("mov_inventario")
        .select(
            "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "cod_bodega_origen,cod_bodega_destino,num_documento,registrado_por,fecha"
        )
        .in_("tipo_mov", ["TRASLADO_SALIDA", "TRASLADO_ENTRADA"])
        .eq("registrado_por", REGISTRADO_POR)
        .eq("cod_bodega_origen", "BOD-005")
        .gte("fecha", fecha_ini)
        .lt("fecha", fecha_fin)
        .order("cod_mov")
        .execute()
        .data
        or []
    )


def _movs_entrada_lote(sb, fecha_ini: str, fecha_fin: str) -> list[dict]:
    return (
        sb.table("mov_inventario")
        .select(
            "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "cod_bodega_origen,cod_bodega_destino,num_documento,registrado_por,fecha"
        )
        .eq("tipo_mov", "TRASLADO_ENTRADA")
        .eq("registrado_por", REGISTRADO_POR)
        .eq("cod_bodega_destino", "BOD-001")
        .gte("fecha", fecha_ini)
        .lt("fecha", fecha_fin)
        .order("cod_mov")
        .execute()
        .data
        or []
    )


def _movs_lote_completo(sb, fecha_ini: str, fecha_fin: str) -> list[dict]:
    sal = _movs_lote(sb, fecha_ini, fecha_fin)
    if sal:
        return sal + _movs_entrada_lote(sb, fecha_ini, fecha_fin)
    return _movs_entrada_lote(sb, fecha_ini, fecha_fin)


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
    todos: list[dict] = []

    print("=" * 60)
    print(f"REVERTIR TRASLADOS DUPLICADOS — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print("=" * 60)

    for etiqueta, ini, fin in LOTES:
        movs = _movs_lote_completo(sb, ini, fin)
        sal = [m for m in movs if m["tipo_mov"] == "TRASLADO_SALIDA"]
        ent = [m for m in movs if m["tipo_mov"] == "TRASLADO_ENTRADA"]
        mp059 = [m for m in sal if m.get("cod_mp_sistema") == "059"]
        mp587 = [m for m in sal if m.get("cod_mp_sistema") == "587"]
        print(f"\n--- {etiqueta} ---")
        print(f"Movimientos: {len(movs)} ({len(sal)} sal + {len(ent)} ent)")
        if mp059:
            print(f"  MP 059: {mp059[0]['cantidad_mov']} ml")
        if mp587:
            print(f"  MP 587 pimiento amarillo: {mp587[0]['cantidad_mov']} gr")
        for m in movs:
            print(
                f"  {m['cod_mov']:36} {m['tipo_mov'][:16]:16} "
                f"MP {m['cod_mp_sistema']:8} qty={m['cantidad_mov']}"
            )
        todos.extend(movs)

    if not todos:
        print("\nNo hay movimientos (ya revertidos?).")
        return 0

    cod_movs = list({m["cod_mov"] for m in todos if m.get("cod_mov")})
    print(f"\nTotal filas a borrar: {len(cod_movs)}")

    if args.dry_run:
        print("[dry-run] Sin cambios. Ejecuta con --produccion para aplicar.")
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

    # Verificar MP 059
    from recalcular_stock_sheets import build_stock_calculado

    stock = build_stock_calculado()
    print(f"\nStock MP 059 @ BOD-005: {stock.get(('059', 'BOD-005'), 0):.1f} ml")
    print(f"Stock MP 059 @ BOD-001: {stock.get(('059', 'BOD-001'), 0):.1f} ml")

    print("\nListo. Lotes 27-jun 02:37 y 28-jun 13:32 anulados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
