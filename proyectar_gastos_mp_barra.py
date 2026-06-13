"""
Proyección de gastos MP barra (BOD-002 + BOD-003) por periodo.

Fórmula de compra estimada:
  cantidad_base = max(0, consumo_diario × días + par_level − stock_barra)

Cantidades de compra en botellas/unidades legibles (misma lógica que generar_ordenes_compra).
Precio unitario desde BD_ITEMS_PROV.precio_ref; fallback costo_unitario_ref del maestro.

Uso:
  python proyectar_gastos_mp_barra.py
  python proyectar_gastos_mp_barra.py --desde 2026-06-09 --hasta 2026-07-09
  python proyectar_gastos_mp_barra.py --csv exports/mi_proyeccion.csv
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

BODEGAS_BARRA = frozenset({"BOD-002", "BOD-003"})

_ALIASES_BODEGA = {
    "BOD-001": "BOD-001",
    "COCINA": "BOD-001",
    "BOD-002": "BOD-002",
    "BARRA": "BOD-002",
    "BOD-003": "BOD-003",
    "CONSIGNACION": "BOD-003",
    "CONSIGNACIÓN": "BOD-003",
}


def _norm_bodega_catalogo(cod: object) -> str:
    s = (str(cod or "").strip().upper())
    return _ALIASES_BODEGA.get(s, s)


def _cargar_items_barra(proveedores: dict) -> dict[str, list[dict]]:
    """Ítems proveedor barra con destino BOD-002 / BOD-003 (acepta alias BARRA)."""
    from collections import defaultdict

    from generar_ordenes_compra import _norm_cod_mp, _norm_cod_prov, _to_float

    from procesar_facturas_drive import cargar_bd_items_prov

    provs = set(proveedores.keys())
    mp_items: dict[str, list[dict]] = defaultdict(list)
    for it in cargar_bd_items_prov():
        cp = _norm_cod_prov(it.get("cod_proveedor"))
        if cp not in provs:
            continue
        cod = _norm_cod_mp(it.get("cod_mp_sistema"))
        if not cod:
            continue
        bod = _norm_bodega_catalogo(it.get("cod_bodega_destino"))
        if bod not in BODEGAS_BARRA:
            continue
        factor = _to_float(it.get("factor_conversion"), 1.0) or 1.0
        try:
            prioridad = int(_to_float(it.get("prioridad"), 99))
        except (TypeError, ValueError):
            prioridad = 99
        mp_items[cod].append(
            {
                "cod_proveedor": cp,
                "descripcion_proveedor": (it.get("descripcion_proveedor") or "").strip(),
                "unidad_compra": (it.get("unidad_compra") or it.get("unidad_base_sistema") or "").strip(),
                "unidad_base_sistema": (it.get("unidad_base_sistema") or "").strip(),
                "factor_conversion": factor,
                "cod_bodega_destino": bod,
                "prioridad": prioridad,
                "precio_ref": it.get("precio_ref"),
            }
        )
    for cod in mp_items:
        mp_items[cod].sort(key=lambda x: (x["prioridad"], x["cod_proveedor"]))
    return mp_items


def _parse_fecha(s: str) -> date:
    return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def _dias_inclusive(desde: date, hasta: date) -> int:
    return max(0, (hasta - desde).days + 1)


def _to_float(v: object, default: float = 0.0) -> float:
    from numeros_sheets import parse_numero_sheets

    return parse_numero_sheets(v, default)


def _stock_barra(por_bodega: dict) -> float:
    return round(sum(float(por_bodega.get(b, 0) or 0) for b in BODEGAS_BARRA), 4)


def _precio_unidad_compra(item: dict, *, unidad_base: str) -> tuple[float, str]:
    """
    USD por unidad de compra (botella, caja, unidad).
    precio_ref = USD / unidad_base (ml, gr, uni).
    """
    from numeros_sheets import precio_ref_a_unidad_base

    pr_raw = _to_float(item.get("precio_ref"), 0.0)
    factor = _to_float(item.get("factor_conversion"), 1.0) or 1.0
    ub = (unidad_base or item.get("unidad_base_sistema") or "").strip().upper()
    uc = (item.get("unidad_compra") or "").strip().lower()

    if pr_raw <= 0:
        return 0.0, "sin_precio_ref"

    pb = precio_ref_a_unidad_base(pr_raw, factor)

    if ub == "ML" and factor > 0:
        return round(pb * factor, 4), "precio_ref_botella"

    if ub == "UNI":
        if uc in ("caja", "cajas") and factor > 1:
            return round(pb, 4), "precio_ref_por_botella"
        return round(pb, 4), "precio_ref_por_uni"

    if factor > 1:
        return round(pb * factor, 4), "precio_ref_pack"
    return round(pb, 4), "precio_ref"


def _precio_fallback_maestro(
    cod_mp: str, *, unidad_base: str, factor: float, rows_mp: list[dict]
) -> tuple[float, str]:
    """costo_unitario_ref (USD/unidad_base) → unidad de compra."""
    from costo_mp_canonico import norm_mp

    nk = norm_mp(cod_mp)
    cu = 0.0
    for r in rows_mp:
        if norm_mp(r.get("cod_mp_sistema")) == nk:
            cu = _to_float(r.get("costo_unitario_ref"), 0.0)
            if cu > 0:
                break
    if cu <= 0:
        return 0.0, "sin_costo"
    ub = (unidad_base or "").upper()
    if ub == "ML" and factor > 0:
        return round(cu * factor, 4), "costo_ref_botella"
    if ub == "UNI" and factor > 1:
        return round(cu, 4), "costo_ref_por_uni"
    return round(cu * factor if factor > 1 else cu, 4), "costo_ref"


def _leer_bd_mp_sistema() -> list[dict]:
    import os

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sh = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))
    vals = sh.worksheet("BD_MP_SISTEMA").get_all_values()
    hi = next(
        (i for i, r in enumerate(vals) if any((c or "").strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if hi is None:
        return []
    headers = [(c or "").strip() for c in vals[hi]]
    out: list[dict] = []
    for row in vals[hi + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        out.append(
            {
                headers[j]: (row[j].strip() if j < len(row) else "")
                for j in range(len(headers))
                if headers[j]
            }
        )
    return out


def proyectar_gastos_barra(
    *,
    desde: date,
    hasta: date,
    tipo: str = "barra",
) -> tuple[list[dict], dict]:
    from generar_ordenes_compra import (
        cargar_proveedores_por_tipo,
        enriquecer_linea_unidades_barra,
    )
    from inventario_stock_mp import agrupar_stock_par_por_mp, norm_mp

    dias = _dias_inclusive(desde, hasta)
    proveedores = cargar_proveedores_por_tipo(tipo)
    items_por_mp = _cargar_items_barra(proveedores)
    rows_maestro = _leer_bd_mp_sistema()
    agrupado = agrupar_stock_par_por_mp(rows_maestro)

    consumo_por_mp: dict[str, float] = {}
    par_por_mp: dict[str, float] = {}
    meta_por_mp: dict[str, dict] = {}
    for r in rows_maestro:
        cod = norm_mp(r.get("cod_mp_sistema"))
        if not cod:
            continue
        cd = _to_float(r.get("consumo_diario_calculado"), 0.0)
        par = _to_float(r.get("par_level"), 0.0)
        if cd > 0:
            consumo_por_mp[cod] = cd
        if par > 0:
            par_por_mp[cod] = par
        if cod not in meta_por_mp:
            meta_por_mp[cod] = {
                "nombre_mp": (r.get("nombre_mp") or cod).strip(),
                "unidad_base": (r.get("unidad_base") or "").strip(),
            }

    filas: list[dict] = []
    total_usd = 0.0
    sin_precio = 0

    candidatos = sorted(
        set(items_por_mp.keys()),
        key=lambda c: meta_por_mp.get(c, {}).get("nombre_mp", c),
    )

    for cod in candidatos:
        items = items_por_mp.get(cod) or []
        if not items:
            continue
        item = items[0]
        cp = item["cod_proveedor"]
        prov = proveedores.get(cp)
        if not prov:
            continue

        info = agrupado.get(cod, {})
        por_bod = info.get("por_bodega") or {}
        stock_b = _stock_barra(por_bod)
        consumo_d = consumo_por_mp.get(cod, _to_float(info.get("consumo_diario_calculado"), 0.0))
        par = par_por_mp.get(cod, _to_float(info.get("par_level"), 0.0))
        meta = meta_por_mp.get(cod, {})
        nombre = meta.get("nombre_mp") or info.get("nombre_mp") or cod
        ub = meta.get("unidad_base") or info.get("unidad_base") or item.get("unidad_base_sistema") or ""

        consumo_periodo = round(consumo_d * dias, 4)
        cant_base = round(max(0.0, consumo_periodo + par - stock_b), 4)
        if cant_base <= 0.001:
            continue

        factor = _to_float(item.get("factor_conversion"), 1.0) or 1.0
        linea = {
            "cod_mp_sistema": cod,
            "nombre_mp": nombre,
            "unidad_base": ub,
            "stock_barra": stock_b,
            "consumo_diario": round(consumo_d, 6),
            "par_level": round(par, 4),
            "consumo_periodo": consumo_periodo,
            "cantidad_base": cant_base,
            "descripcion_proveedor": item.get("descripcion_proveedor") or "",
            "factor_conversion": factor,
        }
        enriquecer_linea_unidades_barra(linea, item)

        cant_compra = int(linea.get("unidades_a_pedir") or 0)
        if cant_compra <= 0:
            continue

        uc = (linea.get("unidad_compra") or item.get("unidad_compra") or "unidad").strip()
        valor_u, fuente_p = _precio_unidad_compra(item, unidad_base=ub)
        if valor_u <= 0:
            valor_u, fuente_p = _precio_fallback_maestro(
                cod, unidad_base=ub, factor=factor, rows_mp=rows_maestro
            )
        if valor_u <= 0:
            sin_precio += 1

        # Si la línea pide cajas pero el informe es por botella, convertir
        if uc in ("caja", "cajas") and linea.get("botellas_equivalentes"):
            cant_compra = int(linea["botellas_equivalentes"])
            uc = "botellas"
        elif ub == "ML":
            uc = linea.get("unidad_compra") or "botellas"

        valor_total = round(cant_compra * valor_u, 2) if valor_u > 0 else 0.0
        total_usd += valor_total

        filas.append(
            {
                "fecha_desde": desde.isoformat(),
                "fecha_hasta": hasta.isoformat(),
                "dias_periodo": dias,
                "cod_proveedor": cp,
                "nombre_proveedor": prov["razon_social"],
                "cod_mp": cod,
                "nombre_mp": nombre,
                "descripcion_proveedor": linea.get("descripcion_proveedor") or "",
                "unidad_compra": uc,
                "cantidad": cant_compra,
                "valor_unitario_usd": round(valor_u, 4),
                "valor_total_usd": valor_total,
                "stock_barra": stock_b,
                "consumo_diario": round(consumo_d, 4),
                "par_level": round(par, 4),
                "consumo_periodo_base": consumo_periodo,
                "cantidad_base": cant_base,
                "texto_cantidad": linea.get("texto_cantidad") or "",
                "fuente_precio": fuente_p,
            }
        )

    filas.sort(key=lambda r: (-r["valor_total_usd"], r["nombre_proveedor"], r["nombre_mp"]))
    resumen = {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "dias": dias,
        "lineas": len(filas),
        "total_usd": round(total_usd, 2),
        "sin_precio": sin_precio,
        "mps_catalogo_barra": len(candidatos),
    }
    return filas, resumen


def _fmt_num_excel_latam(v: float, decimales: int = 2) -> str:
    """Número para Excel español: coma decimal, sin separador de miles."""
    if decimales <= 0:
        return str(int(round(v)))
    s = f"{round(v, decimales):.{decimales}f}"
    return s.replace(".", ",")


def escribir_csv(filas: list[dict], path: Path) -> None:
    """CSV con ; y coma decimal (abre bien en Excel Ecuador/LATAM)."""
    cols = [
        "fecha_desde",
        "fecha_hasta",
        "nombre_proveedor",
        "cod_mp",
        "nombre_mp",
        "descripcion_proveedor",
        "unidad_compra",
        "cantidad",
        "valor_unitario_usd",
        "valor_total_usd",
        "stock_barra",
        "consumo_diario",
        "par_level",
        "consumo_periodo_base",
        "cantidad_base",
        "texto_cantidad",
    ]
    num_2 = {
        "valor_unitario_usd",
        "valor_total_usd",
        "stock_barra",
        "consumo_diario",
        "par_level",
        "consumo_periodo_base",
        "cantidad_base",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=cols,
            delimiter=";",
            extrasaction="ignore",
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        for row in filas:
            out = dict(row)
            out["cantidad"] = str(int(out.get("cantidad") or 0))
            for k in num_2:
                if k in out:
                    dec = 4 if k == "valor_unitario_usd" else 2
                    out[k] = _fmt_num_excel_latam(float(out[k] or 0), dec)
            w.writerow(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Proyección gastos MP barra")
    ap.add_argument("--desde", default="2026-06-09", help="Fecha inicio (ISO)")
    ap.add_argument("--hasta", default="2026-07-09", help="Fecha fin inclusive (ISO)")
    ap.add_argument(
        "--csv",
        default="exports/proyeccion_gastos_barra_2026-06-09_2026-07-09.csv",
        help="Ruta CSV salida",
    )
    args = ap.parse_args()

    desde = _parse_fecha(args.desde)
    hasta = _parse_fecha(args.hasta)
    if hasta < desde:
        desde, hasta = hasta, desde

    filas, resumen = proyectar_gastos_barra(desde=desde, hasta=hasta)
    out = Path(args.csv)
    escribir_csv(filas, out)

    print(f"Proyeccion gastos BARRA: {desde} -> {hasta} ({resumen['dias']} dias)")
    print(f"Catalogo barra BOD-002: {resumen['mps_catalogo_barra']} MPs")
    print(f"Lineas con compra estimada: {resumen['lineas']}")
    print(f"Total proyectado: USD {resumen['total_usd']:,.2f}")
    if resumen["sin_precio"]:
        print(f"  WARN: {resumen['sin_precio']} lineas sin precio (valor_total=0)")
    print(f"CSV: {out.resolve()}")
    print()
    for r in filas[:25]:
        print(
            f"  {r['nombre_proveedor'][:22]:22} | {r['nombre_mp'][:28]:28} | "
            f"{r['cantidad']:>4} {r['unidad_compra'][:8]:8} | "
            f"${r['valor_unitario_usd']:>7.2f} | ${r['valor_total_usd']:>9.2f}"
        )
    if len(filas) > 25:
        print(f"  ... +{len(filas) - 25} lineas en CSV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
