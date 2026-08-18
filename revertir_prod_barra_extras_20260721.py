"""
Revierte producciones barra erróneas de hoy (Eduardo solo hizo ron banana SUB-053).

Elimina mov_inventario de:
  PROD-SUB-SUB-051-202607211800  (Negroni)
  PROD-SUB-SUB-052-202607211800  (Tokio mule)
  PROD-SUB-SUB-054-202607211800  (Mojito coco)

Conserva SUB-053 (ron banana).

Uso:
  python revertir_prod_barra_extras_20260721.py --dry-run
  python revertir_prod_barra_extras_20260721.py --produccion
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(override=True)

DOCS = (
    "PROD-SUB-SUB-051-202607211800",
    "PROD-SUB-SUB-052-202607211800",
    "PROD-SUB-SUB-054-202607211800",
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
    print("=" * 70)
    print(
        f"REVERTIR PROD BARRA EXTRAS 21-jul — "
        f"{'DRY RUN' if args.dry_run else 'PRODUCCION'}"
    )
    print("Conserva SUB-053 ron banana")
    print("=" * 70)

    all_movs: list[dict] = []
    for doc in DOCS:
        movs = (
            sb.table("mov_inventario")
            .select(
                "cod_mov,fecha,tipo_mov,cod_mp_sistema,nombre_mp,cantidad_mov,"
                "cod_bodega_origen,cod_bodega_destino,num_documento,unidad_base"
            )
            .eq("num_documento", doc)
            .order("tipo_mov")
            .execute()
            .data
            or []
        )
        print(f"\n--- {doc}: {len(movs)} movs ---")
        if not movs:
            print("  (ya revertido o no existe)")
            continue
        for m in movs:
            print(
                f"  {m.get('tipo_mov'):16} | {m.get('cod_mp_sistema'):10} | "
                f"{(m.get('nombre_mp') or '')[:28]:28} | {m.get('cantidad_mov')} "
                f"{m.get('unidad_base') or ''} | {m.get('cod_mov')}"
            )
        all_movs.extend(movs)

    if not all_movs:
        print("\nNada que revertir.")
        return 0

    # Resumen por MP
    by_mp: dict[str, float] = defaultdict(float)
    for m in all_movs:
        cod = str(m.get("cod_mp_sistema") or "")
        qty = float(m.get("cantidad_mov") or 0)
        if m.get("tipo_mov") == "ENTRADA":
            by_mp[cod] -= qty  # al borrar entrada, stock baja
        else:
            by_mp[cod] += qty  # al borrar salida, stock sube
    print("\nEfecto neto esperado en stock (borrar estos movs):")
    for cod, delta in sorted(by_mp.items()):
        print(f"  {cod}: {delta:+.1f}")

    cod_movs = [m["cod_mov"] for m in all_movs if m.get("cod_mov")]
    mps = sorted({str(m.get("cod_mp_sistema") or "") for m in all_movs if m.get("cod_mp_sistema")})

    if args.dry_run:
        print(f"\n[dry-run] Se eliminarían {len(cod_movs)} movimientos.")
        return 0

    for i in range(0, len(cod_movs), 40):
        lote = cod_movs[i : i + 40]
        sb.table("mov_inventario").delete().in_("cod_mov", lote).execute()
    print(f"\nEliminados {len(cod_movs)} movimientos.")

    print(f"\nRecalculando stock BOD-002 ({len(mps)} MPs afectados)…")
    r = subprocess.run(
        [
            sys.executable,
            "recalcular_stock_sheets.py",
            "--produccion",
            "--cod-bodega",
            "BOD-002",
        ],
        check=False,
    )
    if r.returncode != 0:
        print(f"  WARN recalcular BOD-002 exit={r.returncode}")
        # Fallback por MP
        for cod in mps:
            subprocess.run(
                [
                    sys.executable,
                    "recalcular_stock_sheets.py",
                    "--produccion",
                    "--cod-mp",
                    cod,
                ],
                check=False,
            )

    # Verificar docs borrados y stock SUBs
    print("\n=== Verificación post-revert ===")
    for doc in DOCS:
        n = (
            sb.table("mov_inventario")
            .select("cod_mov", count="exact")
            .eq("num_documento", doc)
            .execute()
        )
        print(f"  {doc}: {n.count} movs restantes")

    keep = (
        sb.table("mov_inventario")
        .select("cod_mov,cantidad_mov,cod_mp_sistema")
        .eq("num_documento", "PROD-SUB-SUB-053-202607211800")
        .execute()
        .data
        or []
    )
    print(f"  SUB-053 (conservado): {len(keep)} movs OK")

    from whatsapp_webhook import leer_bd_mp_sistema

    rows = leer_bd_mp_sistema()
    for cod in ("SUB-051", "SUB-052", "SUB-053", "SUB-054"):
        for r in rows:
            if str(r.get("cod_mp_sistema") or "").strip().upper() == cod and str(
                r.get("cod_bodega") or ""
            ).upper() in ("BOD-002", "002"):
                print(
                    f"  stock {cod}@BOD-002 = {r.get('stock_actual')} "
                    f"{r.get('unidad_base')}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
