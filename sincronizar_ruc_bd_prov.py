"""
Completa RUC vacíos en BD_PROV usando facturas_procesadas (ruc_proveedor + meta).

Uso:
  python sincronizar_ruc_bd_prov.py --dry-run
  python sincronizar_ruc_bd_prov.py --produccion
"""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict

from dotenv import load_dotenv
from google_credentials import google_credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

load_dotenv(override=True)


def _norm_name(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cargar_ruc_por_nombre() -> dict[str, set[str]]:
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    res = (
        sb.table("facturas_procesadas")
        .select("ruc_proveedor,meta")
        .order("fecha_factura", desc=True)
        .limit(5000)
        .execute()
    )
    out: dict[str, set[str]] = defaultdict(set)
    for row in res.data or []:
        ruc = (row.get("ruc_proveedor") or "").strip()
        if not ruc:
            continue
        meta = row.get("meta") or {}
        nombre = ""
        if isinstance(meta, dict):
            nombre = (meta.get("razon_social") or meta.get("proveedor") or "").strip()
        if nombre:
            out[_norm_name(nombre)].add(ruc)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    import gspread

    ruc_por_nombre = _cargar_ruc_por_nombre()
    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_PROV")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_proveedor" in r)
    hdr = [(c or "").strip() for c in vals[hi]]
    ci = {h: i for i, h in enumerate(hdr)}
    iruc = ci.get("RUC")
    irazon = ci.get("razon_social")
    if iruc is None or irazon is None:
        print("ERROR: BD_PROV sin columnas RUC / razon_social")
        return 1

    updates: list[tuple[int, str, str]] = []
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        razon = row[irazon].strip() if irazon < len(row) else ""
        ruc = row[iruc].strip() if iruc < len(row) else ""
        if not razon or ruc:
            continue
        candidatos = ruc_por_nombre.get(_norm_name(razon), set())
        if len(candidatos) == 1:
            updates.append((i, razon, next(iter(candidatos))))
        elif len(candidatos) > 1:
            print(f"SKIP {razon}: varios RUC {sorted(candidatos)}")

    if not updates:
        print("Nada que actualizar: todos los proveedores tienen RUC o sin candidato único.")
        return 0

    print(f"Filas a completar: {len(updates)}")
    for row_no, razon, ruc in updates:
        print(f"  fila {row_no}: {razon} -> RUC {ruc}")

    if args.produccion:
        batch = [
            {"range": rowcol_to_a1(row_no, iruc + 1), "values": [[ruc]]}
            for row_no, _, ruc in updates
        ]
        ws.batch_update(batch, value_input_option=ValueInputOption.user_entered)
        print("BD_PROV actualizado.")
    else:
        print("[DRY-RUN]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
