"""
Audita conteos contabilizados: compara delta registrado (base Sheets en snapshot)
vs delta que habría salido con saldo mov_inventario al momento del ajuste.

Uso:
  python auditar_conteo_delta_sheets.py
  python auditar_conteo_delta_sheets.py --min-error 1
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from supabase import create_client

from recalcular_stock_sheets import (
    TIPOS_RESTA_ORIGEN,
    TIPOS_SUMA_DESTINO,
    _bodega_mov,
    _clave_stock,
)

load_dotenv(override=True)


def paginar(sb, tabla: str, select: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = sb.table(tabla).select(select).range(offset, offset + 999).execute().data or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def parse_fecha(f: object) -> str:
    return str(f or "")[:19]


def stock_antes_conteo(
    movs_sorted: list[dict],
    cod_mp: str,
    cod_bodega: str,
    antes_de_cod_mov: str,
) -> float:
    k = _clave_stock(cod_mp, cod_bodega)
    saldo = 0.0
    cod_mp_n = cod_mp.strip()
    for m in movs_sorted:
        if m.get("cod_mov") == antes_de_cod_mov:
            break
        if (m.get("cod_mp_sistema") or "").strip() != cod_mp_n:
            continue
        tipo = (m.get("tipo_mov") or "").strip()
        bod = _bodega_mov(m, tipo)
        if _clave_stock(cod_mp, bod) != k:
            continue
        cant = float(m.get("cantidad_mov") or 0)
        if tipo in TIPOS_SUMA_DESTINO:
            saldo += cant
        elif tipo in TIPOS_RESTA_ORIGEN:
            saldo -= cant
    return round(saldo, 4)


def main() -> None:
    p = argparse.ArgumentParser(description="Auditoría delta conteo vs mov_inventario")
    p.add_argument(
        "--min-error",
        type=float,
        default=0.01,
        help="Umbral mínimo |error_delta| para listar (default 0.01)",
    )
    args = p.parse_args()

    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    dets = paginar(
        sb,
        "conteo_envio_detalle",
        "cod_mp_sistema,cod_bodega,nombre_mp,conteo_fisico,stock_sistema_snapshot,"
        "delta_calculado,cod_mov_ajuste,estado_linea",
    )
    dets = [
        d
        for d in dets
        if d.get("cod_mov_ajuste") and (d.get("estado_linea") or "").strip() == "CONTABILIZADO"
    ]
    print(f"Líneas conteo contabilizadas: {len(dets)}")

    movs = paginar(
        sb,
        "mov_inventario",
        "cod_mov,cod_mp_sistema,tipo_mov,cantidad_mov,cod_bodega_origen,"
        "cod_bodega_destino,fecha,origen_documento",
    )
    mov_by_cod = {m["cod_mov"]: m for m in movs}
    movs_sorted = sorted(movs, key=lambda m: (parse_fecha(m.get("fecha")), m.get("cod_mov", "")))

    errores: list[dict] = []
    ok = 0
    for d in dets:
        cod_mov = d["cod_mov_ajuste"]
        mov = mov_by_cod.get(cod_mov)
        if not mov:
            continue
        cod = (d["cod_mp_sistema"] or "").strip()
        bod = (d["cod_bodega"] or "").strip()
        conteo = float(d["conteo_fisico"])
        snap = float(d["stock_sistema_snapshot"] or 0)
        delta_reg = float(d["delta_calculado"])
        stock_mov = stock_antes_conteo(movs_sorted, cod, bod, cod_mov)
        delta_ok = round(conteo - stock_mov, 4)
        error_delta = round(delta_reg - delta_ok, 4)
        if abs(error_delta) < args.min_error:
            ok += 1
            continue
        errores.append(
            {
                "cod_mp": cod,
                "nombre": ((d.get("nombre_mp") or "")[:45]),
                "bodega": bod,
                "conteo": conteo,
                "snap_sheets": snap,
                "stock_mov_antes": stock_mov,
                "delta_registrado": delta_reg,
                "delta_correcto": delta_ok,
                "error_delta": error_delta,
                "cod_mov": cod_mov,
                "fecha": parse_fecha(mov.get("fecha")),
            }
        )

    errores.sort(key=lambda x: abs(x["error_delta"]), reverse=True)
    print(f"OK (delta cuadra con mov): {ok}")
    print(f"Con desfase Sheets vs mov (|error| >= {args.min_error}): {len(errores)}")
    if not errores:
        return

    print()
    for e in errores:
        print(
            f"MP {e['cod_mp']:>3} {e['bodega']} {e['fecha']}"
            f" | conteo={e['conteo']} snap={e['snap_sheets']} mov_antes={e['stock_mov_antes']}"
            f" | delta_reg={e['delta_registrado']} delta_ok={e['delta_correcto']}"
            f" ERR={e['error_delta']:+.4f}"
            f" | {e['nombre']}"
        )

    # Resumen por magnitud
    graves = [e for e in errores if abs(e["error_delta"]) >= 10]
    moderados = [e for e in errores if 1 <= abs(e["error_delta"]) < 10]
    leves = [e for e in errores if abs(e["error_delta"]) < 1]
    print()
    print(f"Resumen: graves (>=10): {len(graves)} | moderados (1-10): {len(moderados)} | leves (<1): {len(leves)}")

    from collections import defaultdict

    por_fecha: dict[str, dict] = defaultdict(lambda: {"n": 0, "errores": 0, "sum_abs": 0.0})
    for d in dets:
        mov = mov_by_cod.get(d.get("cod_mov_ajuste") or "")
        if not mov:
            continue
        fecha = parse_fecha(mov.get("fecha"))[:10]
        por_fecha[fecha]["n"] += 1
    for e in errores:
        por_fecha[e["fecha"][:10]]["errores"] += 1
        por_fecha[e["fecha"][:10]]["sum_abs"] += abs(e["error_delta"])

    print("\nPor fecha de contabilización:")
    for fecha in sorted(por_fecha):
        x = por_fecha[fecha]
        print(
            f"  {fecha}: {x['errores']}/{x['n']} con desfase"
            + (f" | Σ|error|={x['sum_abs']:,.0f}" if x["errores"] else "")
        )


if __name__ == "__main__":
    main()
