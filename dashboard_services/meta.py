"""Metadatos globales del portal de dashboards."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ZONA_EC = ZoneInfo("America/Guayaquil")


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


def _neto_linea(row: dict) -> float:
    try:
        sub = float(row.get("subtotal") or 0)
        desc = float(row.get("descuento_valor") or 0)
        if sub or desc:
            return sub - desc
        return float(row.get("total") or 0)
    except (TypeError, ValueError):
        return 0.0


def frescura_ventas_hoy(sb) -> dict:
    """Resumen del día operativo EC para el banner del dashboard."""
    hoy = datetime.now(ZONA_EC).date().isoformat()
    out = {
        "hoy": hoy,
        "hoy_docs": 0,
        "hoy_neto": 0.0,
        "hoy_ultima_hora": None,
        "hoy_ultima_carga": None,
    }
    try:
        rows: list[dict] = []
        offset = 0
        while True:
            chunk = (
                sb.table("hist_ventas")
                .select(
                    "num_documento,hora,subtotal,descuento_valor,total,"
                    "estado_documento,creado_en"
                )
                .eq("fecha", hoy)
                .range(offset, offset + 999)
                .execute()
                .data
                or []
            )
            rows.extend(chunk)
            if len(chunk) < 1000:
                break
            offset += 1000
    except Exception:
        return out

    docs: set[str] = set()
    neto = 0.0
    max_hora = ""
    max_creado = ""
    for r in rows:
        est = (r.get("estado_documento") or "").strip().upper()
        if est in ("ANULADO", "ANULADA"):
            continue
        docs.add(str(r.get("num_documento") or ""))
        neto += _neto_linea(r)
        h = (r.get("hora") or "").strip()
        if h > max_hora:
            max_hora = h
        c = (r.get("creado_en") or "").strip()
        if c > max_creado:
            max_creado = c

    out["hoy_docs"] = len(docs)
    out["hoy_neto"] = round(neto, 2)
    out["hoy_ultima_hora"] = max_hora[:5] if max_hora else None
    if max_creado:
        try:
            from datetime import timezone

            raw = max_creado.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            # Supabase suele devolver timestamps naive en UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out["hoy_ultima_carga"] = dt.astimezone(ZONA_EC).strftime("%H:%M")
        except ValueError:
            out["hoy_ultima_carga"] = max_creado[11:16] if len(max_creado) >= 16 else None
    return out


def meta_dashboard(sb) -> dict:
    ventas_hasta = ultima_fecha_tabla(sb, "hist_ventas", "fecha")
    mov_hasta = ultima_fecha_tabla(sb, "mov_inventario", "fecha")
    mov_desde = primera_fecha_tabla(sb, "mov_inventario", "fecha")
    fresco = frescura_ventas_hoy(sb)
    return {
        "ventas_hasta": ventas_hasta,
        "movimientos_hasta": mov_hasta,
        "movimientos_desde": mov_desde,
        "ahora_ec": datetime.now(ZONA_EC).strftime("%Y-%m-%d %H:%M"),
        **fresco,
        "dashboards": [
            {"id": "ventas", "nombre": "Ventas", "estado": "activo"},
            {"id": "compras", "nombre": "Compras", "estado": "activo"},
            {"id": "rentabilidad", "nombre": "Rentabilidad", "estado": "activo"},
            {"id": "inventario", "nombre": "Inventario vivo", "estado": "activo"},
            {"id": "roturas", "nombre": "Roturas", "estado": "activo"},
            {"id": "confianza", "nombre": "Confianza inventario", "estado": "activo"},
        ],
    }
