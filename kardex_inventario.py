from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_sb: Client | None = None


def _get_sb() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _sb


def get_kardex(cod_mp_sistema: str, fecha_desde: str, fecha_hasta: str) -> dict:
    """
    Retorna dict con movimientos, resumen y delta.
    fecha_desde / fecha_hasta: 'YYYY-MM-DD'
    """
    sb = _get_sb()
    res = (
        sb.table("mov_inventario")
        .select("*")
        .eq("cod_mp_sistema", cod_mp_sistema)
        .gte("fecha", fecha_desde + "T00:00:00")
        .lte("fecha", fecha_hasta + "T23:59:59")
        .order("fecha")
        .execute()
    )
    movs = res.data or []

    entradas = [m for m in movs if m["tipo_mov"] == "ENTRADA"]
    salidas = [m for m in movs if m["tipo_mov"].startswith("SALIDA")]
    ajustes = [m for m in movs if m["tipo_mov"].startswith("AJUSTE")]

    total_entradas = sum(float(m["cantidad_mov"]) for m in entradas)
    total_salidas = sum(float(m["cantidad_mov"]) for m in salidas)
    total_ajustes = sum(float(m["cantidad_mov"]) for m in ajustes)

    nombre_mp = movs[0]["nombre_mp"] if movs else cod_mp_sistema
    unidad = movs[0]["unidad_base"] if movs else ""

    return {
        "cod_mp_sistema": cod_mp_sistema,
        "nombre_mp": nombre_mp,
        "unidad": unidad,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "movimientos": movs,
        "entradas": entradas,
        "salidas": salidas,
        "ajustes": ajustes,
        "total_entradas": total_entradas,
        "total_salidas": total_salidas,
        "total_ajustes": total_ajustes,
    }


def inferir_causa(kardex: dict, stock_snapshot: float, conteo_fisico: float) -> list[str]:
    causas = []
    delta = conteo_fisico - stock_snapshot

    # Entradas sin costo (posible factura no registrada correctamente)
    sin_costo = [e for e in kardex["entradas"] if not e.get("costo_unitario")]
    if sin_costo:
        causas.append(f"• {len(sin_costo)} entrada(s) sin costo registrado — revisar facturas")

    # Delta negativo sistemático — posible gramaje incorrecto
    if delta < 0 and kardex["total_salidas"] > 0:
        ratio = abs(delta) / kardex["total_salidas"]
        if ratio > 0.1:
            causas.append(
                "• Consumo real supera receta sistemáticamente — revisar gramaje en BD_RECETAS_DETALLE"
            )

    # Sin entradas en el período — posible compra no registrada
    if delta < 0 and not kardex["entradas"]:
        causas.append("• Sin entradas registradas en el período — ¿compra no facturada?")

    # Ajustes previos en el período
    if kardex["ajustes"]:
        causas.append(f"• {len(kardex['ajustes'])} ajuste(s) de inventario físico anterior en el período")

    if not causas:
        causas.append("• Sin causa evidente — verificar conteo físico")

    return causas


def formatear_kardex_wa(
    kardex: dict,
    stock_snapshot: float,
    conteo_fisico: float,
    costo_ref: float | None,
) -> str:
    delta = conteo_fisico - stock_snapshot
    pct = (delta / stock_snapshot * 100) if stock_snapshot else 0
    valor = delta * costo_ref if costo_ref else None

    def fmt_fecha(f: str) -> str:
        try:
            return datetime.fromisoformat(str(f)).strftime("%d/%m")
        except Exception:
            return str(f)[:10]

    def fmt_num(n: float) -> str:
        return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    lineas = [
        f"📦 KARDEX — {kardex['nombre_mp']} (cod {kardex['cod_mp_sistema']})",
        f"📅 {kardex['fecha_desde']} — {kardex['fecha_hasta']}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Stock inicial:     {fmt_num(stock_snapshot)} {kardex['unidad']}",
        "",
        "ENTRADAS",
    ]

    if kardex["entradas"]:
        for e in kardex["entradas"]:
            costo_str = (
                f"  ${float(e['costo_unitario']):.4f}/{kardex['unidad']}"
                if e.get("costo_unitario")
                else ""
            )
            lineas.append(
                f"{fmt_fecha(e['fecha'])}  {e.get('origen_documento', ''):<14} "
                f"+{fmt_num(float(e['cantidad_mov']))} {kardex['unidad']}{costo_str}"
            )
    else:
        lineas.append("(sin entradas)")

    lineas += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total entradas:    +{fmt_num(kardex['total_entradas'])} {kardex['unidad']}",
        "",
        "SALIDAS",
    ]

    # Agrupar salidas por día
    salidas_por_dia: dict[str, float] = {}
    for s in kardex["salidas"]:
        dia = fmt_fecha(s["fecha"])
        salidas_por_dia[dia] = salidas_por_dia.get(dia, 0) + float(s["cantidad_mov"])

    if salidas_por_dia:
        for dia, cant in salidas_por_dia.items():
            lineas.append(f"{dia}  Venta receta       -{fmt_num(cant)} {kardex['unidad']}")
    else:
        lineas.append("(sin salidas)")

    lineas += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total salidas:     -{fmt_num(kardex['total_salidas'])} {kardex['unidad']}",
        "",
        f"Stock teórico:     {fmt_num(stock_snapshot + kardex['total_entradas'] - kardex['total_salidas'])} {kardex['unidad']}",
        f"Stock físico:      {fmt_num(conteo_fisico)} {kardex['unidad']}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{'⚠️' if abs(pct) >= 1 else 'ℹ️'} Diferencia:     {fmt_num(delta)} {kardex['unidad']} ({pct:+.1f}%)",
    ]

    if valor is not None:
        lineas.append(f"💰 Valor delta:    ${valor:,.2f}")

    lineas += ["", "🔍 Posible causa:"]
    lineas.extend(inferir_causa(kardex, stock_snapshot, conteo_fisico))

    return "\n".join(lineas)


def generar_csv(kardex: dict) -> bytes:
    """Retorna CSV como bytes para enviar por WhatsApp."""
    campos = [
        "fecha",
        "tipo_mov",
        "cantidad_mov",
        "unidad_base",
        "costo_unitario",
        "costo_total",
        "cod_bodega_origen",
        "cod_bodega_destino",
        "origen_documento",
        "num_documento",
        "observaciones",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=campos, extrasaction="ignore")
    writer.writeheader()
    for m in kardex["movimientos"]:
        writer.writerow({k: m.get(k, "") for k in campos})
    return output.getvalue().encode("utf-8-sig")


def generar_xlsx(kardex: dict) -> bytes:
    """Retorna Excel como bytes para enviar por WhatsApp."""
    try:
        import openpyxl
    except ImportError as e:
        raise ImportError("pip install openpyxl") from e

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = kardex["nombre_mp"][:31]

    campos = [
        "fecha",
        "tipo_mov",
        "cantidad_mov",
        "unidad_base",
        "costo_unitario",
        "costo_total",
        "cod_bodega_origen",
        "cod_bodega_destino",
        "origen_documento",
        "num_documento",
        "observaciones",
    ]
    ws.append(campos)
    for m in kardex["movimientos"]:
        ws.append([str(m.get(k, "") or "") for k in campos])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
