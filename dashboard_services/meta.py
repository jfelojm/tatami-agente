"""Metadatos globales del portal de dashboards."""

from __future__ import annotations


def ultima_fecha_tabla(sb, tabla: str, columna: str = "fecha") -> str | None:
    try:
        r = (
            sb.table(tabla)
            .select(columna)
            .order(columna, desc=True)
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if rows and rows[0].get(columna):
            return str(rows[0][columna])[:10]
    except Exception:
        pass
    return None


def primera_fecha_tabla(sb, tabla: str, columna: str = "fecha") -> str | None:
    try:
        r = (
            sb.table(tabla)
            .select(columna)
            .order(columna, desc=False)
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if rows and rows[0].get(columna):
            return str(rows[0][columna])[:10]
    except Exception:
        pass
    return None


def meta_dashboard(sb) -> dict:
    ventas_hasta = ultima_fecha_tabla(sb, "hist_ventas", "fecha")
    mov_hasta = ultima_fecha_tabla(sb, "mov_inventario", "fecha")
    mov_desde = primera_fecha_tabla(sb, "mov_inventario", "fecha")
    return {
        "ventas_hasta": ventas_hasta,
        "movimientos_hasta": mov_hasta,
        "movimientos_desde": mov_desde,
        "dashboards": [
            {"id": "ventas", "nombre": "Ventas", "estado": "activo"},
            {"id": "compras", "nombre": "Compras", "estado": "activo"},
            {"id": "rentabilidad", "nombre": "Rentabilidad", "estado": "activo"},
            {"id": "inventario", "nombre": "Inventario vivo", "estado": "activo"},
            {"id": "roturas", "nombre": "Roturas", "estado": "activo"},
            {"id": "confianza", "nombre": "Confianza inventario", "estado": "activo"},
        ],
    }
