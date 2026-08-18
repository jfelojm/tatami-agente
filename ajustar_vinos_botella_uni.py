"""
Convierte vinos de botella 750ml a unidad_base=uni y ajusta stock/costos/recetas/factor.

MPs (pedido Felipe 17/7/2026):
  213  Vino Garnacha Rosado
  232  Vino JP Chenet Cabernet-Syrah
  233  Vino JP Chenet Ice Edition
  210  Vino Chandon Brut Rosé  (ya uni pero stock/factor inflados ×750)
  V0ALC2 SANGRE DE TORO TINTO 750ML

Regla:
  1 botella = 750 ml = 1 uni
  stock_uni = stock_ml / 750
  costo_uni = costo_ml * 750
  factor_conversion items_prov = 1
  precio_ref (si venía por ml) *= 750
  recetas: cantidad_ml / 750, unidad_base=uni
  mov_inventario: cantidad /= 750, costo_u *= 750, unidad=uni

Uso:
  python ajustar_vinos_botella_uni.py
  python ajustar_vinos_botella_uni.py --produccion
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

from google_credentials import google_credentials
from sheet_numbers import parse_sheet_number

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
ML_BOTELLA = 750.0

# Códigos a convertir (todos se tratan como si el ledger estuviera en escala ml)
CODS = ["213", "232", "233", "210", "V0ALC2"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _sheet():
    creds = google_credentials(SCOPES)
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _header_idx(values: list[list[str]], key: str) -> tuple[int, list[str]]:
    hi = next(i for i, r in enumerate(values) if key in [(c or "").strip() for c in r])
    return hi, [(c or "").strip() for c in values[hi]]


def _fmt_num(x: float) -> str:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _needs_scale(unidad: str, qty: float) -> bool:
    """
    True si la cantidad aún está en escala ml (o uni inflada).
    Heurística: unidad ml, o uni con |qty| >= 50 (botellas reales son pocas).
    """
    u = (unidad or "").strip().lower()
    if u == "ml":
        return True
    if u == "uni" and abs(qty) >= 50:
        return True
    return False


def ajustar_maestro(sh, *, produccion: bool) -> None:
    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi, h = _header_idx(vals, "cod_mp_sistema")
    ic = h.index("cod_mp_sistema")
    iu = h.index("unidad_base")
    it = h.index("tipo_control")
    icu = h.index("costo_unitario_ref")
    ist = h.index("stock_actual") if "stock_actual" in h else None
    inm = h.index("nombre_mp") if "nombre_mp" in h else None

    updates: list[dict] = []
    print("\n--- BD_MP_SISTEMA ---")
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        cod = (row[ic] if ic < len(row) else "").strip()
        if cod not in CODS:
            continue
        uni = (row[iu] if iu < len(row) else "").strip()
        costo = parse_sheet_number(row[icu] if icu < len(row) else "") or 0.0
        stock = parse_sheet_number(row[ist] if ist is not None and ist < len(row) else "") or 0.0
        nombre = row[inm] if inm is not None and inm < len(row) else ""

        # Costo: si unidad era ml O costo luce como USD/ml (< 1), escalar ×750
        costo_nuevo = costo
        if uni.lower() == "ml" or (costo > 0 and costo < 1.0):
            costo_nuevo = round(costo * ML_BOTELLA, 6)

        stock_nuevo = stock
        if _needs_scale(uni, stock) or uni.lower() == "ml":
            stock_nuevo = round(stock / ML_BOTELLA, 4)

        print(
            f"  {cod} {nombre}: {uni}->{'uni'} | "
            f"stock {stock} -> {stock_nuevo} | "
            f"costo {costo} -> {costo_nuevo}"
        )
        updates.append({"range": rowcol_to_a1(i, iu + 1), "values": [["uni"]]})
        updates.append({"range": rowcol_to_a1(i, it + 1), "values": [["UNIDAD"]]})
        updates.append(
            {"range": rowcol_to_a1(i, icu + 1), "values": [[_fmt_num(costo_nuevo)]]}
        )
        # stock lo deja recalcular_stock_sheets; no forzamos aquí salvo dry-run info

    if produccion and updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        print(f"  Escrito maestro: {len(updates)} celdas")


def ajustar_items_prov(sh, *, produccion: bool) -> None:
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi, h = _header_idx(vals, "cod_mp_sistema")
    cols = {k: h.index(k) for k in h if k}
    ic = cols["cod_mp_sistema"]
    updates: list[dict] = []
    print("\n--- BD_ITEMS_PROV ---")
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        mp = (row[ic] if ic < len(row) else "").strip()
        if mp not in CODS:
            continue
        fac = parse_sheet_number(row[cols["factor_conversion"]]) if "factor_conversion" in cols else None
        ub = (row[cols["unidad_base_sistema"]] if "unidad_base_sistema" in cols else "").strip()
        pref = parse_sheet_number(row[cols["precio_ref"]]) if "precio_ref" in cols else None
        desc = row[cols["descripcion_proveedor"]] if "descripcion_proveedor" in cols else ""

        fac_nuevo = 1.0
        pref_nuevo = pref
        # Si factor=750 o base=ml, precio_ref estaba por ml → ×750
        if pref is not None and pref > 0:
            if (fac and abs(fac - ML_BOTELLA) < 0.01) or ub.lower() == "ml" or pref < 1.0:
                pref_nuevo = round(pref * ML_BOTELLA, 6)

        print(
            f"  {mp} | factor {fac}->{fac_nuevo} | base {ub}->uni | "
            f"precio_ref {pref}->{pref_nuevo} | {desc[:50]}"
        )
        if "factor_conversion" in cols:
            updates.append(
                {
                    "range": rowcol_to_a1(i, cols["factor_conversion"] + 1),
                    "values": [["1"]],
                }
            )
        if "unidad_base_sistema" in cols:
            updates.append(
                {
                    "range": rowcol_to_a1(i, cols["unidad_base_sistema"] + 1),
                    "values": [["uni"]],
                }
            )
        if "precio_ref" in cols and pref_nuevo is not None:
            updates.append(
                {
                    "range": rowcol_to_a1(i, cols["precio_ref"] + 1),
                    "values": [[_fmt_num(pref_nuevo)]],
                }
            )

    if produccion and updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        print(f"  Escrito items_prov: {len(updates)} celdas")


def ajustar_recetas(sh, *, produccion: bool) -> None:
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    vals = ws.get_all_values()
    hi, h = _header_idx(vals, "cod_mp_sistema")
    cols = {k: h.index(k) for k in h if k}
    updates: list[dict] = []
    print("\n--- BD_RECETAS_DETALLE ---")
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        mp = (row[cols["cod_mp_sistema"]] if "cod_mp_sistema" in cols else "").strip()
        if mp not in CODS:
            continue
        cant = parse_sheet_number(row[cols["cantidad"]]) if "cantidad" in cols else None
        uni = (row[cols["unidad_base"]] if "unidad_base" in cols else "").strip()
        rec = row[cols["cod_receta"]] if "cod_receta" in cols else ""
        var = row[cols["variedad_smart_menu"]] if "variedad_smart_menu" in cols else ""

        if cant is None:
            continue

        # Convertir cantidades en ml (o 1 ml bug de botella) a uni
        if uni.lower() == "ml":
            if abs(cant - 1.0) < 1e-9:
                # Caso V0ALC2 rec 97: "1 ml" significaba 1 botella mal tipada
                cant_nueva = 1.0
            else:
                cant_nueva = round(cant / ML_BOTELLA, 6)
        elif uni.lower() == "uni" and abs(cant) >= 50:
            cant_nueva = round(cant / ML_BOTELLA, 6)
        else:
            cant_nueva = cant  # ya uni razonable

        print(f"  rec={rec} var={var!r} mp={mp}: {cant} {uni} -> {cant_nueva} uni")
        if "cantidad" in cols:
            updates.append(
                {
                    "range": rowcol_to_a1(i, cols["cantidad"] + 1),
                    "values": [[_fmt_num(cant_nueva)]],
                }
            )
        if "unidad_base" in cols:
            updates.append(
                {
                    "range": rowcol_to_a1(i, cols["unidad_base"] + 1),
                    "values": [["uni"]],
                }
            )

    if produccion and updates:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        print(f"  Escrito recetas: {len(updates)} celdas")


def ajustar_movimientos(sb, *, produccion: bool) -> dict[str, int]:
    print("\n--- mov_inventario ---")
    counts: dict[str, int] = {}
    for cod in CODS:
        rows: list[dict] = []
        offset = 0
        while True:
            chunk = (
                sb.table("mov_inventario")
                .select("cod_mov,cantidad_mov,unidad_base,costo_unitario,costo_total")
                .eq("cod_mp_sistema", cod)
                .range(offset, offset + 999)
                .execute()
                .data
                or []
            )
            rows.extend(chunk)
            if len(chunk) < 1000:
                break
            offset += 1000

        n = 0
        for m in rows:
            qty = float(m.get("cantidad_mov") or 0)
            uni = (m.get("unidad_base") or "").strip()
            costo_u = m.get("costo_unitario")
            costo_u_f = float(costo_u) if costo_u is not None else None

            scale_qty = _needs_scale(uni, qty) or uni.lower() == "ml"
            scale_costo = (
                costo_u_f is not None
                and costo_u_f > 0
                and (uni.lower() == "ml" or costo_u_f < 1.0)
            )

            if not scale_qty and not scale_costo:
                if uni.lower() != "uni" and produccion:
                    sb.table("mov_inventario").update({"unidad_base": "uni"}).eq(
                        "cod_mov", m["cod_mov"]
                    ).execute()
                continue

            patch: dict = {"unidad_base": "uni"}
            qty_n = qty
            if scale_qty:
                qty_n = round(qty / ML_BOTELLA, 6)
                patch["cantidad_mov"] = qty_n
            if scale_costo:
                costo_n = round(costo_u_f * ML_BOTELLA, 6)
                patch["costo_unitario"] = costo_n
                # Si solo escalamos costo (qty ya en uni), recalcular total
                if not scale_qty and qty_n:
                    patch["costo_total"] = round(qty_n * costo_n, 4)
            # Si escalamos qty+costo, costo_total se mantiene (valorizado constante)

            if n < 5:
                print(
                    f"  {cod} {m['cod_mov']}: qty {qty}->{qty_n} | "
                    f"costo_u {costo_u_f}->{patch.get('costo_unitario', costo_u_f)}"
                )
            if produccion:
                sb.table("mov_inventario").update(patch).eq(
                    "cod_mov", m["cod_mov"]
                ).execute()
            n += 1
        counts[cod] = n
        print(f"  {cod}: {n}/{len(rows)} movimientos a ajustar")
    return counts


def saldo_ledger(sb, cod: str) -> float:
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("mov_inventario")
            .select("tipo_mov,cantidad_mov")
            .eq("cod_mp_sistema", cod)
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    stock = 0.0
    for m in rows:
        t = m.get("tipo_mov") or ""
        c = float(m.get("cantidad_mov") or 0)
        if t in ("AJUSTE_POSITIVO", "ENTRADA", "TRASLADO_ENTRADA"):
            stock += c
        elif t in ("SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRASLADO_SALIDA"):
            stock -= c
    return stock


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--produccion", action="store_true")
    args = p.parse_args()
    dry = not args.produccion

    print("=" * 60)
    print(f"VINOS BOTELLA → uni {'DRY RUN' if dry else 'PRODUCCIÓN'}")
    print(f"MPs: {', '.join(CODS)} | factor {ML_BOTELLA} ml/botella")
    print("=" * 60)

    sh = _sheet()
    ajustar_maestro(sh, produccion=args.produccion)
    ajustar_items_prov(sh, produccion=args.produccion)
    ajustar_recetas(sh, produccion=args.produccion)

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    ajustar_movimientos(sb, produccion=args.produccion)

    if args.produccion:
        print("\nRecalculando stock Sheets…")
        for cod in CODS:
            rc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "recalcular_stock_sheets.py"),
                    "--produccion",
                    "--cod-mp",
                    cod,
                ],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            ).returncode
            if rc != 0:
                print(f"  WARN recalcular {cod} exit={rc}")

    print("\n--- Saldos ledger (post) ---")
    for cod in CODS:
        s = saldo_ledger(sb, cod)
        print(f"  {cod}: {s:.4f} uni")

    if dry:
        print("\nDRY RUN: no se escribió nada. Reejecutar con --produccion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
