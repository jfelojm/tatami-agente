"""
Revierte ENTRADA blanco Silk & Spice mal mapeada a MP-223 (tinto).

Acciones:
  1. AJUSTE_NEGATIVO -2 uni en MP-223 BOD-002
  2. Marca mov ENTRADA original como revertido
  3. Desactiva item 10113748 (blanco → tinto) en BD_ITEMS_PROV
  4. Recalcula stock MP-223

Uso:
  python revertir_entrada_blanco_mp223.py --dry-run
  python revertir_entrada_blanco_mp223.py --produccion
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

COD_MOV_ENTRADA = "MOV-20260610-223-20260611092350545"
ITEM_PROV_BLANCO = "10113748"
QTY = 2.0
CU = 15.22
BOD = "BOD-002"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    ent = (
        sb.table("mov_inventario")
        .select("*")
        .eq("cod_mov", COD_MOV_ENTRADA)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not ent:
        print(f"ERROR: no existe {COD_MOV_ENTRADA}")
        return 1
    e = ent[0]
    print(
        f"ENTRADA a revertir: +{e['cantidad_mov']} uni MP-223 "
        f"({e.get('observaciones','')[:60]}...)"
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]
    cod_aj = f"MOV-REVERT-blanco223-{ts}"
    ajuste = {
        "cod_mov": cod_aj,
        "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "tipo_mov": "AJUSTE_NEGATIVO",
        "cod_mp_sistema": "223",
        "nombre_mp": e.get("nombre_mp") or "Vino tinto Silk and spice",
        "cod_bodega_origen": BOD,
        "cod_bodega_destino": None,
        "cantidad_mov": QTY,
        "unidad_base": "uni",
        "costo_unitario": CU,
        "costo_total": round(QTY * CU, 4),
        "origen_documento": "AJUSTE_MANUAL",
        "num_documento": e.get("num_documento") or "",
        "registrado_por": "AGENTE",
        "observaciones": (
            f"Revierte ENTRADA blanco mal mapeado a MP-223 tinto | "
            f"ref={COD_MOV_ENTRADA} | SOGRAPE VINO BLANCO SILK & SPICE"
        ),
    }
    print(f"AJUSTE: -{QTY} uni @ {CU} -> {cod_aj}")

    if args.produccion:
        sb.table("mov_inventario").insert(ajuste).execute()
        sb.table("mov_inventario").update(
            {
                "observaciones": (
                    (e.get("observaciones") or "")
                    + f" | REVERTIDO por {cod_aj} (blanco≠tinto)"
                ).strip(" |"),
            }
        ).eq("cod_mov", COD_MOV_ENTRADA).execute()
        print("Ajuste insertado y ENTRADA marcada REVERTIDO.")

        import gspread
        from google.oauth2.service_account import Credentials
        from gspread.utils import ValueInputOption, rowcol_to_a1

        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_CREDENTIALS_PATH"],
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
        ws = sh.worksheet("BD_ITEMS_PROV")
        vals = ws.get_all_values()
        hi = next(i for i, r in enumerate(vals) if "cod_item_prov" in r)
        h = [(c or "").strip() for c in vals[hi]]
        ic = h.index("cod_item_prov")
        ia = h.index("activo") if "activo" in h else None
        for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
            if (row[ic] if ic < len(row) else "").strip() != ITEM_PROV_BLANCO:
                continue
            if ia is not None:
                ws.batch_update(
                    [{"range": rowcol_to_a1(i, ia + 1), "values": [["NO"]]}],
                    value_input_option=ValueInputOption.user_entered,
                )
                print(f"BD_ITEMS_PROV {ITEM_PROV_BLANCO}: activo=NO (blanco ≠ MP-223 tinto)")
            break

        subprocess.run(
            [sys.executable, "recalcular_stock_sheets.py", "--produccion", "--cod-mp", "223"],
            cwd=os.path.dirname(__file__) or ".",
            check=True,
        )
    else:
        print("[DRY-RUN]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
