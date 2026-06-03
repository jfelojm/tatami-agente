"""
Bodegas donde debe existir stock del pseudo-MP (SUB-xxx) en BD_MP_SISTEMA.

Solo bodegas del detalle de producción (BD_SUBRECETAS_DETALLE, líneas MP).
No se infieren desde la carta (BD_RECETAS_DETALLE): un plato en cocina que usa
el batch no implica que el lote se almacene en BOD-001.
"""

from __future__ import annotations

from codigos_subreceta import cod_sub_canonico
from bodegas_config import BODEGAS_DESCARGO_VENTA, normalizar_cod_bodega
from subrecetas_detalle import (
    agrupar_detalle_por_padre,
    cargar_bd_subrecetas_detalle,
    es_linea_mp_detalle,
)


def bodegas_para_subreceta(
    cod_sub: str,
    *,
    por_padre: dict[str, list[dict]] | None = None,
    sh=None,
) -> set[str]:
    """Bodegas donde la subreceta debe tener fila SUB-xxx (stock puede ser 0)."""
    cod = cod_sub_canonico(cod_sub)
    if not cod:
        return set()

    if por_padre is None:
        por_padre = agrupar_detalle_por_padre(cargar_bd_subrecetas_detalle(sh))

    bods: set[str] = set()
    for ln in por_padre.get(cod, []):
        if es_linea_mp_detalle(ln):
            b = normalizar_cod_bodega(ln.get("cod_bodega"))
            if b in BODEGAS_DESCARGO_VENTA:
                bods.add(b)

    return bods


def mapa_bodegas_todas_subs(
    subs_meta: dict[str, dict],
    *,
    por_padre: dict[str, list[dict]] | None = None,
    sh=None,
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cod in subs_meta:
        b = bodegas_para_subreceta(cod, por_padre=por_padre, sh=sh)
        if b:
            out[cod] = b
    return out
