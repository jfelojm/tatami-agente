"""
Audita BD_MP_SISTEMA.costo_unitario_ref vs mediana de BD_ITEMS_PROV (fórmula canónica).

Corrige filas donde el maestro no coincide con cargar_costo_desde_items_prov().

Uso:
  python auditar_costos_mp_prov_vs_maestro.py
  python auditar_costos_mp_prov_vs_maestro.py --produccion
  python auditar_costos_mp_prov_vs_maestro.py -o logs/auditoria_mp_prov_vs_maestro.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google_credentials import google_credentials

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"

# Tolerancia: diferencia absoluta o relativa
TOL_ABS = 1e-5
TOL_REL = 0.02  # 2%
# No auto-corregir si el ratio sugiere catálogo prov incoherente (pack mal / MP mezclado)
RATIO_MIN_AUTO = 0.25
RATIO_MAX_AUTO = 4.0


def _diffiere(cu_hoja: float, cu_esperado: float) -> bool:
    if cu_esperado <= 0:
        return False
    if cu_hoja <= 0:
        return True
    if abs(cu_hoja - cu_esperado) <= TOL_ABS:
        return False
    rel = abs(cu_hoja - cu_esperado) / max(cu_esperado, TOL_ABS)
    return rel > TOL_REL


def main() -> int:
    import gspread
    from gspread.utils import ValueInputOption, rowcol_to_a1

    from costo_mp_canonico import cargar_costo_desde_items_prov, norm_mp
    from numeros_sheets import parse_numero_sheets

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--produccion", action="store_true", help="Escribe correcciones en BD_MP_SISTEMA")
    p.add_argument("-o", "--output", default="", help="CSV de discrepancias")
    args = p.parse_args()

    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    prov = cargar_costo_desde_items_prov(sh)

    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    h = [(c or "").strip() for c in vals[hi]]
    ic = h.index("cod_mp_sistema")
    ib = h.index("cod_bodega")
    icu = h.index("costo_unitario_ref")
    inom = h.index("nombre_mp") if "nombre_mp" in h else None

    discrepancias: list[dict] = []
    revisar_manual: list[dict] = []
    updates: list[dict] = []
    ok = 0
    sin_catalogo = 0

    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod = norm_mp(row[ic] if ic < len(row) else "")
        if not cod:
            continue
        cu_esperado = prov.get(cod)
        if not cu_esperado or cu_esperado <= 0:
            sin_catalogo += 1
            continue
        cu_hoja = parse_numero_sheets(row[icu] if icu < len(row) else 0)
        if not _diffiere(cu_hoja, cu_esperado):
            ok += 1
            continue
        bod = row[ib] if ib < len(row) else ""
        nombre = row[inom] if inom is not None and inom < len(row) else ""
        ratio = (cu_hoja / cu_esperado) if cu_esperado > 0 else 0
        row_d = {
            "cod_mp": cod,
            "nombre_mp": nombre,
            "cod_bodega": bod,
            "costo_hoja": cu_hoja,
            "costo_esperado": cu_esperado,
            "ratio_hoja_vs_esperado": round(ratio, 4),
            "fila": i,
        }
        if ratio < RATIO_MIN_AUTO or ratio > RATIO_MAX_AUTO:
            revisar_manual.append(row_d)
            continue
        discrepancias.append(row_d)
        updates.append({"range": rowcol_to_a1(i, icu + 1), "values": [[cu_esperado]]})

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.output) if args.output else LOGS / f"auditoria_mp_prov_vs_maestro_{ts}.csv"
    if discrepancias:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(discrepancias[0].keys()))
            w.writeheader()
            w.writerows(discrepancias)

    print(f"MPs con catálogo prov: {len(prov)}")
    print(f"Filas maestro OK (dentro tolerancia): {ok}")
    print(f"Filas sin precio en catálogo (omitidas): {sin_catalogo}")
    print(f"Discrepancias a corregir (auto): {len(discrepancias)}")
    print(f"Revisar manual (ratio extremo, catálogo): {len(revisar_manual)}")
    if discrepancias:
        print(f"CSV: {out_path}")
        for d in discrepancias[:25]:
            print(
                f"  MP {d['cod_mp']} @ {d['cod_bodega']}: "
                f"{d['costo_hoja']:.6f} -> {d['costo_esperado']:.6f} "
                f"(ratio {d['ratio_hoja_vs_esperado']})"
            )
        if len(discrepancias) > 25:
            print(f"  ... y {len(discrepancias) - 25} más")

    if args.produccion and updates:
        for j in range(0, len(updates), 50):
            ws.batch_update(updates[j : j + 50], value_input_option=ValueInputOption.user_entered)
        print(f"Escritas {len(updates)} celdas en BD_MP_SISTEMA.")
    elif discrepancias and not args.produccion:
        print("[DRY-RUN] Usa --produccion para escribir.")

    return 1 if discrepancias and not args.produccion else 0


if __name__ == "__main__":
    raise SystemExit(main())
