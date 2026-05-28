"""
Detecta filas BD_ITEMS_PROV donde precio_ref interpretado >> BD_MP_SISTEMA.
Causa típica: precio de bulto en columna precio_ref sin dividir por factor.

Uso:
  python auditar_precio_ref_vs_mp.py
  python auditar_precio_ref_vs_mp.py --csv logs/precio_ref_sospechosos.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", metavar="PATH")
    p.add_argument("--ratio", type=float, default=5.0, help="Alerta si cu_prov > cu_mp × ratio")
    args = p.parse_args()

    import gspread
    from google.oauth2.service_account import Credentials

    from costo_mp_canonico import cargar_costo_desde_bd_mp, norm_mp
    from numeros_sheets import parse_numero_sheets, precio_ref_a_unidad_base

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    hoja_mp = cargar_costo_desde_bd_mp(sh)
    mp_min: dict[str, float] = {}
    for (mp, _bod), cu in hoja_mp.items():
        if cu <= 0:
            continue
        nk = norm_mp(mp)
        mp_min[nk] = min(mp_min.get(nk, cu), cu)

    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    h = [(c or "").strip() for c in vals[hi]]
    icod = h.index("cod_mp_sistema")
    ip = h.index("precio_ref")
    ifac = h.index("factor_conversion")
    inom = h.index("nombre_item_prov") if "nombre_item_prov" in h else h.index("descripcion_proveedor")
    ia = h.index("activo") if "activo" in h else None
    irow = h.index("cod_item_prov") if "cod_item_prov" in h else None

    filas = []
    for ri, row in enumerate(vals[hi + 1 :], start=hi + 2):
        if ia is not None and ia < len(row) and (row[ia] or "").strip().upper() == "NO":
            continue
        cod = norm_mp(row[icod] if icod < len(row) else "")
        if not cod:
            continue
        pr = parse_numero_sheets(row[ip] if ip < len(row) else 0)
        fac = parse_numero_sheets(row[ifac] if ifac < len(row) else 0, 0)
        if pr <= 0:
            continue
        cu_raw = precio_ref_a_unidad_base(pr, fac) if fac > 0 else pr
        cu_div = round(pr / fac, 6) if fac > 1 else pr
        cu_mp = mp_min.get(cod, 0.0)
        ratio = (cu_raw / cu_mp) if cu_mp > 0 else 0.0
        sospechoso = cu_mp > 0 and cu_raw > cu_mp * args.ratio
        if not sospechoso and cu_mp > 0 and cu_div < cu_raw * 0.5 and cu_div <= cu_mp * 2:
            # precio_ref parece bulto: dividir arreglaría
            if pr > 0 and fac > 1 and cu_div < cu_mp * 2:
                sospechoso = cu_raw > cu_mp * args.ratio or cu_raw > 0.05
        if sospechoso or (cu_raw > 0.05 and cu_mp > 0 and cu_mp < 0.01):
            filas.append(
                {
                    "fila": ri,
                    "cod_mp": cod,
                    "cod_item_prov": row[irow] if irow is not None and irow < len(row) else "",
                    "nombre": (row[inom] if inom < len(row) else "")[:50],
                    "precio_ref": pr,
                    "factor": fac,
                    "cu_interpretado": cu_raw,
                    "cu_pr_div_fac": cu_div,
                    "cu_bd_mp": cu_mp,
                    "ratio": round(ratio, 1) if ratio else "",
                }
            )

    por_mp: dict[str, list] = defaultdict(list)
    for f in filas:
        por_mp[f["cod_mp"]].append(f)

    print("=== precio_ref sospechoso vs BD_MP ===\n")
    print(f"Filas ítem prov alertadas: {len(filas)}")
    print(f"MPs distintos: {len(por_mp)}\n")
    for cod in sorted(por_mp, key=lambda c: (int(c) if c.isdigit() else 9999, c))[:30]:
        f0 = por_mp[cod][0]
        print(
            f"  MP {cod}: cu_interp={f0['cu_interpretado']:.6f} "
            f"cu_div={f0['cu_pr_div_fac']:.6f} BD_MP={f0['cu_bd_mp']:.6f} "
            f"({len(por_mp[cod])} ítems)"
        )

    if args.csv and filas:
        path = args.csv
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as out:
            w = csv.DictWriter(out, fieldnames=list(filas[0].keys()))
            w.writeheader()
            w.writerows(filas)
        print(f"\nCSV: {path}")

    return 1 if filas else 0


if __name__ == "__main__":
    raise SystemExit(main())
