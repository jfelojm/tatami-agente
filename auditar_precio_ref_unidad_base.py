"""
Lista ítems BD_ITEMS_PROV cuyo precio_ref parece precio de compra (no USD/unidad_base).
"""

from __future__ import annotations

import csv
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)


def main():
    import gspread
    from google.oauth2.service_account import Credentials
    from procesar_facturas_drive import _precio_ref_unidad_base
    from recalcular_stock_sheets import _precio_unitario_desde_items_prov

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = next(
        i
        for i, r in enumerate(vals)
        if any((c or "").strip() == "cod_item_prov" for c in r)
    )
    headers = [(c or "").strip() for c in vals[hi]]
    idx = {h: headers.index(h) for h in headers}

    rows_out = []
    for row in vals[hi + 1 :]:
        ub = (row[idx["unidad_base_sistema"]] if idx["unidad_base_sistema"] < len(row) else "").strip().lower()
        if ub not in ("gr", "ml"):
            continue
        try:
            precio = float((row[idx["precio_ref"]] if idx["precio_ref"] < len(row) else "").replace(",", "."))
            fac = float((row[idx["factor_conversion"]] if idx["factor_conversion"] < len(row) else "1").replace(",", "."))
        except ValueError:
            continue
        if precio <= 0 or fac <= 1:
            continue
        cu_lectura = _precio_unitario_desde_items_prov(precio, fac)
        cu_correcto = _precio_ref_unidad_base({"factor_conversion": str(fac)}, precio)
        # Sospecha: lectura no dividió pero debería (precio de pack)
        if precio > 0.5 and abs(cu_lectura - precio) < 0.001 and cu_correcto < precio * 0.5:
            rows_out.append(
                {
                    "cod_item_prov": row[idx["cod_item_prov"]] if idx["cod_item_prov"] < len(row) else "",
                    "cod_mp": row[idx["cod_mp_sistema"]] if idx["cod_mp_sistema"] < len(row) else "",
                    "descripcion": row[idx["descripcion_proveedor"]] if idx["descripcion_proveedor"] < len(row) else "",
                    "precio_ref_actual": precio,
                    "factor": fac,
                    "precio_ref_deberia": cu_correcto,
                    "unidad_base": ub,
                }
            )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"auditar_precio_ref_pack_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()) if rows_out else [])
        if rows_out:
            w.writeheader()
            w.writerows(rows_out)
    print(f"Ítems con precio_ref sin dividir (gr/ml, factor>1): {len(rows_out)}")
    print(f"CSV: {path}")
    for r in rows_out[:15]:
        print(r)


if __name__ == "__main__":
    main()
