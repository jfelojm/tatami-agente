"""
Catálogo de bodegas, matriz de traslados y reglas de descargo / confirmación.

Variables de entorno (opcionales):
  ALERTA_WA_JACKY   — confirmaciones BOD-001, BOD-005
  ALERTA_WA_EDUARDO — confirmaciones BOD-002, BOD-003
"""

from __future__ import annotations

import os
from typing import NamedTuple


class BodegaInfo(NamedTuple):
    cod: str
    nombre: str
    tipo: str  # FISICA | VIRTUAL
    notas: str
    activa: bool


BODEGAS: dict[str, BodegaInfo] = {
    "BOD-001": BodegaInfo("BOD-001", "Cocina", "FISICA", "Restaurante — área cocina", True),
    "BOD-002": BodegaInfo("BOD-002", "Barra", "FISICA", "Restaurante — área barra", True),
    "BOD-003": BodegaInfo("BOD-003", "Consignación", "VIRTUAL", "Barra / Cocina según ítem", True),
    "BOD-004": BodegaInfo("BOD-004", "Limpieza", "FISICA", "Administrativa — inactiva", False),
    "BOD-005": BodegaInfo("BOD-005", "Bodega externa", "FISICA", "Casa de Jacky (externa)", True),
}

# Descargo de ventas: solo cocina y barra
BODEGAS_DESCARGO_VENTA = frozenset({"BOD-001", "BOD-002"})

# Pares origen → destino (se aceptan ambos sentidos)
_TRASLADOS_DIRIGIDOS: frozenset[tuple[str, str]] = frozenset(
    {
        ("BOD-001", "BOD-002"),
        ("BOD-001", "BOD-005"),
        ("BOD-002", "BOD-001"),
        ("BOD-002", "BOD-003"),
        ("BOD-002", "BOD-005"),
        ("BOD-003", "BOD-002"),
        ("BOD-005", "BOD-001"),
        ("BOD-005", "BOD-002"),
    }
)

# Confirmación de bodega distinta al default del ítem
_BODEGA_A_ENV_JACKY = frozenset({"BOD-001", "BOD-005"})
_BODEGA_A_ENV_EDUARDO = frozenset({"BOD-002", "BOD-003"})


def normalizar_cod_bodega(cod: str | None) -> str:
    return (cod or "").strip().upper()


# Alias operativos (WhatsApp / voz) → código canónico
_ALIASES_BODEGA: dict[str, str] = {
    "COCINA": "BOD-001",
    "BARRA": "BOD-002",
    "CONSIGNACION": "BOD-003",
    "CONSIGNACIÓN": "BOD-003",
    "LIMPIEZA": "BOD-004",
    "EXTERNA": "BOD-005",
    "BODEGA EXTERNA": "BOD-005",
    "BODEGAISRAEL": "BOD-005",
    "ISRAEL": "BOD-005",
    "001": "BOD-001",
    "002": "BOD-002",
    "003": "BOD-003",
    "004": "BOD-004",
    "005": "BOD-005",
}


def _sin_tildes(s: str) -> str:
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def resolver_cod_bodega(cod: str | None) -> str:
    """
    Código BOD-00X desde entrada usuario: BOD-002, barra, consignación, 002, etc.
    Si no resuelve, devuelve normalizar_cod_bodega (p. ej. ya es BOD-002).
    """
    raw = (cod or "").strip().upper()
    if not raw:
        return ""
    if raw in BODEGAS:
        return raw
    compact = raw.replace(" ", "").replace("_", "-")
    if compact in BODEGAS:
        return compact
    if compact.startswith("BOD") and len(compact) >= 6 and compact[3] != "-":
        # BOD002 → BOD-002
        digits = compact[3:].lstrip("-")
        if digits.isdigit():
            candidato = f"BOD-{digits.zfill(3)}"
            if candidato in BODEGAS:
                return candidato
    alias = _sin_tildes(raw)
    if alias in _ALIASES_BODEGA:
        return _ALIASES_BODEGA[alias]
    alias_compact = _sin_tildes(compact)
    if alias_compact in _ALIASES_BODEGA:
        return _ALIASES_BODEGA[alias_compact]
    return raw


