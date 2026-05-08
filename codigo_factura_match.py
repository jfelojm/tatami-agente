"""
Normaliza códigos de ítem en XML de factura para hacer match con BD_ITEMS_PROV.

Caso COLEMUN: el código trae un guion y un número de orden de línea en la factura
(ej. ABC123-1 vs ABC123-2). Para matchear hay que quitar el sufijo -N al final.
"""

import re

# Si la razón social del emisor contiene alguno de estos tokens, aplica la regla.
_STRIP_ORDEN_TOKENS = ("COLEMUN",)
# RUCs (13 dígitos) con el mismo criterio de sufijo en código de ítem (p. ej. COLEMUN).
_STRIP_ORDEN_RUCS = frozenset({"0992613092001"})


def _ruc_normalizado(ruc: str) -> str:
    digits = re.sub(r"\D+", "", (ruc or "").strip().lstrip("'"))
    if not digits:
        return ""
    if len(digits) < 13:
        digits = digits.zfill(13)
    return digits


def aplica_strip_sufijo_orden_factura(razon_social: str = "", ruc: str = "") -> bool:
    u = (razon_social or "").strip().upper()
    if any(t in u for t in _STRIP_ORDEN_TOKENS):
        return True
    return _ruc_normalizado(ruc) in _STRIP_ORDEN_RUCS


def normalizar_cod_item_para_match(
    cod: str,
    razon_social: str = "",
    ruc: str = "",
    *,
    cod_proveedor: str = "",
    cod_proveedores_strip: frozenset[str] | None = None,
) -> str:
    """
    Quita espacios, prefijo ' de texto en Sheets, ceros a la izquierda.
    Para COLEMUN (razón social, RUC conocido o cod_proveedor en BD_PROV): quita
    sufijo -<dígitos> al final (orden de línea en la factura).
    """
    s = (cod or "").strip().lstrip("'")
    s = re.sub(r"\s+", "", s)
    strip = aplica_strip_sufijo_orden_factura(razon_social, ruc)
    if (
        not strip
        and cod_proveedores_strip
        and cod_proveedor
        and cod_proveedor in cod_proveedores_strip
    ):
        strip = True
    if strip:
        s = re.sub(r"-\d+$", "", s)
    s = s.lstrip("0")
    return s


def cod_proveedores_strip_sufijo_desde_bd_prov(values: list[list[str]]) -> frozenset[str]:
    """
    Lee filas de la hoja BD_PROV (get_all_values()) y devuelve cod_proveedor
    donde la razón social indica facturas con sufijo de orden en el código.
    """
    header_row_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_proveedor" for c in row):
            header_row_idx = i
            break
    if header_row_idx is None:
        return frozenset()
    headers = [(c or "").strip() for c in values[header_row_idx]]
    try:
        ic = headers.index("cod_proveedor")
        ir = headers.index("razon_social")
    except ValueError:
        return frozenset()
    try:
        iu = headers.index("RUC")
    except ValueError:
        iu = None
    out: set[str] = set()
    for row in values[header_row_idx + 1 :]:
        if len(row) <= max(ic, ir):
            continue
        cod = (row[ic] or "").strip()
        razon = (row[ir] or "").strip()
        ruc_row = (row[iu] or "").strip() if iu is not None and len(row) > iu else ""
        if cod and aplica_strip_sufijo_orden_factura(razon, ruc_row):
            out.add(cod)
    return frozenset(out)
