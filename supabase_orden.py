"""Columna única por tabla para paginar Supabase con .range().

Sin ORDER BY, Postgres no garantiza el mismo orden entre páginas: en tablas
grandes se repiten u omiten filas. Toda paginación debe ordenar por una
columna única.
"""

PK_TABLA: dict[str, str] = {
    "mov_inventario": "cod_mov",
    "hist_ventas": "cod_venta",
    "sri_comprobantes_recibidos": "clave_acceso",
    "facturas_procesadas": "num_factura",
    "conteo_ciclo": "id",
    "conteo_linea": "id",
    "conteo_envio": "id",
    "conteo_envio_detalle": "id",
}


def orden_estable(tabla: str) -> str:
    return PK_TABLA.get(tabla, "id")
