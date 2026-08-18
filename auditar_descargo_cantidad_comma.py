"""
Audita líneas de receta cuya cantidad en Sheets usa coma decimal y el descargo
las omitía (float() → consumo 0).

Uso:
  python auditar_descargo_cantidad_comma.py
  python auditar_descargo_cantidad_comma.py --csv exports/auditoria_comma_descargo.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(override=True)

from descargo_inventario import cargar_recetas, get_ingredientes  # noqa: E402
from recetas_detalle import es_linea_subreceta  # noqa: E402
from sheet_numbers import parse_sheet_number  # noqa: E402


def _consumo_float_roto(ing: dict, qty: float = 1.0) -> float:
    try:
        gramaje = float(ing.get("cantidad", 0))
        pct = float(ing.get("pct_aplicacion", 1) or 1)
        merma = float(ing.get("merma_pct", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return qty * gramaje * pct * (1 + merma)


def _consumo_ok(ing: dict, qty: float = 1.0) -> float:
    gramaje = parse_sheet_number(ing.get("cantidad"), 0.0)
    pct = parse_sheet_number(ing.get("pct_aplicacion"), 1.0) or 1.0
    merma = parse_sheet_number(ing.get("merma_pct"), 0.0)
    if gramaje <= 0:
        return 0.0
    return qty * gramaje * pct * (1 + merma)


def _cantidad_afectada(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    if _consumo_float_roto({"cantidad": s}) > 0:
        return False
    return _consumo_ok({"cantidad": s}) > 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default="", help="Ruta CSV opcional")
    args = p.parse_args()

    cargar_recetas()
    # recetas únicas: cod_receta + variedad
    recetas_vistas: set[tuple[str, str]] = set()
    afectados: dict[str, dict] = {}
    por_receta: dict[str, list[str]] = defaultdict(list)

    # Necesitamos todas las combinaciones receta/variedad del maestro
    import gspread
    from google_credentials import google_credentials

    sh = gspread.authorize(
        google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    ).open_by_key(os.environ["SPREADSHEET_ID"])
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    rows = ws.get_all_values()
    hi = next(i for i, r in enumerate(rows) if "cod_receta" in [c.strip() for c in r])
    hdr = [h.strip() for h in rows[hi]]
    ci = {h: i for i, h in enumerate(hdr)}

    for r in rows[hi + 1 :]:
        cod = (r[ci.get("cod_receta", 0)] if "cod_receta" in ci else "").strip()
        var = (r[ci.get("variedad_smart_menu", 0)] if "variedad_smart_menu" in ci else "").strip()
        if cod:
            recetas_vistas.add((cod, var))

    for cod_receta, variedad in sorted(recetas_vistas):
        ings = get_ingredientes(cod_receta, variedad or None)
        for ing in ings:
            raw = str(ing.get("cantidad") or "").strip()
            if not _cantidad_afectada(raw):
                continue
            es_sub = es_linea_subreceta(ing)
            cod_item = (
                (ing.get("cod_subreceta") or "").strip()
                if es_sub
                else (ing.get("cod_mp_sistema") or "").strip()
            )
            nom = (
                (ing.get("nombre_subreceta") or ing.get("nombre_mp") or "").strip()
            )
            key = f"{'SUB' if es_sub else 'MP'}:{cod_item}"
            if key not in afectados:
                afectados[key] = {
                    "tipo": "SUB" if es_sub else "MP",
                    "cod": cod_item,
                    "nombre": nom,
                    "cantidad_ejemplo": raw,
                    "consumo_1venta": round(_consumo_ok(ing, 1.0), 4),
                    "unidad": (ing.get("unidad_base") or "").strip(),
                    "bodega": (ing.get("cod_bodega") or "").strip(),
                    "recetas": [],
                }
            nom_rec = (ing.get("nombre_receta") or cod_receta).strip()
            ref = f"{nom_rec} ({cod_receta})"
            if ref not in afectados[key]["recetas"]:
                afectados[key]["recetas"].append(ref)
                por_receta[cod_receta].append(cod_item)

    items = sorted(afectados.values(), key=lambda x: (x["tipo"], x["cod"]))
    print(f"Líneas de receta con cantidad coma omitida en descargo: {len(items)}")
    print(f"{'Tipo':<4} {'Cod':<10} {'Cant':>10} {'U':<4} {'Bod':<8} Nombre")
    print("-" * 72)
    for it in items:
        print(
            f"{it['tipo']:<4} {it['cod']:<10} {it['cantidad_ejemplo']:>10} "
            f"{it['unidad']:<4} {it['bodega']:<8} {it['nombre'][:32]}"
        )
        for rec in it["recetas"][:4]:
            print(f"      -> {rec}")
        if len(it["recetas"]) > 4:
            print(f"      -> ... +{len(it['recetas']) - 4} recetas más")

    if args.csv:
        os.makedirs(os.path.dirname(args.csv) or ".", exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "tipo",
                    "cod",
                    "nombre",
                    "cantidad_ejemplo",
                    "consumo_1venta",
                    "unidad",
                    "bodega",
                    "n_recetas",
                    "recetas",
                ],
            )
            w.writeheader()
            for it in items:
                w.writerow(
                    {
                        **{k: it[k] for k in it if k != "recetas"},
                        "n_recetas": len(it["recetas"]),
                        "recetas": "; ".join(it["recetas"]),
                    }
                )
        print(f"\nCSV: {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
