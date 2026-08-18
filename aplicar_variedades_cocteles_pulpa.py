"""
Variedades cócteles: Daiquiri / Margarita / Mojito
  clasico = receta actual
  fresa/durazno/mango/maracuya = clásico + 60 ml pulpa Finestcall
  Precio: clasico igual; sabores +1 USD
  Inactiva DAIQUIRI DURAZNO (sm 570)

Uso:
  python aplicar_variedades_cocteles_pulpa.py --dry-run
  python aplicar_variedades_cocteles_pulpa.py --produccion
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

# Keys = cod_receta normalizado (sin ceros), como en BD_RECETAS_DETALLE.
RECETAS = {
    "50": {"nombre": "MARGARITA", "sm": "55"},
    "51": {"nombre": "MOJITO", "sm": "56"},
    "139": {"nombre": "DAIQUIRI", "sm": "123"},
}
# variedad POS (minúsculas) -> (cod_mp, nombre_mp)
PULPAS = {
    "durazno": ("169", "Finestcall Durazno"),
    "mango": ("172", "Finestcall Mango"),
    "fresa": ("171", "Finestcall Fresa"),
    "maracuya": ("173", "Finestcall Maracuyá"),
}
SABORES = list(PULPAS.keys())
CLASICO = "clasico"
ML_PULPA = 60.0
BOD_PULPA = "BOD-002"
SM_DAIQUIRI_DURAZNO_OLD = "570"


def _norm_rec(c: str) -> str:
    s = (c or "").strip()
    return str(int(s)) if s.isdigit() else s


def _parse_precio(v: str) -> float:
    s = (v or "").strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt_precio(x: float) -> str:
    # Mantener estilo hoja (coma decimal si era así en clásico se decide por origen)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.2f}".replace(".", ",")


def _headers(vals: list[list[str]]) -> tuple[int, list[str]]:
    for i, row in enumerate(vals[:5]):
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
    print(f"VARIIDADES COCTELES PULPA — {'DRY RUN' if dry else 'PRODUCCION'}")
    print("=" * 70)

    # ── DETALLE ──────────────────────────────────────────────
    ws_d = sh.worksheet("BD_RECETAS_DETALLE")
    vals_d = ws_d.get_all_values()
    hi_d, hdr_d = _headers(vals_d)
    width_d = max(len(r) for r in vals_d)

    # index data rows (1-based sheet rows)
    base_por_rec: dict[str, list[dict]] = {k: [] for k in RECETAS}
    rows_to_patch_clasico: list[tuple[int, dict]] = []  # sheet_row, dict

    for i, row in enumerate(vals_d[hi_d + 1 :], start=hi_d + 2):
        if not any(c.strip() for c in row):
            continue
        d = _row_dict(hdr_d, row)
        rec = _norm_rec(d.get("cod_receta", ""))
        if rec not in RECETAS:
            continue
        var = (d.get("variedad_smart_menu") or "").strip().lower()
        if var in ("", CLASICO):
            base_por_rec[rec].append(d)
            if var == "":
                rows_to_patch_clasico.append((i, d))

    print("\n[1] Base actual (vacío/clasico) por receta:")
    for rec, lines in base_por_rec.items():
        print(f"  {rec} {RECETAS[rec]['nombre']}: {len(lines)} líneas")
        for ln in lines:
            print(
                f"    var={ln.get('variedad_smart_menu')!r} "
                f"mp={ln.get('cod_mp_sistema')} {ln.get('nombre_mp')} "
                f"{ln.get('cantidad')} {ln.get('unidad_base')}"
            )
        if not lines:
            print("  ERROR: sin líneas base")
            return 1

    # Nuevas filas sabor
    nuevas_detalle: list[list[str]] = []
    for rec, meta in RECETAS.items():
        base_lines = base_por_rec[rec]
        # plantilla clasico (con var clasico)
        plantilla = []
        for ln in base_lines:
            p = deepcopy(ln)
            p["variedad_smart_menu"] = CLASICO
            p["nombre_receta"] = meta["nombre"]
            p["cod_receta"] = rec if not str(ln.get("cod_receta", "")).isdigit() else str(ln.get("cod_receta"))
            # normalizar cod_receta a como estaba
            p["cod_receta"] = (ln.get("cod_receta") or rec).strip()
            plantilla.append(p)

        for sabor, (cod_mp, nom_mp) in PULPAS.items():
            for p in plantilla:
                q = deepcopy(p)
                q["variedad_smart_menu"] = sabor
                # limpiar costos para que recalculo los llene
                for col in ("costo_unitario", "costo_linea", "nota_costo"):
                    if col in q:
                        q[col] = ""
                nuevas_detalle.append(_dict_to_row(hdr_d, q, width_d))
            # línea pulpa
            pulp = {h: "" for h in hdr_d}
            pulp["nombre_receta"] = meta["nombre"]
            pulp["cod_receta"] = (base_lines[0].get("cod_receta") or rec).strip()
            pulp["variedad_smart_menu"] = sabor
            pulp["nombre_mp"] = nom_mp
            pulp["cod_mp_sistema"] = cod_mp
            pulp["cantidad"] = str(int(ML_PULPA)) if ML_PULPA == int(ML_PULPA) else str(ML_PULPA)
            pulp["unidad_base"] = "ml"
            pulp["cod_bodega"] = BOD_PULPA
            pulp["merma_pct"] = "0"
            pulp["es_opcional"] = "NO"
            pulp["pct_aplicacion"] = "1"
            nuevas_detalle.append(_dict_to_row(hdr_d, pulp, width_d))

    print(f"\n  Patch clasico (vacío→clasico): {len(rows_to_patch_clasico)} filas")
    print(f"  Nuevas filas detalle (4 sabores × 3 recetas): {len(nuevas_detalle)}")

    # ── PRODUCTOS ────────────────────────────────────────────
    ws_p = sh.worksheet("BD_PRODUCTOS")
    vals_p = ws_p.get_all_values()
    hi_p, hdr_p = _headers(vals_p)
    width_p = max(len(r) for r in vals_p)

    sm_targets = {meta["sm"] for meta in RECETAS.values()}
    prod_base: dict[str, dict] = {}  # sm -> row dict
    prod_row_idx: dict[str, int] = {}
    inactivar_rows: list[int] = []

    for i, row in enumerate(vals_p[hi_p + 1 :], start=hi_p + 2):
        if not any(c.strip() for c in row):
            continue
        d = _row_dict(hdr_p, row)
        sm = (d.get("cod_smart_menu") or "").strip()
        if sm == SM_DAIQUIRI_DURAZNO_OLD:
            inactivar_rows.append(i)
            print(f"  Inactivar fila {i}: {d.get('nombre_producto')} sm={sm}")
        if sm in sm_targets:
            var = (d.get("variedad_smart_menu") or "").strip().lower()
            if var in ("", CLASICO) and sm not in prod_base:
                prod_base[sm] = d
                prod_row_idx[sm] = i

    print("\n[2] Productos base:")
    for sm, d in prod_base.items():
        print(
            f"  sm={sm} {d.get('nombre_producto')} precio={d.get('precio_venta')} "
            f"var={d.get('variedad_smart_menu')!r} rec={d.get('cod_receta')}"
        )

    if set(sm_targets) - set(prod_base):
        print("ERROR faltan productos base:", set(sm_targets) - set(prod_base))
        return 1

    nuevas_prod: list[list[str]] = []
    patches_prod_clasico: list[tuple[int, dict]] = []
    for sm, d in prod_base.items():
        precio0 = _parse_precio(d.get("precio_venta", ""))
        # patch a clasico
        dc = deepcopy(d)
        dc["variedad_smart_menu"] = CLASICO
        dc["precio_venta"] = d.get("precio_venta") or _fmt_precio(precio0)
        patches_prod_clasico.append((prod_row_idx[sm], dc))
        for sabor in SABORES:
            # ¿ya existe?
            exists = False
            for row in vals_p[hi_p + 1 :]:
                dd = _row_dict(hdr_p, row)
                if (dd.get("cod_smart_menu") or "").strip() == sm and (
                    dd.get("variedad_smart_menu") or ""
                ).strip().lower() == sabor:
                    exists = True
                    break
            if exists:
                print(f"  SKIP prod ya existe sm={sm} var={sabor}")
                continue
            p = deepcopy(d)
            p["variedad_smart_menu"] = sabor
            p["precio_venta"] = _fmt_precio(precio0 + 1.0)
            p["activo"] = "SI"
            nuevas_prod.append(_dict_to_row(hdr_p, p, width_p))

    print(f"  Patch productos → clasico: {len(patches_prod_clasico)}")
    print(f"  Nuevos productos sabor: {len(nuevas_prod)}")
    print(f"  Inactivar Daiquiri Durazno filas: {inactivar_rows}")

    if dry:
        print("\n[dry-run] No se escribió nada.")
        print("Ejemplo nuevas detalle (primeras 3):")
        for r in nuevas_detalle[:3]:
            print(" ", r[:12])
        print("Ejemplo nuevos productos:")
        for r in nuevas_prod[:3]:
            print(" ", r[:8])
        return 0

    # ── ESCRITURA ────────────────────────────────────────────
    print("\n[3] Escribiendo Sheets...")

    # Patch detalle vacío → clasico
    col_var_d = hdr_d.index("variedad_smart_menu") + 1
    for sheet_row, _d in rows_to_patch_clasico:
        ws_d.update_cell(sheet_row, col_var_d, CLASICO)
        time.sleep(0.15)
    print(f"  Detalle clasico patch: {len(rows_to_patch_clasico)}")

    # Append detalle
    if nuevas_detalle:
        # RAW evita que Sheets convierta 093 → 93
        ws_d.append_rows(nuevas_detalle, value_input_option=ValueInputOption.raw)
        print(f"  Detalle append: {len(nuevas_detalle)}")
        time.sleep(1)

    # Patch productos clasico
    col_var_p = hdr_p.index("variedad_smart_menu") + 1
    for sheet_row, dc in patches_prod_clasico:
        ws_p.update_cell(sheet_row, col_var_p, CLASICO)
        time.sleep(0.15)
    print(f"  Productos clasico patch: {len(patches_prod_clasico)}")

    # Inactivar 570
    if "activo" in hdr_p:
        col_act = hdr_p.index("activo") + 1
        for sheet_row in inactivar_rows:
            ws_p.update_cell(sheet_row, col_act, "NO")
            time.sleep(0.15)
        print(f"  Inactivados: {len(inactivar_rows)}")

    if nuevas_prod:
        ws_p.append_rows(nuevas_prod, value_input_option=ValueInputOption.user_entered)
        print(f"  Productos append: {len(nuevas_prod)}")

    print("\nOK escritura. Siguiente: calcular_costo_recetas.py --produccion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
