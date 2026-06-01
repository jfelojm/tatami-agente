"""
Stock y PAR por materia prima (multi-bodega).

Contrato:
  - par_level y consumo_diario_calculado son **globales por cod_mp** (misma en cada fila).
  - Para comparar vs PAR o generar órdenes: stock efectivo = **suma** de stock_actual
    en todas las bodegas activas donde exista fila del MP.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from bodegas_config import BODEGAS, bodega_activa, normalizar_cod_bodega


def norm_mp(cod: object) -> str:
    s = str(cod or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s.zfill(3) if len(s) <= 3 else s
    return s


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or default)
    except (TypeError, ValueError):
        return default


def agrupar_stock_par_por_mp(
    rows: list[dict],
    *,
    solo_bodegas_activas: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    cod_mp_norm -> {
        stock_total, par_level, nombre_mp, unidad_base,
        por_bodega: {BOD-002: stock, ...}
    }
    """
    par_por_mp: dict[str, float] = {}
    meta: dict[str, dict[str, str]] = {}
    stock_por_bodega: dict[str, dict[str, float]] = defaultdict(dict)

    for r in rows:
        cod = norm_mp(r.get("cod_mp_sistema"))
        if not cod:
            continue
        bod = normalizar_cod_bodega(r.get("cod_bodega"))
        if solo_bodegas_activas and bod and not bodega_activa(bod):
            continue
        stock = _to_float(r.get("stock_actual"))
        par = _to_float(r.get("par_level"))
        nombre = (r.get("nombre_mp") or "").strip()
        unidad = (r.get("unidad_base") or "").strip()
        if cod not in meta:
            meta[cod] = {"nombre_mp": nombre or cod, "unidad_base": unidad}
        else:
            if nombre:
                meta[cod]["nombre_mp"] = nombre
            if unidad and not meta[cod]["unidad_base"]:
                meta[cod]["unidad_base"] = unidad
        if par > 0:
            par_por_mp[cod] = par
        if bod:
            stock_por_bodega[cod][bod] = stock_por_bodega[cod].get(bod, 0.0) + stock

    out: dict[str, dict[str, Any]] = {}
    for cod in set(par_por_mp) | set(stock_por_bodega):
        por_bod = stock_por_bodega.get(cod, {})
        stock_total = round(sum(por_bod.values()), 4)
        par = par_por_mp.get(cod, 0.0)
        m = meta.get(cod, {"nombre_mp": cod, "unidad_base": ""})
        out[cod] = {
            "cod_mp_sistema": cod,
            "nombre_mp": m["nombre_mp"],
            "unidad_base": m["unidad_base"],
            "stock_total": stock_total,
            "par_level": round(par, 4),
            "por_bodega": {k: round(v, 4) for k, v in sorted(por_bod.items())},
            "bajo_par": par > 0 and stock_total < par,
            "cantidad_faltante": round(max(0.0, par - stock_total), 4) if par > 0 else 0.0,
        }
    return out


def mps_bajo_par(
    rows: list[dict],
    *,
    solo_bodegas_activas: bool = True,
) -> dict[str, dict[str, Any]]:
    """MPs con par_level > 0 y stock_total < par."""
    return {
        k: v
        for k, v in agrupar_stock_par_por_mp(
            rows, solo_bodegas_activas=solo_bodegas_activas
        ).items()
        if v.get("bajo_par")
    }
