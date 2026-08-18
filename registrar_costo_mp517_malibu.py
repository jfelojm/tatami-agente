"""
Registries costo de referencia MP 517 Malibu ron de coco (compra sin factura).

Precio: USD 15,83 / botella 750 ml → 0,021107 USD/ml en catálogo y maestro.

Uso:
  python registrar_costo_mp517_malibu.py
  python registrar_costo_mp517_malibu.py --produccion
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from dotenv import load_dotenv
from google_credentials import google_credentials

load_dotenv(override=True)

PRECIO_BOTELLA = 15.83
FACTOR_ML = 750
PRECIO_REF = round(PRECIO_BOTELLA / FACTOR_ML, 6)
MP = "517"
COD_PROV = "001"
NOM_PROV = "FLOR MARIA SALAZAR GONZALEZ"
COD_ITEM = "MALIBU-517-REF"


def main() -> int:
    import gspread
    from gspread.utils import ValueInputOption, rowcol_to_a1

    from costo_mp_canonico import norm_mp
    from numeros_sheets import parse_numero_sheets

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    tag = "PRODUCCION" if args.produccion else "DRY-RUN"
    print(f"=== MP {MP} Malibu - {PRECIO_BOTELLA} USD/bot -> {PRECIO_REF} USD/ml ({tag}) ===\n")

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])

    ws_ip = sh.worksheet("BD_ITEMS_PROV")
    rows = ws_ip.get_all_values()
    hi = next(i for i, r in enumerate(rows) if "cod_mp_sistema" in [c.strip() for c in r])
    hdr = [h.strip() for h in rows[hi]]
    ci = {h: i for i, h in enumerate(hdr)}

    found_row = None
    for i, r in enumerate(rows[hi + 1 :], start=hi + 2):
        if norm_mp(r[ci["cod_mp_sistema"]] if ci["cod_mp_sistema"] < len(r) else "") == MP:
            found_row = i
            break

    data = {
        "nombre_proveedor": NOM_PROV,
        "cod_proveedor": COD_PROV,
        "cod_item_prov": COD_ITEM,
        "descripcion_proveedor": "Malibu ron de coco 750 ml (ref costo sin factura)",
        "nombre_mp": "Malibu ron de coco",
        "cod_mp_sistema": MP,
        "unidad_compra": "uni",
        "factor_conversion": str(FACTOR_ML),
        "unidad_base_sistema": "ml",
        "cod_bodega_destino": "BOD-002",
        "activo": "SI",
        "precio_ref": PRECIO_REF,
        "precio_unitario_xml": PRECIO_BOTELLA,
        "fecha_precio_ref": date.today().isoformat(),
    }

    if found_row:
        print(f"BD_ITEMS_PROV: actualizar fila {found_row}")
        if args.produccion:
            upd = [
                {
                    "range": rowcol_to_a1(found_row, ci[col] + 1),
                    "values": [[val]],
                }
                for col, val in data.items()
                if col in ci
            ]
            ws_ip.batch_update(upd, value_input_option=ValueInputOption.user_entered)
    else:
        print("BD_ITEMS_PROV: append fila nueva")
        if args.produccion:
            fila = [""] * len(hdr)
            for k, v in data.items():
                if k in ci:
                    fila[ci[k]] = v
            ws_ip.append_row(fila, value_input_option=ValueInputOption.user_entered)

    ws_mp = sh.worksheet("BD_MP_SISTEMA")
    vals = ws_mp.get_all_values()
    hi2 = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    h2 = [(c or "").strip() for c in vals[hi2]]
    ic = h2.index("cod_mp_sistema")
    icu = h2.index("costo_unitario_ref")
    for i, row in enumerate(vals[hi2 + 1 :], start=hi2 + 2):
        if norm_mp(row[ic] if ic < len(row) else "") != MP:
            continue
        old = parse_numero_sheets(row[icu] if icu < len(row) else 0)
        print(f"BD_MP_SISTEMA: {old} -> {PRECIO_REF} USD/ml")
        if args.produccion:
            ws_mp.batch_update(
                [{"range": rowcol_to_a1(i, icu + 1), "values": [[PRECIO_REF]]}],
                value_input_option=ValueInputOption.user_entered,
            )
        break

    if not args.produccion:
        print("\nEjecuta con --produccion para escribir.")
        return 0

    print("\nRecalculando costos en recetas y subrecetas...")
    import subprocess
    import sys

    for script in ("calcular_costo_subrecetas.py", "calcular_costo_recetas.py"):
        subprocess.run([sys.executable, script, "--produccion"], check=True)
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
