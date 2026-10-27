"""
Alta PROMOS cocteles 2×$14 (Smart Menu 608–612).

Una venta = 2 cocteles del mismo tipo → receta promo = receta base × 2.

  SM 608 Promo Buchanitas          rec 614 ← 038 ×2 (Buchanan's Pineapple → Buchanan's 12)
  SM 609 Promo negroni morlaco     rec 615 ← 604 ×2
  SM 610 Promo coconut mojito      rec 616 ← 052 ×2
  SM 611 Promo Classic Negroni     rec 617 ← 046 ×2
  SM 612 Promo ron Banana negroni  rec 618 ← 045 ×2

Uso:
  python alta_promos_cocteles_2x.py
  python alta_promos_cocteles_2x.py --produccion
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date

import gspread
from dotenv import load_dotenv
from gspread.utils import ValueInputOption

from google_credentials import google_credentials

load_dotenv(override=True)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FACTOR = 2
PRECIO = "14"
CAT = "PROMOS"
HOY = date.today().strftime("%d/%m/%Y")

# (cod_smart_menu, nombre_producto, cod_receta_nueva, cod_receta_base)
PROMOS = [
    ("608", "Promo Buchanitas", "614", "38"),
    ("609", "Promo negroni morlaco", "615", "604"),
    ("610", "Promo coconut mojito", "616", "52"),
    ("611", "Promo Classic Negroni", "617", "46"),
    ("612", "Promo ron Banana negroni", "618", "45"),
]

# cod_receta_nueva → {cod_mp_base: (cod_mp_nuevo, nombre_mp_nuevo)}
SUSTITUCIONES = {
    "614": {"160": ("158", "Whisky Buchanans")},
}

COLS_COPIA = (
    "variedad_smart_menu", "nombre_subreceta", "cod_subreceta", "nombre_mp",
    "cod_mp_sistema", "unidad_base", "cod_bodega", "merma_pct", "es_opcional", "pct_aplicacion",
)


def _sh():
    creds = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])


def _header_idx(rows: list[list[str]], col: str) -> int:
    for i, r in enumerate(rows):
        if any((c or "").strip() == col for c in r):
            return i
    raise RuntimeError(f"No header con columna {col}")


def _ci(headers: list[str]) -> dict[str, int]:
    return {h: i for i, h in enumerate(headers) if h}


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None:
        return ""
    return (row[idx] if idx < len(row) else "").strip()


def _norm_rec(cod: str) -> str:
    return cod.strip().lstrip("0") or "0"


def _num(s: str) -> float:
    return float(str(s or "0").replace(",", ".") or 0)


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _append_rows(ws, headers: list[str], datos: list[dict]) -> None:
    ci = _ci(headers)
    filas = []
    for data in datos:
        fila = [""] * len(headers)
        for k, v in data.items():
            if k in ci:
                fila[ci[k]] = v
        filas.append(fila)
    ws.append_rows(filas, value_input_option=ValueInputOption.user_entered)


def alta_recetas(sh, *, dry: bool) -> None:
    ws = sh.worksheet("BD_RECETAS_DETALLE")
    rows = ws.get_all_values()
    hi = _header_idx(rows, "cod_receta")
    headers = [(c or "").strip() for c in rows[hi]]
    ci = _ci(headers)
    ic_rec = ci["cod_receta"]
    body = rows[hi + 1 :]

    usados = {_norm_rec(_cell(r, ic_rec)) for r in body if _cell(r, ic_rec)}
    nuevas: list[dict] = []
    for sm, nombre, rec_new, rec_base in PROMOS:
        if _norm_rec(rec_new) in usados:
            dueño = next(
                (_cell(r, ci.get("nombre_receta")) for r in body if _norm_rec(_cell(r, ic_rec)) == _norm_rec(rec_new)),
                "",
            )
            if dueño.upper() != nombre.upper():
                raise SystemExit(f"ABORT: receta {rec_new} ya usada por {dueño!r}")
            print(f"SKIP receta {rec_new} {nombre}: ya existe")
            continue
        base = [r for r in body if _norm_rec(_cell(r, ic_rec)) == _norm_rec(rec_base)]
        if not base:
            raise SystemExit(f"ABORT: receta base {rec_base} sin líneas")
        sust = SUSTITUCIONES.get(rec_new, {})
        print(f"\n  {rec_new} {nombre}  (base {rec_base} × {FACTOR})")
        for r in base:
            line = {k: _cell(r, ci.get(k)) for k in COLS_COPIA}
            mp = line["cod_mp_sistema"]
            if mp in sust:
                line["cod_mp_sistema"], line["nombre_mp"] = sust[mp]
            cant = _num(_cell(r, ci["cantidad"])) * FACTOR
            line.update({"nombre_receta": nombre, "cod_receta": rec_new, "cantidad": _fmt(cant)})
            etiqueta = line["nombre_mp"] or line["nombre_subreceta"]
            print(f"    • {line['cod_mp_sistema'] or line['cod_subreceta']} {etiqueta}: {line['cantidad']} {line['unidad_base']} @{line['cod_bodega']}")
            nuevas.append(line)
    if nuevas and not dry:
        _append_rows(ws, headers, nuevas)
        print(f"\n  OK {len(nuevas)} líneas de receta")


def alta_productos(sh, *, dry: bool) -> None:
    ws = sh.worksheet("BD_PRODUCTOS")
    rows = ws.get_all_values()
    hi = _header_idx(rows, "cod_smart_menu")
    headers = [(c or "").strip() for c in rows[hi]]
    ci = _ci(headers)
    sms = {_cell(r, ci["cod_smart_menu"]) for r in rows[hi + 1 :]}
    nuevos = []
    for sm, nombre, rec_new, _ in PROMOS:
        if sm in sms:
            print(f"  SKIP producto SM {sm}: ya existe")
            continue
        print(f"  + SM {sm} {nombre} rec={rec_new} {CAT} ${PRECIO}")
        nuevos.append({
            "nombre_producto": nombre,
            "cod_smart_menu": sm,
            "variedad_smart_menu": "",
            "cod_receta": rec_new,
            "categoria_menu": CAT,
            "precio_venta": PRECIO,
            "Punto_venta": "BARRA",
            "activo": "SI",
            "version": "V1",
            "fecha_vigencia": HOY,
            "rendimiento": "1",
            "descarga_inventario": "SI",
        })
    if nuevos and not dry:
        _append_rows(ws, headers, nuevos)
        print(f"  OK {len(nuevos)} productos")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--produccion", action="store_true")
    dry = not ap.parse_args().produccion

    sh = _sh()
    print("=== BD_RECETAS_DETALLE ===")
    alta_recetas(sh, dry=dry)
    print("\n=== BD_PRODUCTOS ===")
    alta_productos(sh, dry=dry)

    if dry:
        print("\n[DRY-RUN] usa --produccion para escribir")
        return 0

    print("\n=== calcular_costo_recetas ===")
    subprocess.run(
        [sys.executable, "calcular_costo_recetas.py", "--produccion"],
        check=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    print("\nDONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
