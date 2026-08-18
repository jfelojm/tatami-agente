"""
Lomo Kuro (sm 12 / rec 146): variedades papas | arroz (mismo precio).

  papas  → base + 200 g MP 120 Papa Super Chola
  arroz  → base + 200 g MP 096 Arroz japonés
  Sin default: POS debe mandar ARROZ o PAPAS.

Uso:
  python aplicar_variedades_lomo_kuro.py --dry-run
  python aplicar_variedades_lomo_kuro.py --produccion
"""
from __future__ import annotations

import argparse
import sys
import time
from copy import deepcopy

from dotenv import load_dotenv
from gspread.utils import ValueInputOption

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(override=True)

COD_RECETA = "146"
COD_SM = "12"
VAR_PAPAS = "papas"
VAR_ARROZ = "arroz"
MP_PAPA = "120"
MP_ARROZ = "096"
NOM_ARROZ = "ARROZ JAPONES"
GR_ACOMP = "200"
BOD = "BOD-001"


def _pad_mp(c: str) -> str:
    """No rellenar ceros: en BD_MP hay códigos como '87' (no '087')."""
    return (c or "").strip()


def _norm_rec(c: str) -> str:
    s = (c or "").strip()
    return str(int(s)) if s.isdigit() else s


def _headers(vals: list[list[str]]) -> tuple[int, list[str]]:
    for i, row in enumerate(vals[:8]):
        h = [c.strip() for c in row]
        if "cod_receta" in h or "cod_smart_menu" in h:
            return i, h
    raise RuntimeError("sin header")


def _row_dict(headers: list[str], row: list[str]) -> dict[str, str]:
    return {
        headers[j]: (row[j].strip() if j < len(row) else "")
        for j in range(len(headers))
    }


def _dict_to_row(headers: list[str], d: dict[str, str], width: int) -> list[str]:
    out = [""] * max(width, len(headers))
    for j, h in enumerate(headers):
        out[j] = d.get(h, "")
    return out


