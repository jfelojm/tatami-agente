"""
Descargo de inventario por líneas SUB en BD_RECETAS_DETALLE.

Requiere pseudo-MP en BD_MP_SISTEMA (prefijo SUB-) — ver sync_stock_subrecetas_maestro.py.
Activar con DESCARGO_SUBRECETAS=1 en .env.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from bodegas_config import bodega_permite_descargo_venta, normalizar_cod_bodega, resolver_bodega_receta
from recetas_detalle import es_linea_subreceta, filtrar_solo_mp, filtrar_solo_subreceta

PREFIJO_PSEUDO_MP = "SUB-"


def descargo_subrecetas_habilitado() -> bool:
    v = (os.getenv("DESCARGO_SUBRECETAS") or "0").strip().lower()
    return v in ("1", "true", "yes", "si", "s")


def pseudo_mp_cod(cod_subreceta: str) -> str:
    """cod_subreceta maestro → cod_mp_sistema en BD_MP_SISTEMA (SUB-051)."""
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod_subreceta)


def norm_cod_sub(cod: str) -> str:
    """Clave canónica en dicts de subreceta (SUB-051)."""
    from codigos_subreceta import cod_sub_canonico

    return cod_sub_canonico(cod)


def preparar_ingredientes_descargo(
    ingredientes: list[dict], *, incluir_sub: bool
) -> tuple[list[dict], list[dict]]:
    """Separa líneas MP y SUB según flag de descargo."""
    lineas_mp = filtrar_solo_mp(ingredientes)
    lineas_sub = filtrar_solo_subreceta(ingredientes) if incluir_sub else []
    return lineas_mp, lineas_sub


def calcular_consumo_sub(ingrediente: dict, cantidad_vendida: float) -> float:
    """Misma fórmula que MP en plato: cantidad × ventas × pct × (1+merma)."""
    try:
        gramaje = float(ingrediente.get("cantidad", 0))
        pct = float(ingrediente.get("pct_aplicacion", 1) or 1)
        merma = float(ingrediente.get("merma_pct", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return cantidad_vendida * gramaje * pct * (1 + merma)


def _sheet_float(v: Any, default: float = 0.0) -> float:
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, default)


def cargar_metadata_subrecetas() -> dict[str, dict]:
    """
    cod_subreceta (normalizado) → cabecera útil para descargo.
    Solo subrecetas activas.
    """
    from subrecetas_detalle import cargar_bd_subrecetas

    cab = cargar_bd_subrecetas()
    out: dict[str, dict] = {}
    activos = ("SI", "S", "YES", "1", "TRUE")
    for cod, info in cab.items():
        act = (info.get("activa") or "SI").strip().upper()
        if act not in activos:
            continue
        nk = norm_cod_sub(cod)
        if not nk:
            continue
        out[nk] = {
            **info,
            "cod_subreceta": nk,
            "cod_mp_pseudo": pseudo_mp_cod(nk),
            "nombre_subreceta": (info.get("nombre_subreceta") or "").strip(),
            "unidad": (info.get("unidad") or "gr").strip(),
            "costo_unitario_estandar": _sheet_float(
                info.get("costo_unitario_estandar"), 0.0
            ),
        }
    return out


def resolver_costo_unitario_sub(
    cod_sub: str,
    subs_meta: dict[str, dict],
    mp_info: dict | None,
) -> float:
    """Prioridad: costo en BD_SUBRECETAS; fallback costo_unitario_ref del pseudo-MP."""
    meta = subs_meta.get(norm_cod_sub(cod_sub), {})
    c = _sheet_float(meta.get("costo_unitario_estandar"), 0.0)
    if c > 0:
        return c
    if mp_info:
        return _sheet_float(mp_info.get("costo_unitario_ref"), 0.0)
    return 0.0


def construir_movimiento_sub(
    *,
    ing: dict,
    cantidad_vendida: float,
    cod_receta: str,
    variedad: str | None,
    cod_venta: str,
    fecha_v: str | None,
    hora_raw: str | None,
    mp_info: dict,
    bodega: str,
    subs_meta: dict[str, dict],
    iso_fecha_hora_mov,
) -> tuple[dict | None, tuple[str, str, float] | None, str | None]:
    """
    Retorna (movimiento, delta (cod_mp, bodega, consumo), mensaje_warn).
    """
    cod_sub = norm_cod_sub(ing.get("cod_subreceta") or "")
    if not cod_sub:
        return None, None, "cod_subreceta vacío"

    consumo = calcular_consumo_sub(ing, cantidad_vendida)
    if consumo <= 0:
        return None, None, None

    cod_mp = pseudo_mp_cod(cod_sub)
    meta = subs_meta.get(cod_sub, {})
    nombre_sub = (
        (ing.get("nombre_subreceta") or "").strip()
        or meta.get("nombre_subreceta", "")
        or cod_sub
    )
    unidad = (
        mp_info.get("unidad_base", "")
        or ing.get("unidad_base", "")
        or meta.get("unidad", "")
        or "gr"
    )
    costo_u = resolver_costo_unitario_sub(cod_sub, subs_meta, mp_info)

    cod_mov = (
        f"MOV-{fecha_v.replace('-', '') if fecha_v else '00000000'}-{cod_mp}-"
        f"{uuid.uuid4().hex[:16]}"
    )

    mov = {
        "cod_mov": cod_mov,
        "fecha": iso_fecha_hora_mov(fecha_v, hora_raw),
        "tipo_mov": "SALIDA_VENTA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": nombre_sub,
        "cod_bodega_origen": bodega,
        "cod_bodega_destino": None,
        "cantidad_mov": round(consumo, 4),
        "unidad_base": unidad,
        "costo_unitario": costo_u,
        "costo_total": round(consumo * costo_u, 4),
        "origen_documento": "VENTA_SMART_MENU",
        "num_documento": cod_venta,
        "registrado_por": "AGENTE",
        "observaciones": (
            f"Descargo venta SUB {cod_sub} {nombre_sub} | plato {cod_receta} | "
            f"var={variedad} | bod={bodega} | consumo={round(consumo, 4)} {unidad}"
        ),
    }
    return mov, (cod_mp, bodega, consumo), None


def procesar_linea_sub_venta(
    ing: dict,
    *,
    cantidad_vendida: float,
    cod_receta: str,
    variedad: str | None,
    cod_venta: str,
    fecha_v: str | None,
    hora_raw: str | None,
    mp_sistema: dict[tuple[str, str], dict],
    subs_meta: dict[str, dict],
    mp_key_fn,
    iso_fecha_hora_mov,
) -> tuple[dict | None, tuple[str, str, float] | None, str | None]:
    """
    Valida bodega y existencia pseudo-MP; delega en construir_movimiento_sub.
    mp_key_fn: (cod_mp, cod_bodega) -> tuple key usada en mp_sistema.
    """
    cod_sub = norm_cod_sub(ing.get("cod_subreceta") or "")
    cod_mp = pseudo_mp_cod(cod_sub)
    if not cod_mp:
        return None, None, "cod_subreceta vacío"

    bod_receta = normalizar_cod_bodega(ing.get("cod_bodega"))
    from subrecetas_bodegas_stock import bodegas_para_subreceta

    mp_fb = None
    for bod_try in sorted(bodegas_para_subreceta(cod_sub)):
        mp_fb = mp_sistema.get(mp_key_fn(cod_mp, bod_try))
        if mp_fb:
            break

    bodega, err_bod = resolver_bodega_receta(ing, mp_fb)
    if err_bod == "BODEGA_NO_DESCARGO":
        return (
            None,
            None,
            f"SUB {cod_sub}: bodega {ing.get('cod_bodega')} no descargable (solo cocina/barra)",
        )
    if err_bod or not bodega:
        return None, None, f"SUB {cod_sub}: sin cod_bodega en receta (cocina/barra)"

    # Batches de barra: stock vive en BOD-002 aunque la carta diga otra bodega
    from subrecetas_bodegas_stock import SUBRECETAS_BARRA

    if cod_sub in SUBRECETAS_BARRA:
        bodega = "BOD-002"

    if not bodega_permite_descargo_venta(bodega):
        return None, None, f"SUB {cod_sub}: bodega {bodega} no permitida para venta"

    mp_info = mp_sistema.get(mp_key_fn(cod_mp, bodega), mp_fb or {})
    if not mp_info:
        return (
            None,
            None,
            f"SUB {cod_sub}: falta pseudo-MP {cod_mp} en BD_MP_SISTEMA @ {bodega} "
            f"(ejecutar sync_stock_subrecetas_maestro.py)",
        )

    return construir_movimiento_sub(
        ing=ing,
        cantidad_vendida=cantidad_vendida,
        cod_receta=cod_receta,
        variedad=variedad,
        cod_venta=cod_venta,
        fecha_v=fecha_v,
        hora_raw=hora_raw,
        mp_info=mp_info,
        bodega=bodega,
        subs_meta=subs_meta,
        iso_fecha_hora_mov=iso_fecha_hora_mov,
    )
