"""
Rellena costo_unitario en movimientos TRASLADO_* sin costo, usando costo del maestro origen.

Uso:
  python backfill_traslados_costo.py --dry-run
  python backfill_traslados_costo.py --produccion
  python backfill_traslados_costo.py --produccion --dias 90
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from google_credentials import google_credentials

load_dotenv(override=True)


def _costo_mp_por_bodega() -> dict[tuple[str, str], float]:
    import gspread
    from bodegas_config import normalizar_cod_bodega
    from numeros_sheets import parse_numero_sheets
    from recalcular_stock_sheets import _cod_mp_norm

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    h = [(c or "").strip() for c in vals[hi]]
    ic, ib, icu = h.index("cod_mp_sistema"), h.index("cod_bodega"), h.index("costo_unitario_ref")
    out: dict[tuple[str, str], float] = {}
    for row in vals[hi + 1 :]:
        nk = _cod_mp_norm(row[ic] if ic < len(row) else "")
        bod = normalizar_cod_bodega(row[ib] if ib < len(row) else "")
        if not nk or not bod:
            continue
        try:
            cu = parse_numero_sheets(row[icu] if icu < len(row) else 0)
        except ValueError:
            cu = 0.0
        if cu > 0:
            out[(nk, bod)] = cu
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--dias", type=int, default=0)
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from supabase import create_client

    from bodegas_config import normalizar_cod_bodega
    from inventario_traslado import validar_mov_traslado_lleva_costo
    from recalcular_stock_sheets import _cod_mp_norm, paginar_todo

    costo_hoja = _costo_mp_por_bodega()
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

    actualizados = 0
    for m in traslados:
        if validar_mov_traslado_lleva_costo(m):
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        nk = _cod_mp_norm(m.get("cod_mp_sistema") or "")
        if tipo == "TRASLADO_SALIDA":
            bod = normalizar_cod_bodega(m.get("cod_bodega_origen"))
        else:
            bod = normalizar_cod_bodega(m.get("cod_bodega_destino"))
        cu = costo_hoja.get((nk, bod), 0.0)
        if cu <= 0 and tipo == "TRASLADO_ENTRADA":
            bod_o = normalizar_cod_bodega(m.get("cod_bodega_origen"))
            if bod_o:
                cu = costo_hoja.get((nk, bod_o), 0.0)
        if cu <= 0:
            continue
        cant = float(m.get("cantidad_mov") or 0)
        payload = {
            "costo_unitario": round(cu, 6),
            "costo_total": round(cu * cant, 4) if cant else 0.0,
        }
        cod_mov = (m.get("cod_mov") or "").strip()
        print(f"  {cod_mov} {tipo} MP {nk} @ {bod}: +costo {cu}")
        if args.produccion:
            sb.table("mov_inventario").update(payload).eq("cod_mov", cod_mov).execute()
        actualizados += 1

    print(f"\nMovimientos a corregir: {actualizados}")
    if args.produccion and actualizados:
        print("Ejecuta: python recalcular_stock_sheets.py --produccion --solo-costo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
