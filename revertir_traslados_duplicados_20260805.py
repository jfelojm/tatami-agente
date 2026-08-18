"""
Anula traslados BOD-005 -> BOD-001 duplicados del 2026-08-05.

Mantiene 1 lote (jfelojm @ 12:54, prefijo TRA-202608051254).
Revierte 2 lotes duplicados de topax56 (12:49 TRA-202608051249*, 12:50 TRA-202608051250*).

Uso:
  python revertir_traslados_duplicados_20260805.py --dry-run
  python revertir_traslados_duplicados_20260805.py --produccion
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

FECHA = "2026-08-05"
# Prefijos num_documento a borrar (duplicados)
PREFIX_REVERTIR = ("TRA-202608051249", "TRA-202608051250")
# Prefijo a conservar
PREFIX_MANTENER = "TRA-202608051254"
ETIQUETA = "REVERSION:TRA-duplicados-20260805"


def _pares_005_001(sb, fecha: str) -> list[dict]:
    sal = (
        sb.table("mov_inventario")
        .select(
            "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
            "cod_bodega_origen,cod_bodega_destino,num_documento,registrado_por,fecha"
        )
        .eq("tipo_mov", "TRASLADO_SALIDA")
        .eq("cod_bodega_origen", "BOD-005")
        .gte("fecha", f"{fecha}T00:00:00")
        .lt("fecha", f"{fecha}T23:59:59")
        .execute()
        .data
        or []
    )
    nums = [m["num_documento"] for m in sal if m.get("num_documento")]
    ent_dest: dict[str, str] = {}
    for i in range(0, len(nums), 80):
        chunk = nums[i : i + 80]
        ent = (
            sb.table("mov_inventario")
            .select("num_documento,cod_bodega_destino")
            .in_("num_documento", chunk)
            .eq("tipo_mov", "TRASLADO_ENTRADA")
            .execute()
            .data
            or []
        )
        for e in ent:
            ent_dest[e["num_documento"]] = (e.get("cod_bodega_destino") or "").strip()
    sal001 = [m for m in sal if ent_dest.get(m.get("num_documento")) == "BOD-001"]
    nums001 = {m["num_documento"] for m in sal001}
    ent = []
    if nums001:
        for i in range(0, len(list(nums001)), 80):
            chunk = list(nums001)[i : i + 80]
            ent.extend(
                sb.table("mov_inventario")
                .select(
                    "cod_mov,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
                    "num_documento,registrado_por,fecha"
                )
                .in_("num_documento", chunk)
                .eq("tipo_mov", "TRASLADO_ENTRADA")
                .execute()
                .data
                or []
            )
    return sal001 + ent


def _doc_revertir(num_documento: str) -> bool:
    doc = (num_documento or "").strip()
    return any(doc.startswith(p) for p in PREFIX_REVERTIR)


def _doc_mantener(num_documento: str) -> bool:
    return (num_documento or "").strip().startswith(PREFIX_MANTENER)


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
    todos = _pares_005_001(sb, FECHA)
    borrar = [m for m in todos if _doc_revertir(m.get("num_documento") or "")]
    mantener = [m for m in todos if _doc_mantener(m.get("num_documento") or "")]

    print("=" * 60)
    print(f"REVERTIR DUPLICADOS {FECHA} — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print("=" * 60)
    print(f"Movimientos del dia 005->001: {len(todos)}")
    print(f"MANTENER ({PREFIX_MANTENER}*): {len(mantener)} movs")
    print(f"BORRAR ({', '.join(PREFIX_REVERTIR)}*): {len(borrar)} movs")

    if mantener:
        sal_m = [m for m in mantener if m.get("tipo_mov") == "TRASLADO_SALIDA"]
        print(f"\nLote conservado: {len(sal_m)} lineas SAL")
        for m in sorted(sal_m, key=lambda x: x.get("cod_mp_sistema", "")):
            print(
                f"  {m.get('num_documento')} MP {m.get('cod_mp_sistema')} "
                f"qty={m.get('cantidad_mov')} {m.get('nombre_mp', '')[:30]}"
            )

    if borrar:
        print(f"\nA borrar ({len(borrar)} movs):")
        for m in borrar[:10]:
            print(f"  {m.get('cod_mov')} {m.get('tipo_mov')} {m.get('num_documento')}")
        if len(borrar) > 10:
            print(f"  ... y {len(borrar) - 10} mas")

    if not borrar:
        print("\nNada que revertir.")
        return 0

    cod_movs = list({m["cod_mov"] for m in borrar if m.get("cod_mov")})
    if args.dry_run:
        print(f"\n[dry-run] Se borrarian {len(cod_movs)} cod_mov.")
        return 0

    sb.table("mov_inventario").delete().in_("cod_mov", cod_movs).execute()
    print(f"\nBorrados: {len(cod_movs)} movimientos ({ETIQUETA})")

    from recalcular_stock_sheets import recalcular_produccion

    print("Recalculando stock...")
    recalcular_produccion()
    print("OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
