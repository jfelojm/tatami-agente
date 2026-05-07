import os
from collections import defaultdict
from datetime import date, timedelta

import gspread
import pytz
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from supabase import create_client

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
TZ = pytz.timezone("America/Guayaquil")

# Fuente oficial de total de ventas: Smart Menu grid (SUBTOTAL sin IVA por documento)
from ventas_smartmenu_total import calcular_total_smartmenu


def conectar_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def conectar_sheets():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def semana_cerrada(hoy: date):
    """Retorna (lunes, domingo) de la semana anterior cerrada."""
    lunes = hoy - timedelta(days=hoy.weekday() + 7)
    domingo = lunes + timedelta(days=6)
    return lunes, domingo


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0


# ── Sección 1 — Ventas ───────────────────────────────────────
def seccion_ventas(sb, fecha_ini, fecha_fin):
    res = (
        sb.table("hist_ventas")
        .select("num_documento,nombre_producto,cantidad_vendida,total")
        .gte("fecha", str(fecha_ini))
        .lte("fecha", str(fecha_fin))
        .execute()
    )

    rows = res.data
    if not rows:
        return "Sin datos de ventas para el periodo.", 0.0, 0

    # Total oficial: Smart Menu grid (SUBTOTAL sin IVA por documento), sumado día a día.
    total_ventas = 0.0
    d = fecha_ini
    while d <= fecha_fin:
        total_ventas += calcular_total_smartmenu(d.isoformat(), sin_iva=True).get("total", 0.0)
        d += timedelta(days=1)
    total_tickets = len(
        set(r.get("num_documento") for r in rows if (r.get("num_documento") or "").strip())
    )

    conteo = defaultdict(float)
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(SIN NOMBRE)"
        conteo[nombre] += _safe_float(r.get("cantidad_vendida"))

    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]

    lineas = [
        f"  Total ventas     : ${total_ventas:,.2f}",
        f"  Tickets emitidos : {total_tickets}",
        "  Top 5 vendidos:",
    ]
    for nombre, cant in top5:
        lineas.append(f"    - {nombre}: {int(cant)} unidades")

    return "\n".join(lineas), total_ventas, len(rows)


# ── Sección 2 — Food & Beverage Cost ────────────────────────
def seccion_costos(sb, fecha_ini, fecha_fin, total_ventas):
    res = (
        sb.table("mov_inventario")
        .select("tipo_mov,costo_total,origen_documento")
        .gte("fecha", str(fecha_ini))
        .lte("fecha", str(fecha_fin))
        .execute()
    )

    rows = res.data or []

    # Compras: ENTRADA (facturas) o ENTRADA_COMPRA (si existe en tu esquema)
    costo_compras = 0.0
    for r in rows:
        tipo = (r.get("tipo_mov") or "").strip().upper()
        origen = (r.get("origen_documento") or "").strip().upper()
        if tipo in ("ENTRADA", "ENTRADA_COMPRA") and (not origen or origen == "FACTURA"):
            costo_compras += _safe_float(r.get("costo_total"))

    # Salidas por venta
    costo_salidas = sum(
        _safe_float(r.get("costo_total"))
        for r in rows
        if (r.get("tipo_mov") or "").strip().upper() == "SALIDA_VENTA"
    )

    cost_pct = (costo_salidas / total_ventas) * 100 if total_ventas > 0 else 0.0

    lineas = [
        f"  Costo compras semana  : ${costo_compras:,.2f}",
        f"  Costo teorico insumos : ${costo_salidas:,.2f}",
        f"  Cost % (costo/ventas) : {cost_pct:.1f}%",
        "  Referencia sector     : 28-35% (gastronomia casual)",
    ]
    if costo_salidas == 0:
        lineas.append(
            "  NOTA: sin costos unitarios / movimientos de salida - revisar mov_inventario"
        )

    return "\n".join(lineas), costo_compras, costo_salidas


