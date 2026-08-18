"""
Bodegas donde debe existir stock del pseudo-MP (SUB-xxx) en BD_MP_SISTEMA.

Regla de negocio (no inferir desde carta ni solo desde detalle):
  - Barra (BOD-002): SUB-051..054 (batches de barra).
  - Cocina: todas las demás activas en BOD-001 (restaurante) y BOD-005 (externa).
"""

from __future__ import annotations

from codigos_subreceta import cod_sub_canonico

# Batches de barra — única bodega BOD-002
SUBRECETAS_BARRA = frozenset(
    {
        "SUB-051",  # Batch negroni
        "SUB-052",  # batch tokio mule
        "SUB-053",  # batch ron banana negroni
        "SUB-054",  # Batch mojito de coco
    }
)

BODEGAS_SUB_BARRA = frozenset({"BOD-002"})
BODEGAS_SUB_COCINA = frozenset({"BOD-001", "BOD-005"})

_primera_prod_batch_cache: dict[str, str | None] = {}


def fecha_primera_produccion_batch(cod_sub: str) -> str | None:
    """
    ISO fecha/hora de la primera ENTRADA PRODUCCION_SUBRECETA del batch (SUB-051..054).
    Ventas anteriores no deben generar SALIDA_VENTA del semi.
    """
    cod = cod_sub_canonico(cod_sub)
    if not cod or cod not in SUBRECETAS_BARRA:
        return None
    if cod in _primera_prod_batch_cache:
        return _primera_prod_batch_cache[cod]

    import os

    from descargo_subreceta import pseudo_mp_cod

    cod_mp = pseudo_mp_cod(cod)
    if not cod_mp:
        _primera_prod_batch_cache[cod] = None
        return None

    try:
        from supabase import create_client

        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        rows = (
            sb.table("mov_inventario")
            .select("fecha")
            .eq("cod_mp_sistema", cod_mp)
            .eq("origen_documento", "PRODUCCION_SUBRECETA")
            .eq("tipo_mov", "ENTRADA")
            .order("fecha")
            .limit(1)
            .execute()
            .data
            or []
        )
        fp = (rows[0].get("fecha") or "").strip() if rows else None
    except Exception:
        fp = None

    _primera_prod_batch_cache[cod] = fp or None
    return _primera_prod_batch_cache[cod]


def venta_anterior_a_primera_produccion_batch(
    cod_sub: str, fecha_venta: str | None, hora_venta: str | None
) -> bool:
    """True si la venta es anterior a la primera producción del batch."""
    fp = fecha_primera_produccion_batch(cod_sub)
    if not fp:
        return False
    fv = (fecha_venta or "").strip()[:10]
    hv = (hora_venta or "").strip()[:8]
    if not fv:
        return False
    if hv and len(hv) >= 5:
        if len(hv) == 5:
            hv = hv + ":00"
        venta_iso = f"{fv}T{hv}"
    else:
        venta_iso = f"{fv}T23:59:59"
    return venta_iso < fp[:19]



def bodegas_para_subreceta(
    cod_sub: str,
    *,
    por_padre: dict | None = None,
    sh=None,
) -> set[str]:
    """Bodegas donde la subreceta debe tener fila SUB-xxx en BD_MP_SISTEMA."""
    del por_padre, sh  # regla fija; parámetros legacy por compatibilidad de firma
    cod = cod_sub_canonico(cod_sub)
    if not cod:
        return set()
    if cod in SUBRECETAS_BARRA:
        return set(BODEGAS_SUB_BARRA)
    return set(BODEGAS_SUB_COCINA)


def mapa_bodegas_todas_subs(
    subs_meta: dict[str, dict],
    *,
    por_padre: dict | None = None,
    sh=None,
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cod in subs_meta:
        b = bodegas_para_subreceta(cod, por_padre=por_padre, sh=sh)
        if b:
            out[cod] = b
    return out
