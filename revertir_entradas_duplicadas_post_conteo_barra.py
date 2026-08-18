"""
Revierte ENTRADAs duplicadas post-conteo barra (29-may-2026 18:47:32).
Excluye factura 1204 (cantidades distintas — revisión manual).

Uso:
  python revertir_entradas_duplicadas_post_conteo_barra.py --dry-run
  python revertir_entradas_duplicadas_post_conteo_barra.py --produccion
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(override=True)

# DEL de auditar_entradas_factura_duplicadas.py --post-conteo-barra (sin 1204)
COD_MOVS_REVERTIR = (
    "MOV-20260630-309-20260709113036142",
    "MOV-20260610-156-20260702124449482",
    "MOV-20260610-160-20260702124449864",
    "MOV-20260610-171-20260702124454279",
    "MOV-20260610-199-20260702124450272",
    "MOV-20260610-210-20260702124452706",
    "MOV-20260610-223-20260702124451202",
    "MOV-20260610-228-20260702124456850",
    "MOV-20260610-233-20260702124455925",
    "MOV-20260610-239-20260702124453170",
    "MOV-20260610-288-20260702124450739",
    "MOV-20260610-301-20260702124451784",
    "MOV-20260610-301-20260702124456387",
    "MOV-20260610-302-20260702124453777",
    "MOV-20260610-543-20260702124455204",
    "MOV-20260610-550-20260702124452246",
    "MOV-20260610-V0ALC2-20260702124454788",
)
BOD = "BOD-002"
DOC = "POST-CONTEO-BARRA-DUP-20260710"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from whatsapp_webhook import conectar_supabase
    from recalcular_stock_sheets import _clave_stock, build_stock_calculado

    sb = conectar_supabase()
    movs = (
        sb.table("mov_inventario")
        .select("*")
        .in_("cod_mov", list(COD_MOVS_REVERTIR))
        .order("cod_mp_sistema")
        .execute()
        .data
        or []
    )

    print("=" * 70)
    print(f"REVERTIR DUPLICADOS POST-CONTEO BARRA — {'DRY RUN' if args.dry_run else 'PRODUCCION'}")
    print(f"Documento ref: {DOC} | mov esperados: {len(COD_MOVS_REVERTIR)}")
    print("=" * 70)

    found = {m["cod_mov"] for m in movs}
    missing = [c for c in COD_MOVS_REVERTIR if c not in found]
    if missing:
        print(f"WARN: faltan {len(missing)} mov (ya revertidos?):")
        for c in missing:
            print(f"  {c}")

    if not movs:
        print("Nada que revertir.")
        return 0

    mps = sorted({str(m["cod_mp_sistema"]) for m in movs})
    antes = {mp: float(build_stock_calculado().get(_clave_stock(mp, BOD), 0)) for mp in mps}

    total_qty = 0.0
    total_usd = 0.0
    for m in movs:
        q = float(m.get("cantidad_mov") or 0)
        ct = float(m.get("costo_total") or 0)
        total_qty += q
        total_usd += ct
        print(
            f"  DEL {m['cod_mov']} | MP {m['cod_mp_sistema']} | {m.get('num_documento','')} | "
            f"+{q:g} | ${ct:,.2f}"
        )

    print(f"\nEfecto: −{total_qty:,.0f} uni | −${total_usd:,.2f} | MPs: {', '.join(mps)}")

    if args.dry_run:
        print("\n[dry-run] No se borraron filas.")
        return 0

    for cod in found:
        sb.table("mov_inventario").delete().eq("cod_mov", cod).execute()

    root = os.path.dirname(__file__) or "."
    for mp in mps:
        subprocess.run(
            [sys.executable, "recalcular_stock_sheets.py", "--produccion", "--cod-mp", mp],
            cwd=root,
            check=True,
        )

    despues = {mp: float(build_stock_calculado().get(_clave_stock(mp, BOD), 0)) for mp in mps}
    print("\nStock BOD-002 después:")
    for mp in mps:
        print(f"  MP {mp}: {antes[mp]:,.0f} → {despues[mp]:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