def _clear_costos(d: dict[str, str]) -> None:
    for col in ("costo_unitario", "costo_linea", "nota_costo"):
        if col in d:
            d[col] = ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--produccion", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not args.produccion:
        print("Indica --dry-run o --produccion")
        return 2
    dry = not args.produccion

    from procesar_facturas_drive import _get_sheet

    sh = _get_sheet()
    print("=" * 70)
    print(f"LOMO KURO variedades papas/arroz — {'DRY RUN' if dry else 'PRODUCCION'}")
    print("=" * 70)

    ws_d = sh.worksheet("BD_RECETAS_DETALLE")
    vals_d = ws_d.get_all_values()
    hi_d, hdr_d = _headers(vals_d)
    width_d = max(len(r) for r in vals_d)

    base_rows: list[tuple[int, dict[str, str]]] = []  # sheet_row, dict
    papa_row: tuple[int, dict[str, str]] | None = None

    for i, row in enumerate(vals_d[hi_d + 1 :], start=hi_d + 2):
        if not any(c.strip() for c in row):
            continue
        d = _row_dict(hdr_d, row)
        if _norm_rec(d.get("cod_receta", "")) != _norm_rec(COD_RECETA):
            continue
        var = (d.get("variedad_smart_menu") or "").strip().lower()
        if var and var not in (VAR_PAPAS, VAR_ARROZ):
            print(f"  WARN fila {i}: variedad inesperada {var!r} — se ignora para base")
            continue
        if var == VAR_ARROZ:
            print(f"  SKIP ya existe arroz fila {i}")
            continue
        mp = _pad_mp(d.get("cod_mp_sistema", ""))
        if mp == MP_PAPA:
            papa_row = (i, d)
        else:
            base_rows.append((i, d))

    if not base_rows:
        print("ERROR: sin líneas base para receta 146")
        return 1
    if papa_row is None:
        print("ERROR: no está la línea papa MP 120 en receta 146")
        return 1

    print(f"\n[1] Base (sin papa): {len(base_rows)} líneas")
    print(f"  Papa actual fila {papa_row[0]}: {papa_row[1].get('cantidad')} g → {GR_ACOMP} g")

    # Patches papas: todas las filas actuales vacías/papas → papas; papa qty 200
    patches_detalle: list[tuple[int, int, str]] = []  # row, col_1based, value
    col_var = hdr_d.index("variedad_smart_menu") + 1
    col_cant = hdr_d.index("cantidad") + 1
    col_mp = hdr_d.index("cod_mp_sistema") + 1

    for sheet_row, d in base_rows:
        if (d.get("variedad_smart_menu") or "").strip().lower() != VAR_PAPAS:
            patches_detalle.append((sheet_row, col_var, VAR_PAPAS))
        mp = (d.get("cod_mp_sistema") or "").strip()
        padded = _pad_mp(mp)
        if mp and padded != mp:
            patches_detalle.append((sheet_row, col_mp, padded))

    pr, pd = papa_row
    if (pd.get("variedad_smart_menu") or "").strip().lower() != VAR_PAPAS:
        patches_detalle.append((pr, col_var, VAR_PAPAS))
    if (pd.get("cantidad") or "").strip().replace(",", ".") != GR_ACOMP:
        patches_detalle.append((pr, col_cant, GR_ACOMP))
    if _pad_mp(pd.get("cod_mp_sistema", "")) != (pd.get("cod_mp_sistema") or "").strip():
        patches_detalle.append((pr, col_mp, MP_PAPA))

    # Nuevas filas arroz = base + línea arroz
    nuevas_detalle: list[list[str]] = []
    for _sr, ln in base_rows:
        q = deepcopy(ln)
        q["variedad_smart_menu"] = VAR_ARROZ
        q["cod_receta"] = (ln.get("cod_receta") or COD_RECETA).strip()
        if q.get("cod_mp_sistema"):
            q["cod_mp_sistema"] = _pad_mp(q["cod_mp_sistema"])
        _clear_costos(q)
        nuevas_detalle.append(_dict_to_row(hdr_d, q, width_d))

    arroz = {h: "" for h in hdr_d}
    arroz["nombre_receta"] = "LOMO KURO"
    arroz["cod_receta"] = (base_rows[0][1].get("cod_receta") or COD_RECETA).strip()
    arroz["variedad_smart_menu"] = VAR_ARROZ
    arroz["nombre_mp"] = NOM_ARROZ
    arroz["cod_mp_sistema"] = MP_ARROZ
    arroz["cantidad"] = GR_ACOMP
    arroz["unidad_base"] = "gr"
    arroz["cod_bodega"] = BOD
    arroz["merma_pct"] = "0"
    arroz["es_opcional"] = "NO"
    arroz["pct_aplicacion"] = "1"
    nuevas_detalle.append(_dict_to_row(hdr_d, arroz, width_d))

    print(f"  Patches detalle (papas/qty/pad): {len(patches_detalle)}")
    print(f"  Nuevas filas arroz: {len(nuevas_detalle)}")

    # ── PRODUCTOS ────────────────────────────────────────────
    ws_p = sh.worksheet("BD_PRODUCTOS")
    vals_p = ws_p.get_all_values()
    hi_p, hdr_p = _headers(vals_p)
    width_p = max(len(r) for r in vals_p)

    prod_base: dict[str, str] | None = None
    prod_row_idx: int | None = None
    tiene_arroz = False
    tiene_papas = False

    for i, row in enumerate(vals_p[hi_p + 1 :], start=hi_p + 2):
        if not any(c.strip() for c in row):
            continue
        d = _row_dict(hdr_p, row)
        if (d.get("cod_smart_menu") or "").strip() != COD_SM:
            continue
        var = (d.get("variedad_smart_menu") or "").strip().lower()
        if var == VAR_ARROZ:
            tiene_arroz = True
        elif var == VAR_PAPAS:
            tiene_papas = True
            prod_base = d
            prod_row_idx = i
        elif var == "" and prod_base is None:
            prod_base = d
            prod_row_idx = i

    if prod_base is None or prod_row_idx is None:
        print("ERROR: no hay producto sm=12 base")
        return 1

    print("\n[2] Producto base:")
    print(
        f"  sm={COD_SM} {prod_base.get('nombre_producto')} "
        f"precio={prod_base.get('precio_venta')} var={prod_base.get('variedad_smart_menu')!r}"
    )

    patches_prod: list[tuple[int, int, str]] = []
    col_var_p = hdr_p.index("variedad_smart_menu") + 1
    if not tiene_papas and (prod_base.get("variedad_smart_menu") or "").strip() == "":
        patches_prod.append((prod_row_idx, col_var_p, VAR_PAPAS))

    nuevas_prod: list[list[str]] = []
    if not tiene_arroz:
        p = deepcopy(prod_base)
        p["variedad_smart_menu"] = VAR_ARROZ
        # precio igual
        nuevas_prod.append(_dict_to_row(hdr_p, p, width_p))

    print(f"  Patch productos → papas: {len(patches_prod)}")
    print(f"  Nuevo producto arroz: {len(nuevas_prod)}")
    if tiene_arroz:
        print("  (producto arroz ya existía)")

    if dry:
        print("\n[dry-run] No se escribió nada.")
        print("  Ej. patch detalle:", patches_detalle[:5], "...")
        print("  Ej. nueva arroz MP:", nuevas_detalle[-1][:10])
        if nuevas_prod:
            print("  Ej. prod arroz:", nuevas_prod[0][:8])
        return 0

    print("\n[3] Escribiendo Sheets...")
    from gspread import Cell

    cells_d = [Cell(row=r, col=c, value=v) for r, c, v in patches_detalle]
    if cells_d:
        ws_d.update_cells(cells_d, value_input_option=ValueInputOption.raw)
        print(f"  Detalle patches: {len(cells_d)}")
        time.sleep(0.5)

    if nuevas_detalle:
        ws_d.append_rows(nuevas_detalle, value_input_option=ValueInputOption.raw)
        print(f"  Detalle append arroz: {len(nuevas_detalle)}")
        time.sleep(1)

    cells_p = [Cell(row=r, col=c, value=v) for r, c, v in patches_prod]
    if cells_p:
        ws_p.update_cells(cells_p, value_input_option=ValueInputOption.raw)
        print(f"  Productos patches: {len(cells_p)}")
        time.sleep(0.3)

    if nuevas_prod:
        ws_p.append_rows(nuevas_prod, value_input_option=ValueInputOption.raw)
        print(f"  Productos append: {len(nuevas_prod)}")

    print("\nOK. Siguiente: calcular_costo_recetas.py --produccion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
