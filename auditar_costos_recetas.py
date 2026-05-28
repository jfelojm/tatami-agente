"""
Auditoría de costos de platos y MPs en recetas.

1) Platos con costo inflado (umbral por plato o línea dominante).
2) Líneas MP sospechosas: sin costo, costo unitario alto en gr/ml, posible precio/kg como USD/gr.

Uso:
  python auditar_costos_recetas.py
  python auditar_costos_recetas.py -o reporte_costos_recetas.csv
  python auditar_costos_recetas.py --umbral-plato 25 --umbral-linea 15
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime

from dotenv import load_dotenv

from calcular_costo_recetas import (
    cargar_contexto_costos,
    resumen_plato_costo,
)
from calcular_costo_subrecetas import _safe_float

load_dotenv(override=True)


def _norm_mp_key(c: str) -> str:
    s = (c or "").strip()
    if not s:
        return ""
    n = s.lstrip("0")
    return n if n else "0"


def cargar_mp_maestro(sh) -> dict[str, dict]:
    ws = sh.worksheet("BD_MP_SISTEMA")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_mp_sistema" in r)
    headers = [(c or "").strip() for c in vals[hi]]
    out: dict[str, dict] = {}
    for row in vals[hi + 1 :]:
        d = {
            h: (row[i] if i < len(row) else "").strip()
            for i, h in enumerate(headers)
        }
        cod = _norm_mp_key(d.get("cod_mp_sistema", ""))
        if cod:
            out[cod] = d
    return out


def cargar_precio_ref_prov(sh) -> dict[str, float]:
    """cod_mp_norm -> precio_ref/factor (USD por unidad_base teórica)."""
    ws = sh.worksheet("BD_ITEMS_PROV")
    vals = ws.get_all_values()
    hi = next(
        i
        for i, r in enumerate(vals)
        if any((c or "").strip() == "cod_item_prov" for c in r)
    )
    headers = [(c or "").strip() for c in vals[hi]]
    icod = headers.index("cod_mp_sistema")
    iprecio = headers.index("precio_ref")
    ifac = headers.index("factor_conversion")
    iact = headers.index("activo") if "activo" in headers else None
    sums: dict[str, list[float]] = {}
    for row in vals[hi + 1 :]:
        if iact is not None and (row[iact] if iact < len(row) else "").strip().upper() == "NO":
            continue
        cod = _norm_mp_key(row[icod] if icod < len(row) else "")
        if not cod:
            continue
        precio = _safe_float(row[iprecio] if iprecio < len(row) else 0)
        fac = _safe_float(row[ifac] if ifac < len(row) else 1, 1.0)
        if precio <= 0 or fac <= 0:
            continue
        unit = precio / fac
        sums.setdefault(cod, []).append(unit)
    return {k: sum(v) / len(v) for k, v in sums.items() if v}


def _sospecha_mp(
    d: dict,
    mp_info: dict,
    precio_prov: dict[str, float],
) -> list[str]:
    flags: list[str] = []
    cu = float(d.get("costo_unitario") or 0)
    uni = (d.get("unidad_base") or mp_info.get("unidad_base") or "").strip().lower()
    cod = _norm_mp_key(d.get("cod") or "")
    line = float(d.get("costo_linea") or 0)

    if line <= 0:
        flags.append("sin_costo_linea")
        return flags

    if uni in ("gr", "ml") and cu > 0.15:
        flags.append("costo_unitario_alto_gr_ml")
    if uni in ("gr", "ml") and cu > 0.01 and line > 15:
        flags.append("linea_mp_cara")

    ref = precio_prov.get(cod, 0.0)
    if ref > 0 and uni in ("gr", "ml"):
        # Precio catálogo suele ser USD/kg → /1000 ≈ USD/gr
        ref_gr = ref / 1000.0 if ref > 0.05 else ref
        if cu > 0 and ref_gr > 0:
            ratio = cu / ref_gr
            if ratio > 80:
                flags.append("posible_precio_kg_como_gr_x1000")
            elif ratio < 0.02 and cu > 0.001:
                flags.append("costo_muy_bajo_vs_catalogo")

    if uni == "gr" and cu > 0.05 and ref > 1.0:
        flags.append("revisar_factor_conversion_kg")

    return flags


def auditar(
    *,
    umbral_plato: float,
    umbral_linea: float,
    umbral_cu_gr: float,
) -> tuple[list[dict], list[dict]]:
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])

    costos_mp, unitarios_sub, por_plato, _ = cargar_contexto_costos(sh)
    mp_maestro = cargar_mp_maestro(sh)
    precio_prov = cargar_precio_ref_prov(sh)

    platos_inflados: list[dict] = []
    lineas_sospechosas: list[dict] = []

    for _key, lineas in por_plato.items():
        res = resumen_plato_costo(lineas, costos_mp, unitarios_sub)
        if not res:
            continue
        cod = res["cod_receta"]
        var = res["variedad_smart_menu"]
        nombre = res["nombre_receta"]
        total = float(res["costo_plato_estandar"])

        max_linea = 0.0
        max_det = None
        for d in res.get("detalle_lineas") or []:
            lc = float(d.get("costo_linea") or 0)
            if lc > max_linea:
                max_linea = lc
                max_det = d

        if total >= umbral_plato or max_linea >= umbral_linea:
            platos_inflados.append(
                {
                    "tipo": "plato_inflado",
                    "cod_receta": cod,
                    "variedad_smart_menu": var,
                    "nombre_receta": nombre,
                    "costo_plato": total,
                    "linea_mas_cara": max_linea,
                    "tipo_linea_dominante": (max_det or {}).get("tipo", ""),
                    "cod_dominante": (max_det or {}).get("cod", ""),
                    "nombre_dominante": (max_det or {}).get("nombre", ""),
                    "detalle_dominante": (
                        f"{(max_det or {}).get('cantidad')} "
                        f"{(max_det or {}).get('unidad_base')} × "
                        f"{(max_det or {}).get('costo_unitario')} USD/u"
                    ),
                }
            )

        for d in res.get("detalle_lineas") or []:
            if d.get("tipo") != "MP":
                continue
            mp_info = mp_maestro.get(_norm_mp_key(d.get("cod") or ""), {})
            flags = _sospecha_mp(d, mp_info, precio_prov)
            cu = float(d.get("costo_unitario") or 0)
            uni = (d.get("unidad_base") or "").lower()
            if uni in ("gr", "ml") and cu >= umbral_cu_gr:
                flags.append("umbral_cu_gr")
            if float(d.get("costo_linea") or 0) >= umbral_linea:
                flags.append("umbral_linea")
            if not flags:
                continue
            lineas_sospechosas.append(
                {
                    "tipo": "mp_en_receta",
                    "cod_receta": cod,
                    "variedad_smart_menu": var,
                    "nombre_receta": nombre,
                    "costo_plato": total,
                    "cod_mp": d.get("cod"),
                    "nombre_mp": d.get("nombre"),
                    "cantidad": d.get("cantidad"),
                    "unidad_base": d.get("unidad_base"),
                    "cod_bodega": d.get("cod_bodega"),
                    "costo_unitario_ref": cu,
                    "costo_linea": d.get("costo_linea"),
                    "precio_prov_unit": round(precio_prov.get(_norm_mp_key(d.get("cod") or ""), 0), 6),
                    "flags": "|".join(flags),
                }
            )

    platos_inflados.sort(key=lambda x: x["costo_plato"], reverse=True)
    lineas_sospechosas.sort(key=lambda x: x["costo_linea"], reverse=True)
    return platos_inflados, lineas_sospechosas


def main() -> None:
    p = argparse.ArgumentParser(description="Auditoría costos platos y MPs en recetas")
    p.add_argument("-o", "--output", default="", help="CSV de salida")
    p.add_argument("--umbral-plato", type=float, default=25.0)
    p.add_argument("--umbral-linea", type=float, default=20.0)
    p.add_argument("--umbral-cu-gr", type=float, default=0.08)
    args = p.parse_args()

    platos, lineas = auditar(
        umbral_plato=args.umbral_plato,
        umbral_linea=args.umbral_linea,
        umbral_cu_gr=args.umbral_cu_gr,
    )

    print(f"Platos con costo >= {args.umbral_plato} USD o línea >= {args.umbral_linea}: {len(platos)}")
    for r in platos[:20]:
        print(
            f"  {r['cod_receta']:>4} {r['variedad_smart_menu'][:28]:28} "
            f"${r['costo_plato']:.2f}  domina: {r['tipo_linea_dominante']} "
            f"{r['cod_dominante']} {r['nombre_dominante'][:25]} "
            f"(${r['linea_mas_cara']:.2f})"
        )
    if len(platos) > 20:
        print(f"  ... y {len(platos) - 20} más")

    print(f"\nLíneas MP sospechosas: {len(lineas)}")
    for r in lineas[:25]:
        print(
            f"  plato {r['cod_receta']} | MP {r['cod_mp']} {r['nombre_mp'][:22]:22} "
            f"linea=${r['costo_linea']:.2f} cu={r['costo_unitario_ref']:.4f} "
            f"[{r['flags']}]"
        )
    if len(lineas) > 25:
        print(f"  ... y {len(lineas) - 25} más")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = (args.output or "").strip() or f"auditar_costos_recetas_{ts}.csv"
    fields = [
        "tipo",
        "cod_receta",
        "variedad_smart_menu",
        "nombre_receta",
        "costo_plato",
        "linea_mas_cara",
        "tipo_linea_dominante",
        "cod_dominante",
        "nombre_dominante",
        "detalle_dominante",
        "cod_mp",
        "nombre_mp",
        "cantidad",
        "unidad_base",
        "cod_bodega",
        "costo_unitario_ref",
        "costo_linea",
        "precio_prov_unit",
        "flags",
    ]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in platos + lineas:
            w.writerow(r)
    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()
