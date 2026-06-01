"""
Audita que los traslados lleven costo (política actual) y lista pares sin costo en mov.

Uso:
  python auditar_traslados_costo.py
  python auditar_traslados_costo.py --dias 30
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dias", type=int, default=0, help="0 = todo el historial")
    p.add_argument("--limit", type=int, default=50, help="Máx. grupos a listar")
    args = p.parse_args()

    from supabase import create_client

    from bodegas_config import _TRASLADOS_DIRIGIDOS
    from inventario_traslado import validar_mov_traslado_lleva_costo
    from recalcular_stock_sheets import paginar_todo

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    movs = paginar_todo(
        "mov_inventario",
        "cod_mov,tipo_mov,cod_mp_sistema,cantidad_mov,costo_unitario,fecha,"
        "cod_bodega_origen,cod_bodega_destino,num_documento",
    )

    cutoff = None
    if args.dias > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.dias)

    traslados = [
        m
        for m in movs
        if (m.get("tipo_mov") or "").strip() in ("TRASLADO_SALIDA", "TRASLADO_ENTRADA")
    ]
    if cutoff:
        traslados = [m for m in traslados if (m.get("fecha") or "")[:10] >= cutoff.strftime("%Y-%m-%d")]

    def _grupo_traslado(m: dict) -> str:
        doc = (m.get("num_documento") or "").strip()
        if doc:
            return doc
        cod = (m.get("cod_mov") or "").strip()
        for suf in ("-SAL", "-ENT"):
            if cod.endswith(suf):
                return cod[: -len(suf)]
        return cod

    por_doc: dict[str, list] = defaultdict(list)
    for m in traslados:
        por_doc[_grupo_traslado(m)].append(m)

    sin_costo: list[tuple[str, list]] = []
    con_costo = 0
    for doc, grupo in por_doc.items():
        if all(validar_mov_traslado_lleva_costo(m) for m in grupo):
            con_costo += 1
        else:
            sin_costo.append((doc, grupo))

    print("=" * 60)
    print("AUDITORÍA TRASLADOS — costo en mov_inventario")
    print("=" * 60)
    print(f"Pares permitidos en matriz ({len(_TRASLADOS_DIRIGIDOS)}):")
    for o, d in sorted(_TRASLADOS_DIRIGIDOS):
        print(f"  {o} → {d}")
    print()
    print(f"Grupos traslado (num_documento): {len(por_doc)}")
    print(f"  Con costo en SAL+ENT: {con_costo}")
    print(f"  Sin costo (legacy o origen sin ref): {len(sin_costo)}")
    print()
    print("Registro único en código: inventario_traslado.registrar_traslado_mp")
    print("Recálculo maestro: recalcular_stock_sheets (TRASLADO_ENTRADA + hermano bodega)")
    print()

    if sin_costo:
        print(f"Primeros {min(args.limit, len(sin_costo))} sin costo:")
        for doc, grupo in sin_costo[: args.limit]:
            m0 = grupo[0]
            print(
                f"  {doc} | MP {m0.get('cod_mp_sistema')} | "
                f"{m0.get('cod_bodega_origen')}→{m0.get('cod_bodega_destino')} | "
                f"fecha {str(m0.get('fecha', ''))[:10]}"
            )
            for m in grupo:
                print(
                    f"      {m.get('tipo_mov')} costo={m.get('costo_unitario')} "
                    f"cant={m.get('cantidad_mov')}"
                )
        print("\nCorregir histórico: python backfill_traslados_costo.py --dry-run")
        return 1

    print("OK: todos los grupos de traslado auditados llevan costo_unitario.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
