"""
Ajusta SUB-052 (batch tokio mule): anula SALIDA_VENTA anteriores a la
primera produccion (2026-06-03). Esas ventas debieron descargarse con la
receta antigua, no contra el semi.

Uso:
  python ajustar_sub052_stock_prebatch.py --dry-run
  python ajustar_sub052_stock_prebatch.py --produccion
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
COD = "SUB-052"
BOD = "BOD-002"
FECHA_CORTE = "2026-06-03"  # primera produccion inclusive
DOC = f"AJUSTE-SUB052-PREBATCH-{datetime.now(timezone.utc).strftime('%Y%m%d')}"


def main() -> int:
    from recalcular_stock_sheets import build_stock_calculado
    from reporte_semanal import conectar_supabase
    from whatsapp_webhook import supabase_query_all

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2
    dry = args.dry_run or not args.produccion

    sb = conectar_supabase()
    rows = supabase_query_all(
        sb,
        "mov_inventario",
        "cod_mov,fecha,tipo_mov,cantidad_mov,num_documento,origen_documento,observaciones",
        [("eq", "cod_mp_sistema", COD)],
    )

    pre = [
        r
        for r in rows
        if (r.get("fecha") or "")[:10] < FECHA_CORTE
        and (r.get("tipo_mov") or "").upper() == "SALIDA_VENTA"
    ]
    ml_pre = sum(abs(float(r.get("cantidad_mov") or 0)) for r in pre)
    stock0 = float(build_stock_calculado().get((COD, BOD), 0.0))

    print(f"SUB-052 @ {BOD}")
    print(f"  Stock calculado actual: {stock0:.1f} ml")
    print(f"  Corte: >= {FECHA_CORTE} (1a produccion)")
    print(f"  SALIDA_VENTA pre-batch a anular: {len(pre)} movs / {ml_pre:.0f} ml")
    for r in sorted(pre, key=lambda x: x.get("fecha") or ""):
        print(
            f"    {(r.get('fecha') or '')[:10]}  "
            f"{float(r.get('cantidad_mov') or 0):.0f} ml  "
            f"{r.get('num_documento')}"
        )

    # Tras anular pre-batch, stock teorico = stock0 + ml_pre
    # Objetivo operativo: redondear a 0 si |teorico| < 45 (media porcion)
    teorico = stock0 + ml_pre
    objetivo = 0.0 if abs(teorico) < 45 else round(teorico, 1)
    delta_fine = teorico - objetivo  # si teorico=-20 y obj=0 → delta  -20 means need +20 AJUSTE?
    # stock after delete pre = teorico. Want objetivo.
    # If teorico=-20, objetivo=0 → need AJUSTE_POSITIVO 20
    ajuste_ml = objetivo - teorico

    print(f"\n  Stock tras anular pre-batch: {teorico:.1f} ml")
    print(f"  Objetivo: {objetivo:.1f} ml")
    if abs(ajuste_ml) >= 0.5:
        tipo = "AJUSTE_POSITIVO" if ajuste_ml > 0 else "AJUSTE_NEGATIVO"
        print(f"  Fine-tune: {tipo} {abs(ajuste_ml):.1f} ml")
    else:
        print("  Fine-tune: no hace falta")

    if dry:
        print("\n[DRY-RUN] sin cambios")
        return 0

    # 1) Borrar salidas pre-batch
    for r in pre:
        cod_mov = r.get("cod_mov")
        if not cod_mov:
            continue
        sb.table("mov_inventario").delete().eq("cod_mov", cod_mov).execute()
        print(f"  deleted {cod_mov}")

    # 2) Fine-tune si aplica
    if abs(ajuste_ml) >= 0.5:
        ts = uuid.uuid4().hex[:12]
        cant = abs(ajuste_ml)
        tipo = "AJUSTE_POSITIVO" if ajuste_ml > 0 else "AJUSTE_NEGATIVO"
        costo_u = 0.025098  # costo unitario estandar SUB-052
        mov = {
            "cod_mov": f"MOV-AJ-{COD}-{ts}",
            "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "tipo_mov": tipo,
            "cod_mp_sistema": COD,
            "nombre_mp": "batch tokio mule",
            "cod_bodega_origen": BOD if tipo == "AJUSTE_NEGATIVO" else None,
            "cod_bodega_destino": BOD if tipo == "AJUSTE_POSITIVO" else None,
            "cantidad_mov": cant,
            "unidad_base": "ml",
            "costo_unitario": costo_u,
            "costo_total": round(cant * costo_u, 4),
            "origen_documento": "AJUSTE_MANUAL",
            "num_documento": DOC,
            "registrado_por": "AGENTE",
            "observaciones": (
                f"Cuadre post-anulacion salidas pre-batch (<{FECHA_CORTE}); "
                f"teorico {teorico:.1f} -> objetivo {objetivo:.1f} ml"
            ),
        }
        sb.table("mov_inventario").insert(mov).execute()
        print(f"  inserted {tipo} {cant:.1f} ml")

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

    stock2 = float(build_stock_calculado().get((COD, BOD), 0.0))
    print(f"\nStock final calculado: {stock2:.1f} ml (objetivo {objetivo:.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
