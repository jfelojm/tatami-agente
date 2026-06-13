"""
Candados de costos MP — detecta anomalías antes de que corrompan recetas.

Ejecutar en pipeline o manualmente después de recalcular_stock / sync costos:
  python guardias_costos_mp.py
  python guardias_costos_mp.py --strict   # exit code 1 si hay alertas graves

Alertas:
  - ratio_hoja_vs_prov >10× o <0.1× (MP con catálogo)
  - licor ml con costo <0.01 USD/ml (botella <$7.50)
  - licor ml con costo >0.30 USD/ml sin catálogo
  - recetas usarían costo ~1000× menor que catálogo (prov_pack bug)
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"

# MPs licores barra monitoreados (expandible)
MPS_LICOR_ML = frozenset(
    {"165", "299", "564", "229", "227", "565", "226", "240", "176", "180", "181"}
)


def auditar(*, strict: bool = False) -> tuple[list[dict], int]:
    import gspread
    from google.oauth2.service_account import Credentials

    from costo_mp_canonico import (
        _elegir_costo_mp_final,
        cargar_costo_desde_bd_mp,
        cargar_costo_desde_items_prov,
        norm_mp,
    )
    from numeros_sheets import parse_numero_sheets

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    prov = cargar_costo_desde_items_prov(sh)
    bd = cargar_costo_desde_bd_mp(sh)

    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    h = [(c or "").strip() for c in vals[hi]]
    ic, ib, icu, iu = (
        h.index("cod_mp_sistema"),
        h.index("cod_bodega"),
        h.index("costo_unitario_ref"),
        h.index("unidad_base"),
    )
    inom = h.index("nombre_mp") if "nombre_mp" in h else None

    alertas: list[dict] = []
    vistos: set[str] = set()

    for row in vals[hi + 1 :]:
        cod = norm_mp(row[ic] if ic < len(row) else "")
        if not cod or cod in vistos:
            continue
        vistos.add(cod)
        uni = (row[iu] if iu < len(row) else "").strip().lower()
        cu_h = parse_numero_sheets(row[icu] if icu < len(row) else 0)
        cu_p = prov.get(cod, 0.0)
        bod = row[ib] if ib < len(row) else ""
        nom = row[inom] if inom is not None and inom < len(row) else ""

        if cu_p > 0 and cu_h > 0:
            ratio = cu_h / cu_p
            if ratio < 0.1 or ratio > 10:
                cu_rec, nota = _elegir_costo_mp_final(cu_p, cu_h)
                sev = "CRITICO" if ratio < 0.01 or ratio > 100 else "ALTO"
                alertas.append(
                    {
                        "severidad": sev,
                        "cod_mp": cod,
                        "nombre": nom,
                        "bodega": bod,
                        "unidad": uni,
                        "costo_hoja": cu_h,
                        "costo_prov": cu_p,
                        "ratio": round(ratio, 4),
                        "costo_recetas": cu_rec,
                        "nota": nota or "",
                        "tipo": "ratio_hoja_vs_prov",
                    }
                )

        if uni == "ml" and cod in MPS_LICOR_ML and cu_h > 0:
            if cu_h < 0.01:
                alertas.append(
                    {
                        "severidad": "CRITICO",
                        "cod_mp": cod,
                        "nombre": nom,
                        "bodega": bod,
                        "unidad": uni,
                        "costo_hoja": cu_h,
                        "costo_prov": cu_p,
                        "ratio": 0,
                        "costo_recetas": cu_h,
                        "nota": f"botella_750ml=${cu_h * 750:.2f}",
                        "tipo": "licor_ml_muy_bajo",
                    }
                )
            elif cu_h > 0.30 and not cu_p:
                alertas.append(
                    {
                        "severidad": "ALTO",
                        "cod_mp": cod,
                        "nombre": nom,
                        "bodega": bod,
                        "unidad": uni,
                        "costo_hoja": cu_h,
                        "costo_prov": cu_p,
                        "ratio": 0,
                        "costo_recetas": cu_h,
                        "nota": "sin catalogo y cu alto",
                        "tipo": "licor_ml_muy_alto",
                    }
                )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = LOGS / f"guardias_costos_mp_{ts}.csv"
    if alertas:
        LOGS.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(alertas[0].keys()))
            w.writeheader()
            w.writerows(alertas)

    crit = sum(1 for a in alertas if a["severidad"] == "CRITICO")
    alt = sum(1 for a in alertas if a["severidad"] == "ALTO")
    print(f"Alertas: {len(alertas)} (CRITICO={crit}, ALTO={alt})")
    for a in alertas[:20]:
        print(
            f"  [{a['severidad']}] MP {a['cod_mp']} {a['nombre'][:25]:25} "
            f"hoja={a['costo_hoja']:.6f} prov={a['costo_prov']:.6f} "
            f"ratio={a['ratio']} {a['tipo']}"
        )
    if alertas:
        print(f"CSV: {out}")

    rc = 0
    if strict and crit:
        rc = 1
    return alertas, rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strict", action="store_true", help="Exit 1 si hay alertas CRITICO")
    args = p.parse_args()
    _, rc = auditar(strict=args.strict)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
