"""
Audita cod_mp_sistema duplicados en BD_MP_SISTEMA (mismo código, nombres distintos).

Uso:
  python auditar_cod_mp_duplicados.py
  python auditar_cod_mp_duplicados.py --export exports/dups_mp.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from staging_common import find_header_row, open_master

load_dotenv(override=True)


def auditar() -> list[dict]:
    ws = open_master().worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_mp_sistema")
    if hi is None:
        raise RuntimeError("BD_MP_SISTEMA sin cabecera cod_mp_sistema")
    h = [(c or "").strip() for c in vals[hi]]
    icod = h.index("cod_mp_sistema")
    inom = h.index("nombre_mp") if "nombre_mp" in h else None
    ibod = h.index("cod_bodega") if "cod_bodega" in h else None

    by_cod: dict[str, list[dict]] = defaultdict(list)
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod = (row[icod] if icod < len(row) else "").strip()
        if not cod or cod.upper().startswith("SUB-"):
            continue
        nom = (row[inom] if inom is not None and inom < len(row) else "").strip()
        bod = (row[ibod] if ibod is not None and ibod < len(row) else "").strip()
        by_cod[cod].append({"fila": i, "cod_mp_sistema": cod, "nombre_mp": nom, "cod_bodega": bod})

    out: list[dict] = []
    for cod in sorted(by_cod.keys(), key=lambda c: (not c.isdigit(), int(c) if c.isdigit() else c)):
        names = {(r["nombre_mp"] or "").strip().lower() for r in by_cod[cod] if (r["nombre_mp"] or "").strip()}
        if len(names) > 1:
            for r in by_cod[cod]:
                out.append({**r, "n_nombres_distintos": len(names), "nombres": " | ".join(sorted(names))})
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Auditar cod_mp duplicados en BD_MP_SISTEMA")
    p.add_argument("--export", type=str, default="", help="CSV de salida")
    args = p.parse_args()

    dups = auditar()
    if not dups:
        print("OK: no hay cod_mp_sistema con nombres distintos en BD_MP_SISTEMA.")
        return 0

    print(f"ALERTA: {len({d['cod_mp_sistema'] for d in dups})} codigos con nombres distintos:\n")
    by: dict[str, list[dict]] = defaultdict(list)
    for d in dups:
        by[d["cod_mp_sistema"]].append(d)
    for cod, rows in sorted(by.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 9999):
        print(f"  {cod}: {rows[0]['nombres']}")
        for r in rows:
            print(f"    fila {r['fila']} | {r['nombre_mp']} | {r['cod_bodega']}")

    if args.export:
        path = Path(args.export)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(dups[0].keys()))
            w.writeheader()
            w.writerows(dups)
        print(f"\nExport: {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
