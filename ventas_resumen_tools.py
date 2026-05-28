"""
Resumen de ventas al cliente (hist_ventas) filtrado por BD_PRODUCTOS.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from matching_productos import cargar_bd_productos, construir_lookup
from ventas_smartmenu import estado_documento_excluye_neto_operativo

_catalogo_lookup: dict | None = None


def get_catalogo_lookup() -> dict:
    global _catalogo_lookup
    if _catalogo_lookup is None:
        _catalogo_lookup = construir_lookup(cargar_bd_productos())
    return _catalogo_lookup


def invalidar_cache_catalogo() -> None:
    global _catalogo_lookup
    _catalogo_lookup = None


def venta_en_catalogo(row: dict, lookup: dict | None = None) -> bool:
    """True si cod_smart_menu o nombre_producto está en BD_PRODUCTOS activos."""
    lookup = lookup or get_catalogo_lookup()
    cod = str(row.get("cod_smart_menu") or "").strip()
    if cod and cod in lookup:
        return True
    nombres = {
        (lookup[c]["nombre_producto"] or "").strip().upper()
        for c in lookup
        if (lookup[c].get("nombre_producto") or "").strip()
    }
    n = (row.get("nombre_producto") or "").strip().upper()
    return bool(n and n in nombres)


def nombre_producto_catalogo(row: dict, lookup: dict | None = None) -> str:
    """Nombre canónico desde catálogo; fallback nombre en venta."""
    lookup = lookup or get_catalogo_lookup()
    cod = str(row.get("cod_smart_menu") or "").strip()
    if cod in lookup:
        return (lookup[cod]["nombre_producto"] or "").strip()
    return (row.get("nombre_producto") or "").strip()


def _rango_periodo(periodo: str) -> tuple[str, str, str]:
    hoy = date.today()
    if periodo == "hoy":
        return hoy.isoformat(), hoy.isoformat(), "hoy"
    if periodo == "mes":
        return hoy.replace(day=1).isoformat(), hoy.isoformat(), hoy.strftime("%B %Y")
    lunes = hoy - timedelta(days=hoy.weekday())
    return (
        lunes.isoformat(),
        hoy.isoformat(),
        f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
    )


def calcular_resumen_ventas(
    rows: list[dict],
    *,
    lookup: dict | None = None,
    orden: str = "usd",
    limite: int | None = None,
    desglose_min_variedades: int = 2,
) -> dict:
    """
    Agrega ventas solo de productos en BD_PRODUCTOS.
    orden: 'usd' | 'cantidad'
    """
    lookup = lookup or get_catalogo_lookup()
    activas = [
        r
        for r in rows
        if not estado_documento_excluye_neto_operativo(r.get("estado_documento"))
    ]
    en_cat = [r for r in activas if venta_en_catalogo(r, lookup)]
    fuera = [r for r in activas if not venta_en_catalogo(r, lookup)]

    por_nombre: dict[str, dict] = defaultdict(lambda: {"cantidad": 0.0, "total_usd": 0.0})
    por_variedad: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"cantidad": 0.0, "total_usd": 0.0})
    )

    for r in en_cat:
        nombre = nombre_producto_catalogo(r, lookup)
        if not nombre:
            continue
        q = float(r.get("cantidad_vendida") or 0)
        usd = float(r.get("total") or 0)
        por_nombre[nombre]["cantidad"] += q
        por_nombre[nombre]["total_usd"] += usd
        var = (r.get("variedad_smart_menu") or "").strip() or "(sin variedad)"
        por_variedad[nombre][var]["cantidad"] += q
        por_variedad[nombre][var]["total_usd"] += usd

    key_fn = (
        (lambda x: x[1]["cantidad"])
        if (orden or "usd").strip().lower() == "cantidad"
        else (lambda x: x[1]["total_usd"])
    )
    ranking_items = sorted(por_nombre.items(), key=key_fn, reverse=True)
    if limite is not None and limite > 0:
        ranking_items = ranking_items[:limite]

    ranking = [
        {
            "plato": nombre,
            "cantidad": int(round(d["cantidad"])),
            "total_usd": round(d["total_usd"], 2),
            "en_catalogo": True,
        }
        for nombre, d in ranking_items
    ]

    desglose_variedades: dict[str, list[dict]] = {}
    for nombre, vars_map in por_variedad.items():
        if len(vars_map) < desglose_min_variedades:
            continue
        lineas = sorted(vars_map.items(), key=lambda x: x[1]["cantidad"], reverse=True)
        desglose_variedades[nombre] = [
            {
                "variedad": var,
                "cantidad": int(round(d["cantidad"])),
                "total_usd": round(d["total_usd"], 2),
            }
            for var, d in lineas
        ]

    total_usd = round(sum(d["total_usd"] for d in por_nombre.values()), 2)
    excl_usd = round(sum(float(r.get("total") or 0) for r in fuera), 2)

    return {
        "total_ventas_usd": total_usd,
        "total_usd_excluido_fuera_catalogo": excl_usd,
        "lineas_venta_catalogo": len(en_cat),
        "lineas_venta_excluidas": len(fuera),
        "productos_distintos": len(por_nombre),
        "orden": "cantidad" if orden == "cantidad" else "usd",
        "ranking": ranking,
        "desglose_variedades": desglose_variedades,
        "solo_catalogo": True,
    }


def formatear_resumen_ventas_whatsapp(
    resumen: dict,
    *,
    periodo_label: str,
    fecha_ini: str,
    fecha_fin: str,
    max_items: int = 50,
) -> str:
    """Texto listo para WhatsApp (sin markdown)."""
    lines: list[str] = []
    lines.append(
        f"Ventas {periodo_label} ({fecha_ini} al {fecha_fin}), solo productos en carta (BD_PRODUCTOS):"
    )
    total_hdr = resumen.get("total_ventas_usd_oficial") or resumen.get("total_ventas_usd", 0)
    tickets = resumen.get("tickets")
    hdr = f"Total vendido: {float(total_hdr):.2f} USD"
    if tickets:
        hdr += f" ({tickets} tickets/documentos)"
    hdr += f" | {resumen.get('productos_distintos', 0)} productos en carta"
    lines.append(hdr)
    if resumen.get("total_usd_excluido_fuera_catalogo", 0) > 0:
        lines.append(
            f"Nota: {resumen.get('lineas_venta_excluidas', 0)} lineas fuera de catalogo "
            f"({resumen.get('total_usd_excluido_fuera_catalogo', 0):.2f} USD) no aparecen en este listado."
        )

    orden = resumen.get("orden", "usd")
    subt = "por monto USD" if orden == "usd" else "por cantidad vendida"
    lines.append("")
    lines.append(f"Ranking {subt}:")

    ranking = resumen.get("ranking") or []
    for i, item in enumerate(ranking[:max_items], 1):
        lines.append(
            f"{i}. {item['plato']} - {item['cantidad']} unidades - {item['total_usd']:.2f} USD"
        )
    rest = len(ranking) - max_items
    if rest > 0:
        lines.append(f"... y {rest} productos mas (pide top N o otro corte si lo necesitas).")

    desglose = resumen.get("desglose_variedades") or {}
    if desglose.get("BAO"):
        lines.append("")
        lines.append("BAO por variedad:")
        for v in desglose["BAO"][:12]:
            lines.append(
                f"  - {v['variedad']}: {v['cantidad']} u, {v['total_usd']:.2f} USD"
            )

    lines.append("")
    lines.append("Fuente: hist_ventas (Smart Menu), sin documentos anulados.")
    return "\n".join(lines)
