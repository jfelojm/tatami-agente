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
