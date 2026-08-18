"""
Stock y PAR por materia prima (multi-bodega).

Contrato:
  - par_level y consumo_diario_calculado son **globales por cod_mp** (misma en cada fila).
  - Para comparar vs PAR o generar órdenes: stock efectivo = **suma** de stock_actual
    en todas las bodegas activas donde exista fila del MP.
  - Reposición (órdenes / alertas bajo PAR): solo MPs con activa≠NO en BD_MP_SISTEMA.
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


def mp_fila_activa(row: dict) -> bool:
    """BD_MP_SISTEMA usa columna 'activa' (SI/NO). Vacío = activo."""
    raw = row.get("activa")
    if raw is None or str(raw).strip() == "":
        raw = row.get("activo")
    return str(raw or "SI").strip().upper() != "NO"


def agrupar_stock_par_por_mp(
    rows: list[dict],
    *,
    solo_bodegas_activas: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    cod_mp_norm -> {
        stock_total, par_level, nombre_mp, unidad_base, activa,
        por_bodega: {BOD-002: stock, ...}
    }
    """
    par_por_mp: dict[str, float] = {}
    meta: dict[str, dict[str, str]] = {}
    stock_por_bodega: dict[str, dict[str, float]] = defaultdict(dict)
    # Si alguna fila marca activa=NO, el MP queda inactivo para reposición.
    activa_por_mp: dict[str, bool] = {}

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
        if not mp_fila_activa(r):
            activa_por_mp[cod] = False
        else:
            activa_por_mp.setdefault(cod, True)
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
        activa = activa_por_mp.get(cod, True)
        out[cod] = {
            "cod_mp_sistema": cod,
            "nombre_mp": m["nombre_mp"],
            "unidad_base": m["unidad_base"],
            "stock_total": stock_total,
            "par_level": round(par, 4),
            "por_bodega": {k: round(v, 4) for k, v in sorted(por_bod.items())},
            "activa": activa,
            "bajo_par": par > 0 and stock_total < par,
            "cantidad_faltante": round(max(0.0, par - stock_total), 4) if par > 0 else 0.0,
        }
    return out


def aplicar_equiv_batches_barra_a_stock(
    agrupado: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Para reposición: stock efectivo de botella = stock botella + ml equivalentes
    contenidos en batches barra (SUB-051..054).

    Encaja con PAR de botella que ya incluye buffer del PAR del batch.
    """
    try:
        from descargo_subreceta import pseudo_mp_cod
        from subreceta_consumo_mp import cargar_mp_por_unidad_subreceta
        from subrecetas_bodegas_stock import SUBRECETAS_BARRA
    except Exception:
        return agrupado

    mp_por_unidad = cargar_mp_por_unidad_subreceta()
    extra: dict[str, float] = defaultdict(float)
    for cod_sub in SUBRECETAS_BARRA:
        pseudo = pseudo_mp_cod(cod_sub)
        info_sub = agrupado.get(pseudo) or agrupado.get(cod_sub)
        if not info_sub or not info_sub.get("activa", True):
            continue
        st = float(info_sub.get("stock_total") or 0)
        if st <= 0:
            continue
        for mp, per in (mp_por_unidad.get(cod_sub) or {}).items():
            if per > 0:
                extra[norm_mp(mp)] += st * per

    if not extra:
        return agrupado

    out: dict[str, dict[str, Any]] = {}
    for cod, info in agrupado.items():
        eq = float(extra.get(cod) or 0)
        if eq <= 0:
            out[cod] = info
            continue
        stock_bot = float(info.get("stock_total") or 0)
        stock_ef = round(stock_bot + eq, 4)
        par = float(info.get("par_level") or 0)
        info2 = dict(info)
        info2["stock_botella"] = stock_bot
        info2["stock_en_batch"] = round(eq, 4)
        info2["stock_total"] = stock_ef
        info2["bajo_par"] = par > 0 and stock_ef < par
        info2["cantidad_faltante"] = (
            round(max(0.0, par - stock_ef), 4) if par > 0 else 0.0
        )
        out[cod] = info2
    return out


def mps_bajo_par(
    rows: list[dict],
    *,
    solo_bodegas_activas: bool = True,
    solo_mps_activas: bool = True,
    incluir_equiv_batches_barra: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    MPs con par_level > 0 y stock_total < par.
    Por defecto omite MPs con activa=NO (no se reponen).
    Por defecto suma stock equivalente en batches barra (PAR ya trae ese buffer).
    """
    agrupado = agrupar_stock_par_por_mp(
        rows, solo_bodegas_activas=solo_bodegas_activas
    )
    if incluir_equiv_batches_barra:
        agrupado = aplicar_equiv_batches_barra_a_stock(agrupado)
    return {
        k: v
        for k, v in agrupado.items()
        if v.get("bajo_par") and (not solo_mps_activas or v.get("activa", True))
    }
