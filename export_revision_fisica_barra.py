"""
Listado barra (BOD-002): MPs con error en delta de conteo + stock actual mov_inventario.

Uso:
  python export_revision_fisica_barra.py
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client

from auditar_conteo_delta_sheets import paginar, parse_fecha, stock_antes_conteo
from recalcular_stock_sheets import _clave_stock, build_stock_calculado

load_dotenv(override=True)

BOD_BARRA = "BOD-002"
MIN_ERROR = 0.01
OUT_PATH = "exports/revision_fisica_barra_conteo_error.csv"


def main() -> None:
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    dets = paginar(
        sb,
        "conteo_envio_detalle",
        "cod_mp_sistema,cod_bodega,nombre_mp,unidad_base,conteo_fisico,"
        "stock_sistema_snapshot,delta_calculado,cod_mov_ajuste,estado_linea",
    )
    dets = [
        d
        for d in dets
        if d.get("cod_mov_ajuste")
        and (d.get("estado_linea") or "").strip() == "CONTABILIZADO"
        and (d.get("cod_bodega") or "").strip() == BOD_BARRA
    ]

    movs = paginar(
        sb,
        "mov_inventario",
        "cod_mov,cod_mp_sistema,tipo_mov,cantidad_mov,cod_bodega_origen,"
        "cod_bodega_destino,fecha",
    )
    mov_by_cod = {m["cod_mov"]: m for m in movs}
    movs_sorted = sorted(movs, key=lambda m: (parse_fecha(m.get("fecha")), m.get("cod_mov", "")))
    stock_actual = build_stock_calculado()

    por_mp: dict[str, dict] = {}
    for d in dets:
        mov = mov_by_cod.get(d["cod_mov_ajuste"])
        if not mov:
            continue
        cod = (d["cod_mp_sistema"] or "").strip()
        conteo = float(d["conteo_fisico"])
        delta_reg = float(d["delta_calculado"])
        stock_mov = stock_antes_conteo(movs_sorted, cod, BOD_BARRA, d["cod_mov_ajuste"])
        delta_ok = round(conteo - stock_mov, 4)
        err = round(delta_reg - delta_ok, 4)
        if abs(err) < MIN_ERROR:
            continue
        fecha = parse_fecha(mov.get("fecha"))[:10]
        if cod not in por_mp:
            por_mp[cod] = {
                "cod_mp_sistema": cod,
                "nombre_mp": (d.get("nombre_mp") or "").strip(),
                "unidad_base": (d.get("unidad_base") or "").strip(),
                "stock_actual_mov": round(stock_actual.get(_clave_stock(cod, BOD_BARRA), 0.0), 4),
                "error_delta_acum": err,
                "conteos_con_error": 1,
                "ultimo_conteo_fisico": conteo,
                "ultima_fecha_conteo": fecha,
            }
        else:
            row = por_mp[cod]
            row["error_delta_acum"] = round(row["error_delta_acum"] + err, 4)
            row["conteos_con_error"] += 1
            if fecha >= row["ultima_fecha_conteo"]:
                row["ultima_fecha_conteo"] = fecha
                row["ultimo_conteo_fisico"] = conteo
            if not row["nombre_mp"]:
                row["nombre_mp"] = (d.get("nombre_mp") or "").strip()

    rows = sorted(por_mp.values(), key=lambda r: abs(r["error_delta_acum"]), reverse=True)
    fields = [
        "cod_mp_sistema",
        "nombre_mp",
        "unidad_base",
        "stock_actual_mov",
        "error_delta_acum",
        "conteos_con_error",
        "ultimo_conteo_fisico",
        "ultima_fecha_conteo",
    ]
    os.makedirs("exports", exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"Exportado: {OUT_PATH} ({len(rows)} MPs barra con error de conteo)")
    for r in rows:
        print(
            f"  MP {r['cod_mp_sistema']:>5} | stock {r['stock_actual_mov']:>12} {r['unidad_base']:<4}"
            f" | error_acum {r['error_delta_acum']:+10.2f} | {r['nombre_mp'][:45]}"
        )


if __name__ == "__main__":
    main()
