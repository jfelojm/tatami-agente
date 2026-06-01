"""
Traslados entre bodegas: mov_inventario + costo heredado del origen.

Único punto de registro de TRASLADO_SALIDA / TRASLADO_ENTRADA (WhatsApp y futuros clientes).
Política de costo:
  - Al registrar: copiar costo_unitario_ref de la bodega origen al par de movimientos.
  - Al recalcular maestro: recalcular_stock_sheets usa TRASLADO_ENTRADA con costo y hereda
    de otra bodega del mismo MP si la fila destino está en cero.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from bodegas_config import normalizar_cod_bodega, nombre_bodega, traslado_permitido


def costo_ref_desde_filas_maestro(
    rows: list[dict],
    cod_mp: str,
    bodega_origen: str,
) -> float:
    """Lee costo_unitario_ref de BD_MP_SISTEMA (filas dict) para origen."""
    origen = normalizar_cod_bodega(bodega_origen)
    cod = (cod_mp or "").strip()
    for r in rows:
        if str(r.get("cod_mp_sistema", "")).strip() != cod:
            continue
        if normalizar_cod_bodega(r.get("cod_bodega", "")) != origen:
            continue
        try:
            return float(r.get("costo_unitario_ref") or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def construir_par_movimientos_traslado(
    *,
    cod_mp: str,
    nombre_mp: str,
    bodega_origen: str,
    bodega_destino: str,
    cantidad: float,
    unidad_base: str,
    costo_unitario_ref: float,
    cod_base: str,
    fecha_iso: str,
    registrado_por: str,
    observaciones: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dos filas listas para insert en mov_inventario."""
    obs = observaciones or f"Traslado {bodega_origen} → {bodega_destino}"
    costo_u = round(float(costo_unitario_ref or 0), 6)
    base: dict[str, Any] = {
        "cod_mp_sistema": cod_mp,
        "nombre_mp": nombre_mp,
        "cantidad_mov": cantidad,
        "unidad_base": unidad_base,
        "origen_documento": "TRASLADO",
        "num_documento": cod_base,
        "registrado_por": registrado_por,
        "observaciones": obs,
    }
    if costo_u > 0:
        base["costo_unitario"] = costo_u
        base["costo_total"] = round(costo_u * cantidad, 4)

    salida = {
        **base,
        "cod_mov": cod_base + "-SAL",
        "fecha": fecha_iso,
        "tipo_mov": "TRASLADO_SALIDA",
        "cod_bodega_origen": normalizar_cod_bodega(bodega_origen),
        "cod_bodega_destino": None,
    }
    entrada = {
        **base,
        "cod_mov": cod_base + "-ENT",
        "fecha": fecha_iso,
        "tipo_mov": "TRASLADO_ENTRADA",
        "cod_bodega_origen": None,
        "cod_bodega_destino": normalizar_cod_bodega(bodega_destino),
    }
    return salida, entrada


def registrar_traslado_mp(
    sb,
    *,
    cod_mp: str,
    bodega_origen: str,
    bodega_destino: str,
    cantidad: float,
    nombre_mp: str,
    unidad_base: str,
    costo_unitario_ref: float,
    registrado_por: str = "AGENTE_WHATSAPP",
    recalcular_sheets: bool = True,
    tz: datetime | None = None,
) -> dict[str, Any]:
    """
    Persiste traslado en Supabase y opcionalmente recalcula stock/costo en Sheets.
    Raises ValueError si el par bodega no está permitido o cantidad <= 0.
    """
    origen = normalizar_cod_bodega(bodega_origen)
    destino = normalizar_cod_bodega(bodega_destino)
    if cantidad <= 0:
        raise ValueError("La cantidad debe ser mayor que cero.")
    if not traslado_permitido(origen, destino):
        raise ValueError(
            f"Traslado no permitido: {nombre_bodega(origen)} → {nombre_bodega(destino)}."
        )

    now = tz or datetime.now(timezone.utc)
    cod_base = f"TRA-{now.strftime('%Y%m%d%H%M%S')}"
    salida, entrada = construir_par_movimientos_traslado(
        cod_mp=cod_mp.strip(),
        nombre_mp=nombre_mp,
        bodega_origen=origen,
        bodega_destino=destino,
        cantidad=cantidad,
        unidad_base=unidad_base,
        costo_unitario_ref=costo_unitario_ref,
        cod_base=cod_base,
        fecha_iso=now.isoformat(),
        registrado_por=registrado_por,
    )
    sb.table("mov_inventario").insert(salida).execute()
    sb.table("mov_inventario").insert(entrada).execute()

    if recalcular_sheets:
        try:
            from recalcular_stock_sheets import recalcular_produccion

            recalcular_produccion(cod_mp_filtro=cod_mp.strip())
        except Exception as e:
            print(f"  WARN: recalcular tras traslado: {e}")

    return {
        "cod_mov": cod_base,
        "costo_unitario": salida.get("costo_unitario"),
        "bodega_origen": origen,
        "bodega_destino": destino,
    }


def validar_mov_traslado_lleva_costo(mov: dict) -> bool:
    """True si TRASLADO_ENTRADA/SALIDA sin costo (solo cantidad) — histórico legacy."""
    tipo = (mov.get("tipo_mov") or "").strip()
    if tipo not in ("TRASLADO_ENTRADA", "TRASLADO_SALIDA"):
        return True
    try:
        return float(mov.get("costo_unitario") or 0) > 0
    except (TypeError, ValueError):
        return False
