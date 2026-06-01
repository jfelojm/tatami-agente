"""
Resumen de ventas al cliente (hist_ventas) filtrado por BD_PRODUCTOS.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import calendar

from matching_productos import cargar_bd_productos, construir_lookup
from ventas_smartmenu import estado_documento_excluye_neto_operativo

_MESES_ES = (
    "",
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_DIAS_ES = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)
_MES_NOMBRE_A_NUM: dict[str, int] = {}
for _i, _m in enumerate(_MESES_ES):
    if _m:
        _MES_NOMBRE_A_NUM[_m] = _i
        if len(_m) >= 3:
            _MES_NOMBRE_A_NUM[_m[:3]] = _i

_catalogo_lookup: dict | None = None


def etiqueta_fecha_ecuador(fecha_iso: str) -> dict:
    """Dia de la semana y etiqueta larga para una fecha YYYY-MM-DD (calendario local)."""
    d = date.fromisoformat(fecha_iso[:10])
    dia = _DIAS_ES[d.weekday()]
    return {
        "fecha": d.isoformat(),
        "dia_semana": dia,
        "etiqueta_fecha": f"{dia} {d.day} de {_MESES_ES[d.month]} de {d.year}",
    }


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


def _rango_periodo(periodo: str, hoy: date | None = None) -> tuple[str, str, str]:
    hoy = hoy or date.today()
    if periodo == "hoy":
        return hoy.isoformat(), hoy.isoformat(), "hoy"
    if periodo == "mes":
        return (
            hoy.replace(day=1).isoformat(),
            hoy.isoformat(),
            f"{_MESES_ES[hoy.month]} {hoy.year}",
        )
    lunes = hoy - timedelta(days=hoy.weekday())
    return (
        lunes.isoformat(),
        hoy.isoformat(),
        f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
    )


def resolver_rango_fechas(args: dict | None, hoy: date | None = None) -> tuple[str, str, str]:
    """
    Resuelve (fecha_ini, fecha_fin, etiqueta) desde args de tools de ventas.
    Prioridad: fecha_ini+fecha_fin > anio+mes/mes_nombre > periodo (hoy/semana/mes actual).
    """
    args = args or {}
    hoy = hoy or date.today()

    fi = (args.get("fecha_ini") or "").strip()
    ff = (args.get("fecha_fin") or "").strip()
    if fi and ff:
        d_ini = date.fromisoformat(fi[:10])
        d_fin = date.fromisoformat(ff[:10])
        if d_ini > d_fin:
            raise ValueError("fecha_ini no puede ser posterior a fecha_fin.")
        if (
            d_ini.day == 1
            and d_ini.month == d_fin.month
            and d_ini.year == d_fin.year
            and d_fin.day == calendar.monthrange(d_ini.year, d_ini.month)[1]
        ):
            label = f"{_MESES_ES[d_ini.month]} {d_ini.year}"
        else:
            label = f"{d_ini.strftime('%d/%m/%Y')} al {d_fin.strftime('%d/%m/%Y')}"
        return d_ini.isoformat(), d_fin.isoformat(), label

    mes_raw = args.get("mes")
    mes_nombre = (args.get("mes_nombre") or "").strip().lower()
    mes_num: int | None = None
    if mes_raw is not None and str(mes_raw).strip() != "":
        mes_num = int(mes_raw)
    elif mes_nombre:
        mes_num = _MES_NOMBRE_A_NUM.get(mes_nombre)
        if mes_num is None:
            raise ValueError(f"Mes no reconocido: {mes_nombre!r}. Usa nombre en espanol (ej. mayo).")

    if mes_num is not None:
        if mes_num < 1 or mes_num > 12:
            raise ValueError("mes debe estar entre 1 y 12.")
        anio_raw = args.get("anio")
        if anio_raw is not None and str(anio_raw).strip() != "":
            anio = int(anio_raw)
        else:
            anio = hoy.year
            if mes_num > hoy.month:
                anio -= 1
        ultimo = calendar.monthrange(anio, mes_num)[1]
        d_ini = date(anio, mes_num, 1)
        d_fin = date(anio, mes_num, ultimo)
        return d_ini.isoformat(), d_fin.isoformat(), f"{_MESES_ES[mes_num]} {anio}"

    periodo = (args.get("periodo") or "semana").strip().lower()
    if periodo not in ("hoy", "semana", "mes"):
        periodo = "semana"
    return _rango_periodo(periodo, hoy=hoy)


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
    incluir_productos: bool = True,
    max_items: int | None = 50,
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

    if incluir_productos:
        orden = resumen.get("orden", "usd")
        subt = "por monto USD" if orden == "usd" else "por cantidad vendida"
        lines.append("")
        lines.append(f"Ranking {subt}:")

        ranking = resumen.get("ranking") or []
        items = ranking if (max_items is None) else ranking[:max_items]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {item['plato']} - {item['cantidad']} unidades - {item['total_usd']:.2f} USD"
            )
        if max_items is not None:
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
    else:
        lines.append("")
        lines.append("¿Quieres tambien el detalle de productos? (ej: 'top 20' o 'detalle')")

    lines.append("")
    lines.append("Fuente: hist_ventas (Smart Menu), sin documentos anulados.")
    return "\n".join(lines)


def formatear_ventas_dia_whatsapp(
    *,
    etiqueta_fecha: str,
    total_ventas: float,
    tickets: int,
    fuente: str,
    platos: list[dict] | None = None,
    incluir_productos: bool = False,
    total_productos_distintos: int = 0,
) -> str:
    """Texto listo para WhatsApp de ventas de un solo dia (sin markdown)."""
    neto = (
        "neto tras descuentos"
        if (fuente or "").strip().lower().startswith("smart")
        else "aproximado desde hist_ventas"
    )
    lines = [
        f"El {etiqueta_fecha} se vendio un total de {float(total_ventas):.2f} USD ({neto}), en {int(tickets)} tickets."
    ]
    if incluir_productos and platos:
        lines.append("")
        lines.append("Productos mas vendidos ese dia:")
        for i, p in enumerate(platos, 1):
            cant = int(round(float(p.get("cantidad") or 0)))
            usd = float(p.get("total_usd") or 0)
            lines.append(f"{i}. {p.get('plato', '')} - {cant} unidades ({usd:.2f} USD)")
        if total_productos_distintos > len(platos):
            lines.append(
                f"Se vendieron {total_productos_distintos} productos distintos en total ese dia."
            )
    else:
        lines.append("")
        lines.append("¿Quieres tambien el detalle de productos/platos vendidos ese dia?")
    return "\n".join(lines)


def formatear_ventas_por_dia_whatsapp(
    *,
    periodo_label: str,
    fecha_ini: str,
    fecha_fin: str,
    dias: list[dict],
    total_periodo: float,
    tickets_periodo: int,
    fuente: str,
) -> str:
    """Desglose diario de ventas para WhatsApp (sin markdown)."""
    neto = (
        "neto tras descuentos"
        if (fuente or "").strip().lower().startswith("smart")
        else "neto tras descuentos (hist_ventas)"
    )
    lines = [
        f"Ventas por dia — {periodo_label} ({fecha_ini} al {fecha_fin}):",
        f"Total del periodo: {float(total_periodo):.2f} USD ({int(tickets_periodo)} tickets, {neto})",
        "",
    ]
    if not dias:
        lines.append("Sin ventas registradas en el periodo.")
    else:
        for item in dias:
            fd = date.fromisoformat(item["fecha"][:10])
            lines.append(
                f"{fd.day:02d}/{fd.month:02d} ({item.get('dia_semana', '')}): "
                f"{float(item.get('total_ventas', 0)):.2f} USD — {int(item.get('tickets', 0))} tickets"
            )
    lines.append("")
    lines.append(
        "Fuente: Smart Menu (neto tras descuentos)"
        if fuente == "Smart Menu"
        else "Fuente: hist_ventas (neto, sin documentos anulados; alineado a Smart Menu tras reconciliacion diaria)."
    )
    return "\n".join(lines)
