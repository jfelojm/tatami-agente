"""
Consumo de MPs al usar subrecetas (cadena anidada SUB → SUB → MP).

Usado por PAR/consumo diario, planeación de compras y herramientas de auditoría.
"""

from __future__ import annotations

from collections import defaultdict

from costo_mp_canonico import norm_mp
from codigos_subreceta import cod_sub_canonico
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas,
    cargar_bd_subrecetas_detalle,
    es_linea_mp_detalle,
    es_linea_subreceta_hijo,
    orden_produccion,
)


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _norm_sub(cod: str) -> str:
    return cod_sub_canonico(cod)


def cargar_mp_por_unidad_subreceta() -> dict[str, dict[str, float]]:
    """
    cod_sub normalizado → {cod_mp → cantidad MP por 1 unidad de salida de la sub}.
    Expande MPs directas y subrecetas hijas (anidadas), en orden topológico.
    """
    cab = cargar_bd_subrecetas()
    por_padre = agrupar_detalle_por_padre(cargar_bd_subrecetas_detalle())
    cab_all = {c: cab[c] for c in por_padre if c in cab}

    rend_por_sub: dict[str, float] = {}
    for cod, info in cab.items():
        nk = _norm_sub(cod)
        if nk:
            rend_por_sub[nk] = _safe_float(info.get("rendimiento_estandar"))

    try:
        orden = orden_produccion(cab_all, por_padre)
    except ValueError:
        orden = []
    restantes = sorted(set(por_padre) - set(orden))
    orden = orden + restantes

    out: dict[str, dict[str, float]] = {}
    for cod_sub in orden:
        nk = _norm_sub(cod_sub)
        if not nk:
            continue
        rend = rend_por_sub.get(nk, 0.0)
        if rend <= 0:
            continue
        mp_map: dict[str, float] = defaultdict(float)
        for ln in por_padre.get(cod_sub, []):
            cant = _safe_float(ln.get("cantidad"))
            if cant <= 0:
                continue
            if es_linea_mp_detalle(ln):
                mp = norm_mp(ln.get("cod_mp_sistema") or "")
                if not mp:
                    continue
                merma = _safe_float(ln.get("merma_pct"))
                mp_map[mp] += (cant / rend) * (1.0 + merma)
            elif es_linea_subreceta_hijo(ln):
                hijo = _norm_sub(ln.get("cod_subreceta_hijo") or "")
                hijo_map = out.get(hijo, {})
                if not hijo_map:
                    continue
                factor = cant / rend
                for mp, per_unit in hijo_map.items():
                    mp_map[mp] += factor * per_unit
        if mp_map:
            out[nk] = dict(mp_map)
    return out


def explotar_subreceta_a_mp(
    cod_subreceta: str,
    cantidad_sub: float,
    *,
    mp_por_unidad: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """
    MPs (unidad base) consumidas al usar cantidad_sub unidades de salida de la subreceta.
    Incluye cadena completa de subrecetas hijas en BD_SUBRECETAS_DETALLE.
    """
    if cantidad_sub <= 0:
        return {}
    sub = _norm_sub(cod_subreceta)
    if not sub:
        return {}
    cache = mp_por_unidad if mp_por_unidad is not None else cargar_mp_por_unidad_subreceta()
    per = cache.get(sub, {})
    return {mp: round(cantidad_sub * pu, 6) for mp, pu in per.items() if pu > 0}