# ── Sección 3 — Alertas de precio ───────────────────────────
def seccion_precios(sb, fecha_ini, fecha_fin):
    res = (
        sb.table("hist_precios")
        .select(
            "descripcion_proveedor,precio_anterior,precio_nuevo,variacion_pct,cod_proveedor"
        )
        .gte("fecha_factura", str(fecha_ini))
        .lte("fecha_factura", str(fecha_fin))
        .execute()
    )

    rows = res.data or []
    if not rows:
        return "  Sin variaciones de precio detectadas esta semana."

    lineas = []
    for r in rows:
        var = _safe_float(r.get("variacion_pct")) * 100 if abs(_safe_float(r.get("variacion_pct"))) <= 1 else _safe_float(r.get("variacion_pct"))
        signo = "+" if var > 0 else "-"
        pa = _safe_float(r.get("precio_anterior"))
        pn = _safe_float(r.get("precio_nuevo"))
        desc = (r.get("descripcion_proveedor") or "").strip()
        lineas.append(f"  {signo} {desc} ${pa:.2f} -> ${pn:.2f} ({var:.1f}%)")
    return "\n".join(lineas)


# ── Sección 4 — Stock crítico ────────────────────────────────
def seccion_stock(sheet):
    ws = sheet.worksheet("BD_MP_SISTEMA")
    all_values = ws.get_all_values()
    if len(all_values) < 4:
        return "  BD_MP_SISTEMA sin datos."

    headers = [h.strip() for h in all_values[2]]

    criticos = []
    for row in all_values[3:]:
        if not any((c or "").strip() for c in row):
            continue
        r = dict(zip(headers, row))
        cod = str(r.get("cod_mp_sistema", "")).strip()
        if not cod:
            continue
        try:
            stock = float(str(r.get("stock_actual", "0") or "0").replace(",", "."))
            par = float(str(r.get("par_level", "0") or "0").replace(",", "."))
        except ValueError:
            continue
        if par > 0 and stock < par:
            criticos.append(
                (
                    str(r.get("nombre_mp", cod)).strip() or cod,
                    stock,
                    par,
                    str(r.get("unidad_base", "")).strip(),
                )
            )

    if not criticos:
        return "  Todos los insumos sobre par level."

    criticos.sort(key=lambda x: x[1] / x[2] if x[2] > 0 else 0)
    lineas = [f"  {'Insumo':<35} {'Stock':>8} {'Par':>8} {'Unidad':<6}"]
    lineas.append(f"  {'-'*60}")
    for nombre, stock, par, unidad in criticos[:10]:
        lineas.append(f"  {nombre:<35} {stock:>8.0f} {par:>8.0f} {unidad:<6}")
    if len(criticos) > 10:
        lineas.append(f"  ... y {len(criticos)-10} insumos mas bajo par level")

    return "\n".join(lineas)


def generar_reporte(dry_run: bool = False):
    hoy = datetime.now(TZ).date()
    fecha_ini, fecha_fin = semana_cerrada(hoy)

    print(f"\n{'='*60}")
    print("REPORTE SEMANAL TATAMI BAO BAR")
    print(f"Semana: {fecha_ini.strftime('%d/%m/%Y')} al {fecha_fin.strftime('%d/%m/%Y')}")
    print(f"Generado: {hoy.strftime('%d/%m/%Y')}")
    print(f"{'='*60}\n")

    sb = conectar_supabase()
    sheet = conectar_sheets()

    print("1. VENTAS")
    print("-" * 40)
    txt_ventas, total_ventas, _ = seccion_ventas(sb, fecha_ini, fecha_fin)
    print(txt_ventas)

    print("\n2. COSTOS")
    print("-" * 40)
    txt_costos, _, _ = seccion_costos(sb, fecha_ini, fecha_fin, total_ventas)
    print(txt_costos)

    print("\n3. ALERTAS DE PRECIO")
    print("-" * 40)
    print(seccion_precios(sb, fecha_ini, fecha_fin))

    print("\n4. STOCK CRITICO (top 10)")
    print("-" * 40)
    print(seccion_stock(sheet))

    print(f"\n{'='*60}")
    print("Para Moises - revisar y reenviar a socios.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import sys
    from datetime import datetime

    dry_run = "--dry-run" in sys.argv
    generar_reporte(dry_run)
