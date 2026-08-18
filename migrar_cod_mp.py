"""
Renumerar cod_mp_sistema en hojas del maestro cuando el nombre coincide.

Uso (manzana verde 567 → 588):
  python migrar_cod_mp.py --cod-viejo 567 --cod-nuevo 588 --nombre manzana verde --dry-run
  python migrar_cod_mp.py --cod-viejo 567 --cod-nuevo 588 --nombre manzana verde --produccion
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from typing import Callable

from dotenv import load_dotenv

from staging_common import find_header_row, open_master

load_dotenv(override=True)

COD_COLS = (
    "cod_mp_sistema",
    "cod_mp",
    "cod_mp_asignado",
)


def _norm(s: str) -> str:
    t = (s or "").strip().lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t)


def _nombre_match(celda: str, patron: str) -> bool:
    if not patron:
        return True
    return _norm(patron) in _norm(celda)


def migrar(
    *,
    cod_viejo: str,
    cod_nuevo: str,
    nombre_patron: str,
    dry_run: bool,
    fila_match: Callable[[list[str], list[str], int], bool] | None = None,
) -> dict:
    sh = open_master()
    cod_v = cod_viejo.strip()
    cod_n = cod_nuevo.strip()
    cambios: list[dict] = []

    # Una sola pasada: leer metadata + valores por hoja (evita reabrir maestro por celda)
    targets = _hojas_con_cod(sh)
    for title, hi, ic, col_name in targets:
        ws = sh.worksheet(title)
        vals = ws.get_all_values()
        hdr = [(c or "").strip() for c in vals[hi]]
        inom = hdr.index("nombre_mp") if "nombre_mp" in hdr else None
        if inom is None and "nombre_subreceta" in hdr:
            inom = hdr.index("nombre_subreceta")
        idesc = hdr.index("descripcion_xml") if "descripcion_xml" in hdr else None
        updates: list[dict] = []

        for ri, row in enumerate(vals[hi + 1 :], start=hi + 2):
            if ic >= len(row):
                continue
            actual = (row[ic] or "").strip()
            if actual != cod_v:
                continue
            nom = ""
            if inom is not None and inom < len(row):
                nom = row[inom]
            elif idesc is not None and idesc < len(row):
                nom = row[idesc]
            if nombre_patron and not _nombre_match(str(nom), nombre_patron):
                if not _nombre_match(" ".join(str(x) for x in row), nombre_patron):
                    continue
            if fila_match and not fila_match(hdr, row, ri):
                continue
            updates.append({"range": f"{col_name_to_a1(ic)}{ri}", "values": [[cod_n]]})
            cambios.append(
                {
                    "hoja": title,
                    "fila": ri,
                    "col": col_name,
                    "nombre": nom,
                    "de": cod_v,
                    "a": cod_n,
                }
            )

        if updates and not dry_run:
            ws.batch_update(
                [{"range": u["range"], "values": u["values"]} for u in updates],
                value_input_option="RAW",
            )

    return {"dry_run": dry_run, "total": len(cambios), "cambios": cambios}


def _hojas_con_cod_from_spreadsheet(sh) -> list[tuple[str, int, int, str]]:
    """(titulo, header_row, col_idx, col_name) — solo hojas con columna cod MP."""
    out: list[tuple[str, int, int, str]] = []
    for ws in sh.worksheets():
        try:
            vals = ws.get_all_values()
        except Exception:
            continue
        if not vals:
            continue
        for col_name in COD_COLS:
            hi = find_header_row(vals, col_name)
            if hi is None:
                continue
            hdr = [(c or "").strip() for c in vals[hi]]
            try:
                ic = hdr.index(col_name)
            except ValueError:
                continue
            out.append((ws.title, hi, ic, col_name))
            break
    return out


def _hojas_con_cod(sh) -> list[tuple[str, int, int, str]]:
    return _hojas_con_cod_from_spreadsheet(sh)


def col_name_to_a1(col_idx: int) -> str:
    n = col_idx + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main() -> int:
    p = argparse.ArgumentParser(description="Migrar cod_mp_sistema por nombre en maestro")
    p.add_argument("--cod-viejo", required=True)
    p.add_argument("--cod-nuevo", required=True)
    p.add_argument("--nombre", default="", help="Substring del nombre (ej. manzana verde)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2

    res = migrar(
        cod_viejo=args.cod_viejo,
        cod_nuevo=args.cod_nuevo,
        nombre_patron=args.nombre,
        dry_run=args.dry_run,
    )
    modo = "DRY-RUN" if res["dry_run"] else "PRODUCCION"
    print(f"\n{migrar.__name__} {modo}: {res['total']} celdas\n")
    for c in res["cambios"]:
        print(f"  {c['hoja']} fila {c['fila']} | {c['nombre']!r} | {c['de']} -> {c['a']}")
    if res["total"] == 0:
        print("  (sin cambios — ya migrado o sin coincidencias)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
