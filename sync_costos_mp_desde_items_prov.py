"""
Sincroniza BD_MP_SISTEMA.costo_unitario_ref desde BD_ITEMS_PROV (precio_ref ÷ factor).

Corrige MPs donde el maestro tiene precio de pack como USD/gr (col, repollo, almidón).

Uso:
  python sync_costos_mp_desde_items_prov.py --dry-run
  python sync_costos_mp_desde_items_prov.py --produccion
  python sync_costos_mp_desde_items_prov.py --produccion --mp 006 028 029
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)


def main():
    import gspread
    from google.oauth2.service_account import Credentials
    from gspread.utils import ValueInputOption, rowcol_to_a1
    from numeros_sheets import parse_numero_sheets

    from costo_mp_canonico import cargar_costo_desde_items_prov, norm_mp

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    p.add_argument("--mp", nargs="*", help="Solo estos cod_mp (ej. 006 028)")
    p.add_argument(
        "--tolerancia-rel",
        type=float,
        default=0.02,
        help="Solo actualizar si diferencia relativa > esto (default 2%%)",
    )
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        sys.exit(2)

    filt = {norm_mp(m) for m in args.mp} if args.mp else None

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    prov = cargar_costo_desde_items_prov(sh)

    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    h = [(c or "").strip() for c in vals[hi]]
    ic, ib, icu = h.index("cod_mp_sistema"), h.index("cod_bodega"), h.index("costo_unitario_ref")

    updates = []
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod = norm_mp(row[ic] if ic < len(row) else "")
        if not cod:
            continue
        if filt and cod not in filt:
            continue
        cu_ok = prov.get(cod)
        if not cu_ok:
            continue
        cu_old = parse_numero_sheets(row[icu] if icu < len(row) else 0)
        if abs(cu_old - cu_ok) <= 1e-6:
            continue
        if cu_old > 0 and cu_ok > 0:
            rel = abs(cu_old - cu_ok) / max(cu_ok, 1e-9)
            if rel <= args.tolerancia_rel:
                continue
        bod = row[ib] if ib < len(row) else ""
        print(f"  MP {cod} @ {bod}: {cu_old} -> {cu_ok}")
        updates.append({"range": rowcol_to_a1(i, icu + 1), "values": [[cu_ok]]})

    # MP sin fila en catálogo: heredar costo de otra bodega (p. ej. BOD-002 → BOD-003).
    from recalcular_stock_sheets import _build_costo_ref_por_mp_desde_hoja

    hermano = _build_costo_ref_por_mp_desde_hoja(
        vals[hi + 1 :],
        col_cod=ic,
        col_bod=ib,
        col_costo=icu,
    )
    ya = {u["range"] for u in updates}
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod = norm_mp(row[ic] if ic < len(row) else "")
        if not cod:
            continue
        if filt and cod not in filt:
            continue
        if prov.get(cod):
            continue
        cu_ok = hermano.get(cod)
        if not cu_ok:
            continue
        cu_old = parse_numero_sheets(row[icu] if icu < len(row) else 0)
        if abs(cu_old - cu_ok) <= 1e-6:
            continue
        rng = rowcol_to_a1(i, icu + 1)
        if rng in ya:
            continue
        bod = row[ib] if ib < len(row) else ""
        print(f"  MP {cod} @ {bod}: {cu_old} -> {cu_ok} (heredado otra bodega)")
        updates.append({"range": rng, "values": [[cu_ok]]})
        ya.add(rng)

    print(f"\nCeldas a actualizar: {len(updates)}")
    if args.produccion and updates:
        for j in range(0, len(updates), 50):
            ws.batch_update(updates[j : j + 50], value_input_option=ValueInputOption.user_entered)
        print("Escrito en BD_MP_SISTEMA.")
    elif args.dry_run:
        print("[DRY-RUN]")


if __name__ == "__main__":
    main()
