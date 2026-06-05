"""
Genera órdenes de compra sugeridas por proveedor (stock vs PAR).

Uso:
  python generar_ordenes_compra.py --tipo barra
  python generar_ordenes_compra.py --tipo cocina
  python generar_ordenes_compra.py --tipo todos
  python generar_ordenes_compra.py --tipo barra --sin-ventana   # ignora ventana_pedido
  python generar_ordenes_compra.py --tipo barra --json logs/ordenes_barra.json
  python generar_ordenes_compra.py --tipo barra --produccion    # escribe hoja ORDENES_COMPRA

Criterio:
  - PAR global por cod_mp (columna par_level en BD_MP_SISTEMA).
  - Stock para comparar vs PAR = suma de stock_actual en **todas** las bodegas activas del MP.
  - Ingreso de compra sigue cod_bodega_destino del ítem (ej. BOD-002 barra).
  - Proveedores filtrados por BD_PROV.Tipo y proveedor_inventario=SI.
  - Cantidad a pedir = PAR - stock_total; unidades compra = ceil(cant_base / factor_conversion).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pytz
from dotenv import load_dotenv

load_dotenv(override=True)

TIPO_A_BODEGA = {
    "BARRA": "BOD-002",
    "COCINA": "BOD-001",
}

# MPs con fila en estas bodegas entran al anexo "stock en cero" (alertas barra).
BODEGAS_STOCK_ALERTA_BARRA = frozenset({"BOD-002", "BOD-003"})

STOCK_CERO_TOL = 0.001

DIA_MAP = {"LUN": 0, "MAR": 1, "MIE": 2, "JUE": 3, "VIE": 4, "SAB": 5, "DOM": 6}


def _norm_cod_mp(cod: object) -> str:
    s = str(cod or "").strip()
    return s.zfill(3) if s else ""


def _norm_cod_prov(cod: object) -> str:
    s = str(cod or "").strip()
    return s.zfill(3) if s.isdigit() else s


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v or "").replace(",", ".").strip() or default)
    except (TypeError, ValueError):
        return default


def _find_header(values: list[list[str]], key: str) -> tuple[int, list[str]] | None:
    for i, row in enumerate(values):
        headers = [c.strip() for c in row]
        if key in headers:
            return i, headers
    return None


def _row_dict(headers: list[str], row: list[str]) -> dict[str, str]:
    return {headers[j]: (row[j].strip() if j < len(row) else "") for j in range(len(headers))}


def cargar_proveedores_por_tipo(tipo: str) -> dict[str, dict]:
    """tipo: barra | cocina | todos"""
    from procesar_facturas_drive import _get_sheet

    tipo_u = (tipo or "todos").strip().upper()
    sh = _get_sheet()
    vals = sh.worksheet("BD_PROV").get_all_values()
    found = _find_header(vals, "cod_proveedor")
    if not found:
        return {}
    hi, headers = found
    out: dict[str, dict] = {}
    for row in vals[hi + 1 :]:
        if not any(c.strip() for c in row):
            continue
        r = _row_dict(headers, row)
        cod = _norm_cod_prov(r.get("cod_proveedor", ""))
        if not cod:
            continue
        inv = (r.get("proveedor_inventario") or "").strip().upper()
        if inv in ("NO", ""):
            continue
        activo = (r.get("activo") or "SI").strip().upper()
        if activo == "NO":
            continue
        t = (r.get("Tipo") or "").strip().upper()
        if tipo_u != "TODOS":
            if tipo_u == "BARRA" and t != "BARRA":
                continue
            if tipo_u == "COCINA" and t != "COCINA":
                continue
        out[cod] = {
            "cod_proveedor": cod,
            "razon_social": (r.get("razon_social") or cod).strip(),
            "ruc": (r.get("RUC") or r.get("ruc") or "").strip(),
            "tipo": t,
            "ventana_pedido": (r.get("ventana_pedido") or "").strip(),
            "condicion_pago": (r.get("condicion_pago") or "").strip(),
            "lead_time_dias": int(_to_float(r.get("lead_time_dias"), 1) or 1),
            "frecuencia_compra_dias": _to_float(r.get("frecuencia_compra_dias"), 0.0) or None,
            "contacto_whatsapp": (r.get("contacto_whatsapp") or "").strip(),
            "contacto_nombre": (r.get("contacto_nombre") or "").strip(),
        }
    return out


def proveedor_activo_hoy(ventana: str, hoy: date) -> bool:
    if not ventana:
        return True
    dias = [d.strip().upper() for d in ventana.split(",") if d.strip()]
    return hoy.weekday() in [DIA_MAP[d] for d in dias if d in DIA_MAP]


def cargar_stock_por_mp_bodega(tipo: str) -> dict[str, dict]:
    """cod_mp -> línea con stock_total (todas las bodegas) vs par global."""
    from inventario_stock_mp import mps_bajo_par
    from whatsapp_webhook import leer_bd_mp_sistema

    bodega_pedido = TIPO_A_BODEGA.get(tipo.upper()) if tipo.upper() in TIPO_A_BODEGA else None
    out: dict[str, dict] = {}
    for cod, info in mps_bajo_par(leer_bd_mp_sistema()).items():
        out[cod] = {
            "cod_mp_sistema": cod,
            "nombre_mp": info["nombre_mp"],
            "unidad_base": info["unidad_base"],
            "stock_actual": info["stock_total"],
            "stock_por_bodega": info["por_bodega"],
            "par_level": info["par_level"],
            "cantidad_base": info["cantidad_faltante"],
            "cod_bodega": bodega_pedido or "GLOBAL",
        }
    return out


def cargar_items_prov_por_mp(proveedores: dict[str, dict], bodega: str | None) -> dict[str, list[dict]]:
    from procesar_facturas_drive import cargar_bd_items_prov

    provs = set(proveedores.keys())
    mp_items: dict[str, list[dict]] = defaultdict(list)
    for it in cargar_bd_items_prov():
        cp = _norm_cod_prov(it.get("cod_proveedor"))
        if cp not in provs:
            continue
        cod = _norm_cod_mp(it.get("cod_mp_sistema"))
        if not cod:
            continue
        bod_item = (it.get("cod_bodega_destino") or "").strip().upper()
        if bodega:
            # El ítem define la bodega de ingreso; no mezclar cocina/barra por Tipo del proveedor.
            if not bod_item or bod_item != bodega:
                continue
        factor = _to_float(it.get("factor_conversion"), 1.0) or 1.0
        try:
            prioridad = int(_to_float(it.get("prioridad"), 99))
        except (TypeError, ValueError):
            prioridad = 99
        mp_items[cod].append(
            {
                "cod_proveedor": cp,
                "descripcion_proveedor": (it.get("descripcion_proveedor") or "").strip(),
                "unidad_compra": (it.get("unidad_compra") or it.get("unidad_base_sistema") or "").strip(),
                "unidad_base_sistema": (it.get("unidad_base_sistema") or "").strip(),
                "factor_conversion": factor,
                "cod_bodega_destino": bod_item,
                "prioridad": prioridad,
            }
        )
    for cod in mp_items:
        mp_items[cod].sort(key=lambda x: (x["prioridad"], x["cod_proveedor"]))
    return mp_items


def _texto_item_barra(ln: dict, item: dict) -> str:
    nombre = (ln.get("nombre_mp") or "").strip()
    desc = (ln.get("descripcion_proveedor") or item.get("descripcion_proveedor") or "").strip()
    return f"{nombre} {desc}".upper()


def _pedir_en_unidades_barra(ln: dict, item: dict) -> bool:
    """Hielo, pulpas, bolsas, etc. — no son botellas."""
    t = _texto_item_barra(ln, item)
    for kw in (
        "HIELO",
        "ICE",
        "PULPA",
        "FUNDA",
        "BOLSA",
        "SACO",
        "PAQUETE",
    ):
        if kw in t:
            return True
    return False


def _pedir_en_botellas_uni_barra(ln: dict, item: dict, uc_raw: str) -> bool:
    """Refrescos/cervezas en base uni (o caja)."""
    if _pedir_en_unidades_barra(ln, item):
        return False
    if uc_raw in ("caja", "cajas"):
        return True
    t = _texto_item_barra(ln, item)
    for kw in (
        "CERVEZA",
        "VINO",
        "WHISKY",
        "TEQUILA",
        "GIN ",
        "RON ",
        "VODKA",
        "MEZCAL",
        "COCA",
        "SPRITE",
        "FANTA",
        "AGUA",
        "SODA",
        "CLUB ",
        "KION",
        "PAULANER",
        "LATITUDE",
        "CORONA",
        "HEINEKEN",
        "BUCHANAN",
        "HENNESSY",
        "BOTELLA",
        "LATA ",
        "330",
        "500ML",
        "750",
        "GRB",
        "RGB",
    ):
        if kw in t:
            return True
    return False


def enriquecer_linea_unidades_barra(ln: dict, item: dict) -> None:
    """
    Cantidades legibles para barra: botellas (bebidas), ml (espírituos), unidades (hielo, etc.).
    Rellena texto_cantidad, unidades_a_pedir, unidad_compra.
    """
    ub = (ln.get("unidad_base") or item.get("unidad_base_sistema") or "").strip().upper()
    uc_raw = (item.get("unidad_compra") or "").strip().lower()
    factor = _to_float(ln.get("factor_conversion") or item.get("factor_conversion"), 1.0) or 1.0
    cant_base = _to_float(ln.get("cantidad_base"))

    if ub == "ML" and factor > 0:
        botellas = max(0, math.ceil(cant_base / factor)) if cant_base > 0 else 0
        vol = int(factor) if factor == int(factor) else round(factor, 1)
        ln["unidades_a_pedir"] = botellas
        ln["unidad_compra"] = "botella" if botellas == 1 else "botellas"
        ln["ml_por_unidad"] = vol
        ln["cantidad_ml_pedido"] = round(botellas * factor, 1)
        ln["texto_cantidad"] = (
            f"{botellas} {'botella' if botellas == 1 else 'botellas'} de {vol:g} ml"
        )
        return

    if ub == "UNI":
        cant = max(0, math.ceil(cant_base)) if cant_base > 0 else 0
        if uc_raw in ("caja", "cajas") and factor > 1:
            cajas = max(0, math.ceil(cant / factor)) if cant > 0 else 0
            botellas_pedido = cajas * int(factor) if cajas else 0
            ln["unidades_a_pedir"] = cajas
            ln["unidad_compra"] = "caja" if cajas == 1 else "cajas"
            ln["botellas_equivalentes"] = botellas_pedido
            ln["texto_cantidad"] = (
                f"{botellas_pedido} botellas — pedir {cajas} "
                f"{'caja' if cajas == 1 else 'cajas'} × {int(factor)}"
            )
        elif _pedir_en_botellas_uni_barra(ln, item, uc_raw):
            ln["unidades_a_pedir"] = cant
            ln["unidad_compra"] = "botella" if cant == 1 else "botellas"
            ln["texto_cantidad"] = (
                f"{cant} {'botella' if cant == 1 else 'botellas'}"
            )
        else:
            ln["unidades_a_pedir"] = cant
            ln["unidad_compra"] = "unidad" if cant == 1 else "unidades"
            ln["texto_cantidad"] = (
                f"{cant} {'unidad' if cant == 1 else 'unidades'}"
            )
        return

    if ub == "GR":
        unidades = (
            max(0, math.ceil(cant_base / factor)) if factor > 0 else max(0, math.ceil(cant_base))
        )
        ln["unidades_a_pedir"] = unidades
        ln["unidad_compra"] = "unidad" if unidades == 1 else "unidades"
        ln["texto_cantidad"] = (
            f"{unidades} {'unidad' if unidades == 1 else 'unidades'}"
        )
        return

    unidades = max(0, math.ceil(cant_base / factor)) if factor > 0 else max(0, math.ceil(cant_base))
    ln["unidades_a_pedir"] = unidades
    ln["unidad_compra"] = uc_raw or ub.lower() or "unidades"
    ln["texto_cantidad"] = (
        f"{unidades} {'unidad' if unidades == 1 else 'unidades'}"
    )


def _linea_cantidad_texto(ln: dict) -> str:
    return (ln.get("texto_cantidad") or "").strip() or (
        f"{ln.get('unidades_a_pedir', 0)} {ln.get('unidad_compra', '')}".strip()
    )


def saludo_por_hora() -> str:
    tz = pytz.timezone("America/Guayaquil")
    h = datetime.now(tz).hour
    if h < 12:
        return "Buenos días"
    if h < 19:
        return "Buenas tardes"
    return "Buenas noches"


def formatear_mensaje_whatsapp(prov: dict, lineas: list[dict]) -> str:
    items_txt = []
    for ln in lineas:
        desc = ln.get("descripcion_proveedor") or ln.get("nombre_mp")
        items_txt.append(f"• {desc} — {_linea_cantidad_texto(ln)}")
    return (
        f"{saludo_por_hora()}, le saludo de Tatami.\n"
        f"Orden de compra — {prov['razon_social']}\n\n"
        + "\n".join(items_txt)
        + "\n\nMuchas gracias."
    )


def formatear_orden_texto(prov: dict, lineas: list[dict], *, fecha: date, tipo: str) -> str:
    sep = "=" * 72
    out = [
        sep,
        f"ORDEN DE COMPRA SUGERIDA",
        f"Fecha: {fecha.strftime('%d/%m/%Y')}  |  Área: {tipo.upper()}",
        sep,
        f"Proveedor: {prov['razon_social']}",
        f"Código:    {prov['cod_proveedor']}",
    ]
    if prov.get("ruc"):
        out.append(f"RUC:       {prov['ruc']}")
    if prov.get("condicion_pago"):
        out.append(f"Pago:      {prov['condicion_pago']}")
    if prov.get("lead_time_dias"):
        out.append(f"Lead time: {prov['lead_time_dias']} día(s)")
    out.extend(["", f"{'#':<4} {'Descripción':<36} {'Pedir':>18} {'Stock':>10} {'PAR':>10}"])
    out.append("-" * 72)
    for i, ln in enumerate(lineas, 1):
        desc = (ln.get("descripcion_proveedor") or ln.get("nombre_mp"))[:36]
        pedir_txt = _linea_cantidad_texto(ln)[:18]
        out.append(
            f"{i:<4} {desc:<36} {pedir_txt:>18} "
            f"{ln['stock_actual']:>10.2f} {ln['par_level']:>10.2f}"
        )
    out.append(sep)
    out.append("")
    out.append("MENSAJE WHATSAPP:")
    out.append(formatear_mensaje_whatsapp(prov, lineas))
    out.append("")
    return "\n".join(out)


def generar_ordenes(
    *,
    tipo: str = "barra",
    sin_ventana: bool = False,
    hoy: date | None = None,
) -> list[dict]:
    hoy = hoy or date.today()
    tipo_l = tipo.strip().lower()
    bodega = TIPO_A_BODEGA.get(tipo_l.upper()) if tipo_l in ("barra", "cocina") else None

    proveedores = cargar_proveedores_por_tipo(tipo_l)
    mps_bajo = cargar_stock_por_mp_bodega(tipo_l)
    items_por_mp = cargar_items_prov_por_mp(proveedores, bodega)

    pedidos: dict[str, list[dict]] = defaultdict(list)

    for cod_mp, mp in mps_bajo.items():
        items = items_por_mp.get(cod_mp)
        if not items:
            continue
        item = items[0]
        cp = item["cod_proveedor"]
        if cp not in proveedores:
            continue
        if not sin_ventana and not proveedor_activo_hoy(proveedores[cp]["ventana_pedido"], hoy):
            continue

        cant_base = mp["cantidad_base"]
        factor = item["factor_conversion"] or 1.0
        unidades = math.ceil(cant_base / factor) if factor > 0 else math.ceil(cant_base)
        if unidades <= 0:
            continue

        linea = {
            **mp,
            "descripcion_proveedor": item["descripcion_proveedor"],
            "unidad_compra": item["unidad_compra"],
            "unidades_a_pedir": unidades,
            "factor_conversion": factor,
        }
        if tipo_l == "barra":
            enriquecer_linea_unidades_barra(linea, item)
        pedidos[cp].append(linea)

    ordenes = []
    for cp in sorted(pedidos.keys(), key=lambda c: proveedores[c]["razon_social"]):
        prov = proveedores[cp]
        lineas = sorted(pedidos[cp], key=lambda x: x["nombre_mp"])
        ordenes.append(
            {
                "cod_proveedor": cp,
                "proveedor": prov,
                "n_items": len(lineas),
                "lineas": lineas,
                "mensaje_whatsapp": formatear_mensaje_whatsapp(prov, lineas),
                "texto_orden": formatear_orden_texto(prov, lineas, fecha=hoy, tipo=tipo_l),
            }
        )
    return ordenes


def _mp_en_area_barra(info: dict) -> bool:
    por = info.get("por_bodega") or {}
    return bool(BODEGAS_STOCK_ALERTA_BARRA.intersection(por))


def _linea_pedido_propuesta(
    mp: dict,
    item: dict,
    *,
    tipo: str,
) -> dict:
    """Misma lógica que generar_ordenes para texto de cantidad sugerida."""
    cant_base = float(mp.get("cantidad_base") or 0)
    factor = _to_float(item.get("factor_conversion"), 1.0) or 1.0
    linea = {
        **mp,
        "descripcion_proveedor": item.get("descripcion_proveedor"),
        "unidad_compra": item.get("unidad_compra"),
        "unidades_a_pedir": math.ceil(cant_base / factor) if factor > 0 else math.ceil(cant_base),
        "factor_conversion": factor,
    }
    if tipo.strip().lower() == "barra":
        enriquecer_linea_unidades_barra(linea, item)
    else:
        linea["texto_cantidad"] = _linea_cantidad_texto(linea)
    return linea


def _indice_lineas_ordenes(ordenes: list[dict]) -> dict[str, dict]:
    """cod_mp -> línea ya incluida en órdenes generadas."""
    out: dict[str, dict] = {}
    for oc in ordenes:
        for ln in oc.get("lineas") or []:
            cod = _norm_cod_mp(ln.get("cod_mp_sistema"))
            if cod:
                out[cod] = ln
    return out


def listar_mp_stock_cero_para_alertas(
    *,
    tipo: str = "barra",
    sin_ventana: bool = False,
    hoy: date | None = None,
    ordenes: list[dict] | None = None,
) -> list[dict]:
    """
    MPs con stock total <= 0 en bodegas barra/consignación.
    Incluye pedido propuesto si hay catálogo; si no, motivo general.
    """
    from inventario_stock_mp import agrupar_stock_par_por_mp
    from whatsapp_webhook import leer_bd_mp_sistema

    hoy = hoy or date.today()
    tipo_l = tipo.strip().lower()
    bodega = TIPO_A_BODEGA.get(tipo_l.upper()) if tipo_l in ("barra", "cocina") else None

    proveedores = cargar_proveedores_por_tipo(tipo_l)
    mps_bajo = cargar_stock_por_mp_bodega(tipo_l)
    items_por_mp = cargar_items_prov_por_mp(proveedores, bodega)
    en_orden = _indice_lineas_ordenes(ordenes or [])
    agrupado = agrupar_stock_par_por_mp(leer_bd_mp_sistema())

    filas: list[dict] = []
    for cod, info in agrupado.items():
        if not _mp_en_area_barra(info):
            continue
        stock = float(info["stock_total"])
        if stock > STOCK_CERO_TOL:
            continue

        par = float(info["par_level"])
        fila: dict = {
            "cod_mp_sistema": cod,
            "nombre_mp": info["nombre_mp"],
            "unidad_base": info["unidad_base"],
            "stock_total": round(stock, 4),
            "par_level": round(par, 4),
            "stock_por_bodega": info["por_bodega"],
            "pedido_propuesto": None,
            "motivo_sin_pedido": None,
            "nota": None,
        }

        if cod in en_orden:
            ln = en_orden[cod]
            fila["pedido_propuesto"] = _linea_cantidad_texto(ln)
            fila["nota"] = "Ya incluido en las órdenes de compra de este mensaje"
            fila["descripcion_proveedor"] = (ln.get("descripcion_proveedor") or "").strip()
        elif par <= STOCK_CERO_TOL:
            fila["motivo_sin_pedido"] = (
                "PAR en 0: no hay consumo calculado en recetas o no se ha corrido calcular_par_levels"
            )
        elif cod not in mps_bajo:
            fila["motivo_sin_pedido"] = (
                "Stock total en cero pero PAR no exige reposición (revisar par_level)"
            )
        elif cod not in items_por_mp:
            fila["motivo_sin_pedido"] = (
                "Sin fila en BD_ITEMS_PROV para proveedor Barra con destino BOD-002"
            )
        else:
            item = items_por_mp[cod][0]
            fila["descripcion_proveedor"] = (item.get("descripcion_proveedor") or "").strip()
            cp = item["cod_proveedor"]
            if cp not in proveedores:
                fila["motivo_sin_pedido"] = "Proveedor del ítem no está activo en BD_PROV"
            elif not sin_ventana and not proveedor_activo_hoy(
                proveedores[cp]["ventana_pedido"], hoy
            ):
                fila["motivo_sin_pedido"] = (
                    f"Proveedor sin ventana de pedido hoy "
                    f"({proveedores[cp].get('ventana_pedido') or 'sin ventana'})"
                )
            else:
                prop = _linea_pedido_propuesta(mps_bajo[cod], item, tipo=tipo_l)
                fila["pedido_propuesto"] = _linea_cantidad_texto(prop)
                fila["nota"] = "Bajo PAR pero no entró al bloque de órdenes (revisar ventana/catálogo)"

        filas.append(fila)

    # Anexo útil: PAR > 0 o pedido concreto (omitir PAR=0 sin reposición sugerida).
    filas = [
        f
        for f in filas
        if float(f.get("par_level") or 0) > STOCK_CERO_TOL or f.get("pedido_propuesto")
    ]

    filas.sort(key=lambda x: (-float(x["par_level"]), x["nombre_mp"]))
    return filas


def escribir_hoja_ordenes(ordenes: list[dict], *, tipo: str, fecha: date) -> None:
    from procesar_facturas_drive import _get_sheet

    sh = _get_sheet()
    titulo = "ORDENES_COMPRA"
    try:
        ws = sh.worksheet(titulo)
    except Exception:
        ws = sh.add_worksheet(title=titulo, rows=500, cols=14)

    headers = [
        "fecha",
        "tipo_area",
        "cod_proveedor",
        "razon_social",
        "cod_mp",
        "nombre_mp",
        "descripcion_proveedor",
        "stock_bodega",
        "par_level",
        "cant_base",
        "unidades_pedir",
        "unidad_compra",
        "cod_bodega",
    ]
    rows = [headers]
    for oc in ordenes:
        p = oc["proveedor"]
        for ln in oc["lineas"]:
            rows.append(
                [
                    fecha.isoformat(),
                    tipo,
                    p["cod_proveedor"],
                    p["razon_social"],
                    ln["cod_mp_sistema"],
                    ln["nombre_mp"],
                    ln.get("descripcion_proveedor", ""),
                    ln["stock_actual"],
                    ln["par_level"],
                    ln["cantidad_base"],
                    ln["unidades_a_pedir"],
                    ln.get("unidad_compra", ""),
                    ln.get("cod_bodega", ""),
                ]
            )
    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")
    print(f"  Hoja {titulo}: {len(rows) - 1} líneas escritas.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Órdenes de compra sugeridas por proveedor")
    ap.add_argument("--tipo", choices=("barra", "cocina", "todos"), default="barra")
    ap.add_argument("--sin-ventana", action="store_true", help="Incluir todos los proveedores aunque hoy no sea ventana_pedido")
    ap.add_argument("--json", metavar="PATH", help="Exportar JSON")
    ap.add_argument("--txt", metavar="PATH", help="Exportar texto consolidado")
    ap.add_argument("--produccion", action="store_true", help="Escribir hoja ORDENES_COMPRA en Sheets")
    args = ap.parse_args()

    hoy = date.today()
    ordenes = generar_ordenes(tipo=args.tipo, sin_ventana=args.sin_ventana, hoy=hoy)

    print(f"\nÓRDENES DE COMPRA — {args.tipo.upper()} — {hoy.strftime('%d/%m/%Y')}")
    print(f"Proveedores con ítems a pedir: {len(ordenes)}")
    total_lineas = sum(o["n_items"] for o in ordenes)
    print(f"Líneas totales: {total_lineas}\n")

    if not ordenes:
        print("No hay productos bajo PAR con catálogo de proveedor para este filtro.")
        return 0

    texto_completo = []
    for oc in ordenes:
        print(oc["texto_orden"])
        texto_completo.append(oc["texto_orden"])

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fecha": hoy.isoformat(),
            "tipo": args.tipo,
            "ordenes": [
                {
                    "cod_proveedor": o["cod_proveedor"],
                    "proveedor": o["proveedor"],
                    "n_items": o["n_items"],
                    "lineas": o["lineas"],
                    "mensaje_whatsapp": o["mensaje_whatsapp"],
                }
                for o in ordenes
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {path}")

    if args.txt:
        path = Path(args.txt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(texto_completo), encoding="utf-8")
        print(f"TXT: {path}")

    if args.produccion:
        escribir_hoja_ordenes(ordenes, tipo=args.tipo, fecha=hoy)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
