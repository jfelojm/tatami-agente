"""
Detecta ventas Smart Menu que no deben descargar inventario automático.

Casos:
  1. PREFACTURA — documento consolidado (muchas líneas / cantidades altas).
     No aplica si tipo_documento=FACTURA (factura real en Smart Menu).
  2. BULK_DUPLICADO — línea qty>=20 el mismo día ya cubierto por facturas individuales.
     No aplica si tipo_documento=FACTURA.
  3. PUBLICIDAD — variedad / producto sin descargo (ya en BD_PRODUCTOS).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# Documento consolidado tipo prefactura (ej. doc 6580: tamago×58 + aguas + …)
PREFACTURA_MIN_LINEAS = 8
PREFACTURA_MIN_UNI_DOC = 80
PREFACTURA_MIN_RECETAS = 6
PREFACTURA_MIN_MAX_LINEA = 29

# Línea bulk duplicada (ej. doc 6078/6439: ×24 el mismo día que ventas sueltas)
BULK_DUPLICADO_MIN_QTY = 20

# Conteo físico barra — baseline; reglas omit solo aplican DESPUÉS de esta fecha
FECHA_CONTEO = "2026-05-29"
DESDE_DEFAULT = "2026-05-30"


def venta_es_post_conteo(venta: dict, *, fecha_conteo: str = FECHA_CONTEO) -> bool:
    f = str(venta.get("fecha") or "")[:10]
    return bool(f) and f > fecha_conteo


@dataclass(frozen=True)
class DocStats:
    num_documento: str
    fecha: str
    n_lineas: int
    n_recetas: int
    total_uni: float
    max_linea: float


def _norm_receta(cod: object) -> str:
    s = str(cod or "").strip()
    return s.lstrip("0") or s


def _norm_var(v: object) -> str:
    return str(v or "").strip().upper()


def es_publicidad_propaganda(venta: dict) -> bool:
    var = _norm_var(venta.get("variedad_smart_menu"))
    nom = _norm_var(venta.get("nombre_producto"))
    return "PUBLICIDAD" in var or "PUBLICIDAD" in nom or "PROPAGANDA" in var


def stats_por_documento(ventas: list[dict]) -> dict[str, DocStats]:
    por_doc: dict[str, list[dict]] = defaultdict(list)
    for v in ventas:
        doc = (v.get("num_documento") or "").strip()
        if doc:
            por_doc[doc].append(v)

    out: dict[str, DocStats] = {}
    for doc, lines in por_doc.items():
        recetas = {_norm_receta(x.get("cod_receta")) for x in lines if _norm_receta(x.get("cod_receta"))}
        qtys = [float(x.get("cantidad_vendida") or 0) for x in lines]
        out[doc] = DocStats(
            num_documento=doc,
            fecha=str(lines[0].get("fecha") or "")[:10],
            n_lineas=len(lines),
            n_recetas=len(recetas),
            total_uni=sum(qtys),
            max_linea=max(qtys) if qtys else 0.0,
        )
    return out


def es_tipo_factura_smartmenu(venta: dict) -> bool:
    """True si Smart Menu registró el documento como FACTURA (no NOTA/ticket)."""
    return _norm_var(venta.get("tipo_documento")) == "FACTURA"


def es_documento_prefactura(stats: DocStats) -> bool:
    if stats.n_lineas >= PREFACTURA_MIN_LINEAS and stats.total_uni >= PREFACTURA_MIN_UNI_DOC:
        return True
    if (
        stats.n_recetas >= PREFACTURA_MIN_RECETAS
        and stats.max_linea >= PREFACTURA_MIN_MAX_LINEA
    ):
        return True
    return False


def es_linea_bulk_duplicada(venta: dict, ventas_mismo_dia: list[dict]) -> bool:
    """True si qty>=20 y hay otras líneas misma receta/variedad ese día en otros docs."""
    qty = float(venta.get("cantidad_vendida") or 0)
    if qty < BULK_DUPLICADO_MIN_QTY:
        return False
    doc = (venta.get("num_documento") or "").strip()
    fecha = str(venta.get("fecha") or "")[:10]
    rec = _norm_receta(venta.get("cod_receta"))
    var = _norm_var(venta.get("variedad_smart_menu"))
    if not rec or not fecha:
        return False

    otras = 0
    for v in ventas_mismo_dia:
        if str(v.get("fecha") or "")[:10] != fecha:
            continue
        if (v.get("num_documento") or "").strip() == doc:
            continue
        if _norm_receta(v.get("cod_receta")) != rec:
            continue
        if _norm_var(v.get("variedad_smart_menu")) != var:
            continue
        q = float(v.get("cantidad_vendida") or 0)
        if 0 < q < BULK_DUPLICADO_MIN_QTY:
            otras += 1
    return otras > 0


def motivo_omitir_descargo(
    venta: dict,
    *,
    stats_docs: dict[str, DocStats],
    ventas_por_fecha: dict[str, list[dict]] | None = None,
    fecha_conteo: str = FECHA_CONTEO,
) -> str | None:
    """None = descargar; str = motivo para omitir. Solo aplica post-conteo."""
    if not venta_es_post_conteo(venta, fecha_conteo=fecha_conteo):
        return None

    if es_publicidad_propaganda(venta):
        return "PUBLICIDAD_PROPAGANDA"

    doc = (venta.get("num_documento") or "").strip()
    st = stats_docs.get(doc)
    if st and es_documento_prefactura(st) and not es_tipo_factura_smartmenu(venta):
        return f"PREFACTURA_DOC({doc},{st.n_lineas}ln,{st.total_uni:.0f}uni)"

    fecha = str(venta.get("fecha") or "")[:10]
    if ventas_por_fecha and fecha and not es_tipo_factura_smartmenu(venta):
        if es_linea_bulk_duplicada(venta, ventas_por_fecha.get(fecha, [])):
            return f"BULK_DUPLICADO(doc={doc},qty={venta.get('cantidad_vendida')})"

    return None


def clasificar_ventas(
    ventas: list[dict],
    *,
    fecha_conteo: str = FECHA_CONTEO,
) -> tuple[dict[str, str], dict[str, DocStats]]:
    """
    Retorna (cod_venta -> motivo omitir, stats por documento).
    Solo ventas que deben omitirse aparecen en el dict.
    """
    stats = stats_por_documento(ventas)
    por_fecha: dict[str, list[dict]] = defaultdict(list)
    for v in ventas:
        por_fecha[str(v.get("fecha") or "")[:10]].append(v)

    omit: dict[str, str] = {}
    for v in ventas:
        cod = (v.get("cod_venta") or "").strip()
        if not cod:
            continue
        m = motivo_omitir_descargo(
            v, stats_docs=stats, ventas_por_fecha=por_fecha, fecha_conteo=fecha_conteo
        )
        if m:
            omit[cod] = m
    return omit, stats