def bodega_activa(cod: str | None) -> bool:
    c = normalizar_cod_bodega(cod)
    info = BODEGAS.get(c)
    return bool(info and info.activa)


def bodega_permite_descargo_venta(cod: str | None) -> bool:
    return normalizar_cod_bodega(cod) in BODEGAS_DESCARGO_VENTA


def traslado_permitido(origen: str, destino: str) -> bool:
    o, d = resolver_cod_bodega(origen), resolver_cod_bodega(destino)
    if not o or not d or o == d:
        return False
    if not bodega_activa(o) or not bodega_activa(d):
        return False
    return (o, d) in _TRASLADOS_DIRIGIDOS


def nombre_bodega(cod: str | None) -> str:
    c = normalizar_cod_bodega(cod)
    info = BODEGAS.get(c)
    return info.nombre if info else (cod or "")


def numeros_confirmacion_bodega(cod_bodega: str | None) -> list[str]:
    """Números WA (solo dígitos) para confirmar ingreso en esa bodega."""
    c = normalizar_cod_bodega(cod_bodega)
    nums: list[str] = []
    if c in _BODEGA_A_ENV_JACKY:
        j = (os.getenv("ALERTA_WA_JACKY") or "").strip()
        if j:
            nums.append(j)
    if c in _BODEGA_A_ENV_EDUARDO:
        e = (os.getenv("ALERTA_WA_EDUARDO") or "").strip()
        if e:
            nums.append(e)
    return nums


def resolver_bodega_entrada_linea(
    item_prov: dict,
    *,
    bodega_override: str | None = None,
    confirmada: bool = False,
) -> tuple[str | None, str | None]:
    """
    Bodega destino para ENTRADA de factura (XML Drive o factura manual API).

    Regla operativa: BOD-003 (consignación virtual) es solo para ingresos manuales
    sin factura (traslados / altas operativas). Compras con factura → bodega física
    del ítem; si el catálogo dice BOD-003 se redirige a BOD-002 (barra propia).

    Retorna (cod_bodega, error_code).
    error_code: ITEM_SIN_BODEGA | BODEGA_INVALIDA | REQUIERE_CONFIRMACION
    """
    default = normalizar_cod_bodega(item_prov.get("cod_bodega_destino"))
    if default == "BOD-003":
        default = "BOD-002"
    if not default:
        return None, "ITEM_SIN_BODEGA"
    if not bodega_activa(default):
        return None, "BODEGA_INVALIDA"

    destino = normalizar_cod_bodega(bodega_override) if bodega_override else default
    if not destino:
        destino = default
    if not bodega_activa(destino):
        return None, "BODEGA_INVALIDA"

    if destino != default and not confirmada:
        return None, "REQUIERE_CONFIRMACION"

    return destino, None


def clave_linea_factura(ruc: str, cod_item_xml: str) -> str:
    return f"{(cod_item_xml or '').strip()}|{(ruc or '').strip()}"


def resolver_bodega_receta(ingrediente: dict, mp_fallback: dict | None = None) -> tuple[str | None, str | None]:
    """
    Bodega para SALIDA_VENTA desde línea de receta.
    error_code: RECETA_SIN_BODEGA | BODEGA_NO_DESCARGO
    """
    bod = normalizar_cod_bodega(ingrediente.get("cod_bodega"))
    if not bod and mp_fallback:
        bod = normalizar_cod_bodega(mp_fallback.get("cod_bodega"))
    if not bod:
        return None, "RECETA_SIN_BODEGA"
    if not bodega_permite_descargo_venta(bod):
        return None, "BODEGA_NO_DESCARGO"
    return bod, None
