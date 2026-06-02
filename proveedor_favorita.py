"""
Corporación Favorita: mismo RUC, dos proveedores lógicos (Supermaxi / Titán).

Supermaxi → cod_proveedor 136 (default histórico).
Titán Cuenca → cod_proveedor 175 cuando estab SRI = 219 en num_factura.
"""

from __future__ import annotations

import re

RUC_CORPORACION_FAVORITA = "1790016919001"
COD_PROVEEDOR_SUPERMAXI = "136"
COD_PROVEEDOR_TITAN = "175"
ESTABS_TITAN = frozenset({"219"})


def _solo_digitos_ruc(ruc: str) -> str:
    return re.sub(r"\D+", "", (ruc or "").strip())


def es_ruc_favorita(ruc: str) -> bool:
    return _solo_digitos_ruc(ruc) == RUC_CORPORACION_FAVORITA


def estab_desde_num_factura(num_factura: str) -> str:
    """Primer segmento del número SRI (establecimiento)."""
    parts = (num_factura or "").strip().split("-")
    return parts[0].strip() if parts else ""


def formato_compra_desde_estab(estab: str) -> str:
    """Solo lógica Favorita: 219 → TITAN; cualquier otro estab → SUPERMAXI."""
    if (estab or "").strip() in ESTABS_TITAN:
        return "TITAN"
    return "SUPERMAXI"


def formato_compra_para_factura(ruc: str, num_factura: str = "") -> str:
    """
    SUPERMAXI / TITAN solo si el emisor es Corporación Favorita.
    Otros proveedores: cadena vacía (no aplicar regla Supermaxi/Titán).
    """
    if not es_ruc_favorita(ruc):
        return ""
    return formato_compra_desde_estab(estab_desde_num_factura(num_factura))


def resolver_cod_proveedor_factura(
    ruc: str,
    num_factura: str = "",
    *,
    lookup: dict[str, str] | None = None,
) -> str:
    """
    cod_proveedor para matching de catálogo y pendientes.
    Favorita: estab 219 → Titán (175); cualquier otro estab → Supermaxi (136).
    Otros RUC: lookup BD_PROV (comportamiento previo).
    """
    if es_ruc_favorita(ruc):
        estab = estab_desde_num_factura(num_factura)
        if estab in ESTABS_TITAN:
            return COD_PROVEEDOR_TITAN
        return COD_PROVEEDOR_SUPERMAXI

    if lookup is None:
        return ""
    for key in _ruc_claves_lookup(ruc):
        v = lookup.get(key)
        if v:
            return v
    return ""


def _ruc_claves_lookup(ruc: str) -> list[str]:
    raw = (ruc or "").strip().strip("'")
    out: list[str] = []
    if raw:
        out.append(raw)
    digits = _solo_digitos_ruc(raw)
    if digits:
        if len(digits) <= 13:
            d13 = digits.zfill(13) if len(digits) < 13 else digits
            if len(d13) == 13 and d13 not in out:
                out.append(d13)
        if digits not in out:
            out.append(digits)
    return out


def aplicar_default_lookup_favorita(lookup: dict[str, str]) -> None:
    """Lookup plano RUC→cod: default Supermaxi (136), nunca Titán por RUC solo."""
    for key in _ruc_claves_lookup(RUC_CORPORACION_FAVORITA):
        if key:
            lookup[key] = COD_PROVEEDOR_SUPERMAXI


def meta_proveedor_factura(factura: dict) -> dict:
    """Campos meta para facturas_procesadas."""
    num = (factura.get("num_factura") or "").strip()
    ruc = (factura.get("ruc") or "").strip()
    estab = estab_desde_num_factura(num)
    cod = resolver_cod_proveedor_factura(ruc, num)
    out: dict = {
        "estab": estab,
        "cod_proveedor": cod,
        "formato_compra": formato_compra_para_factura(ruc, num),
    }
    razon = (factura.get("razon_social") or "").strip()
    if razon:
        out["razon_social"] = razon
    return out
