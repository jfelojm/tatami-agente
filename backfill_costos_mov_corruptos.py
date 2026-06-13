"""
Backfill costo_unitario en mov_inventario corruptos (~÷1000 o ratio extremo).

Usa mapa de costos correctos (mismo que corregir_costos_licores_barra.py).

Uso:
  python backfill_costos_mov_corruptos.py --dry-run
  python backfill_costos_mov_corruptos.py --produccion
  python backfill_costos_mov_corruptos.py --produccion --desde 2026-05-29
"""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv

load_dotenv(override=True)

from corregir_costos_licores_barra import COSTOS_CORRECTOS


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument(
        "--desde",
        default="2026-05-29",
        help="Solo movimientos desde esta fecha (YYYY-MM-DD)",
    )
    p.add_argument(
        "--ratio-max",
        type=float,
        default=0.05,
        help="Corregir si costo_mov < cu_ok * ratio_max (default 5%% del correcto)",
    )
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    total = 0
    fixed = 0

    for mp, cu_ok in sorted(COSTOS_CORRECTOS.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        umbral = cu_ok * args.ratio_max
        offset = 0
        while True:
            q = (
                sb.table("mov_inventario")
                .select(
                    "cod_mov,fecha,tipo_mov,cantidad_mov,costo_unitario,costo_total,num_documento"
                )
                .eq("cod_mp_sistema", mp)
                .gte("fecha", f"{args.desde}T00:00:00")
                .gt("costo_unitario", 0)
                .lt("costo_unitario", umbral)
                .range(offset, offset + 999)
                .execute()
            )
            rows = q.data or []
            for m in rows:
                total += 1
                cu_old = float(m.get("costo_unitario") or 0)
                qty = float(m.get("cantidad_mov") or 0)
                cu_new = round(cu_ok, 6)
                ct_new = round(qty * cu_new, 4)
                tipo = (m.get("tipo_mov") or "").strip()
                # ENTRADAs con cantidad/costo total absurdo: no auto-corregir (dato erróneo)
                if tipo == "ENTRADA" and (qty > 200 or ct_new > 5000):
                    print(
                        f"  SKIP {m['cod_mov'][:36]} MP{mp} ENTRADA qty={qty} "
                        f"(revisar manual, no backfill auto)"
                    )
                    continue
                print(
                    f"  {m['cod_mov'][:40]:40} MP{mp:>3} "
                    f"{m['tipo_mov']:16} cu {cu_old:.6f} -> {cu_new:.6f} "
                    f"({m.get('fecha','')[:10]})"
                )
                if args.produccion:
                    sb.table("mov_inventario").update(
                        {"costo_unitario": cu_new, "costo_total": ct_new}
                    ).eq("cod_mov", m["cod_mov"]).execute()
                    fixed += 1
                    if fixed % 50 == 0:
                        time.sleep(0.2)
            if len(rows) < 1000:
                break
            offset += 1000

    # Conteo/traslado con costo 0 pero deberían llevar costo
    for mp, cu_ok in COSTOS_CORRECTOS.items():
        offset = 0
        while True:
            q = (
                sb.table("mov_inventario")
                .select("cod_mov,fecha,tipo_mov,cantidad_mov,costo_unitario,origen_documento")
                .eq("cod_mp_sistema", mp)
                .gte("fecha", f"{args.desde}T00:00:00")
                .in_(
                    "tipo_mov",
                    ["AJUSTE_POSITIVO", "AJUSTE_NEGATIVO", "TRASLADO_ENTRADA", "TRASLADO_SALIDA"],
                )
                .lte("costo_unitario", 0)
                .range(offset, offset + 999)
                .execute()
            )
            rows = q.data or []
            for m in rows:
                qty = float(m.get("cantidad_mov") or 0)
                if qty <= 0:
                    continue
                total += 1
                cu_new = round(cu_ok, 6)
                ct_new = round(qty * cu_new, 4)
                print(
                    f"  {m['cod_mov'][:40]:40} MP{mp:>3} "
                    f"{m['tipo_mov']:16} cu 0 -> {cu_new:.6f} (sin costo)"
                )
                if args.produccion:
                    sb.table("mov_inventario").update(
                        {"costo_unitario": cu_new, "costo_total": ct_new}
                    ).eq("cod_mov", m["cod_mov"]).execute()
                    fixed += 1
            if len(rows) < 1000:
                break
            offset += 1000

    tag = "PRODUCCION" if args.produccion else "DRY-RUN"
    print(f"\n[{tag}] Movimientos revisados/corregidos: {total} | escritos: {fixed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
