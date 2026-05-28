# whatsapp_webhook.py v4 — sin dependencia de consultas_chat_extendidas ni agente_chat
import asyncio
import os
import json
import math
import time
import unicodedata
from collections import OrderedDict, defaultdict, deque
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from supabase import create_client
import gspread
from google.oauth2.service_account import Credentials
import anthropic
import pytz
import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from sesiones_factura import hay_sesion_activa
from ventas_smartmenu import estado_documento_excluye_neto_operativo
from factura_confirmacion_parse import parse_confirmacion_factura
from whatsapp_factura_handler import handle_confirmacion, handle_mensaje_media

from sesiones_conteo import get_sesion_activa, aprobar_items, rechazar_items, cerrar_sesion
from conteo_fisico import contabilizar_envio, ConteoOperacionError
from conteo_operaciones import (
    iniciar_conteo_wa,
    resumen_ciclos_abiertos,
    semana_iso_actual,
)
from kardex_inventario import get_kardex, formatear_kardex_wa, generar_xlsx

load_dotenv()
TZ = pytz.timezone("America/Guayaquil")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
app = FastAPI()

# Dedup webhook Meta (msg_id) con TTL — evita reprocesar y crece sin límite
_mensajes_procesados: OrderedDict[str, float] = OrderedDict()
MSG_DEDUP_TTL_SEC = 86400
MSG_DEDUP_MAX = 50_000

# Cola por número: un mensaje activo + pendientes (Lock + drain)
_wa_locks: dict[str, asyncio.Lock] = {}
_wa_pending: dict[str, deque] = defaultdict(deque)
_wa_cola_avisado: set[str] = set()
MSG_COLA_ESPERA = (
    "Un momento, estoy procesando tu mensaje anterior. Te respondo en seguida."
)

# Cache BD_MP_SISTEMA (reduce lecturas Sheets en ráfagas / múltiples tools)
_bd_mp_cache: list[dict] | None = None
_bd_mp_cache_at: float = 0.0
BD_MP_CACHE_TTL_SEC = 60

_sheet_workbook = None

from conteo_routes import router as conteo_router
app.include_router(conteo_router, prefix="/api/conteo")

# ── Helpers ──────────────────────────────────────────────────
def _to_float(v, default=0.0):
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, default)


def _to_int(v, default=0):
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _paging(args: dict | None, *, default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    """(limit, offset) estándar para listados largos."""
    args = args or {}
    limit = _to_int(args.get("limit", default_limit), default_limit)
    offset = _to_int(args.get("offset", 0), 0)
    limit = _clamp(limit, 1, max_limit)
    offset = max(0, offset)
    return limit, offset


def _normaliza_busqueda_mp(s: str) -> str:
    """Minúsculas y sin tildes para comparar nombres de MP."""
    t = (s or "").lower().strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn"
    )


def _variantes_raiz_plural(ff: str) -> set[str]:
    """Camarones/camaron, langostinos/langostino — heurística simple."""
    out = {ff}
    if len(ff) >= 4 and ff.endswith("es"):
        out.add(ff[:-2])
    if len(ff) >= 4 and ff.endswith("s") and not ff.endswith("es"):
        out.add(ff[:-1])
    return {x for x in out if len(x) >= 3}


def _coincide_nombre_mp(nombre_fila: str, filtro: str) -> bool:
    """True si el nombre en hoja coincide con lo que buscó el usuario (parcial, sin tildes)."""
    nf = _normaliza_busqueda_mp(nombre_fila)
    ff = _normaliza_busqueda_mp(filtro)
    if not ff:
        return True
    if not nf:
        return False
    for v in _variantes_raiz_plural(ff):
        if v and v in nf:
            return True
    for tok in ff.split():
        if len(tok) >= 3:
            for v in _variantes_raiz_plural(tok):
                if v and v in nf:
                    return True
    return False


# ── Conexiones ───────────────────────────────────────────────
_supabase_client = None


def conectar_supabase():
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
        )
    return _supabase_client


def conectar_sheets():
    global _sheet_workbook
    if _sheet_workbook is None:
        creds = Credentials.from_service_account_file(
            os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
        )
        _sheet_workbook = gspread.Client(auth=creds).open_by_key(
            os.getenv("SPREADSHEET_ID")
        )
    return _sheet_workbook


def _purge_mensajes_procesados() -> None:
    now = time.monotonic()
    while _mensajes_procesados:
        _mid, ts = next(iter(_mensajes_procesados.items()))
        if len(_mensajes_procesados) > MSG_DEDUP_MAX or (now - ts) > MSG_DEDUP_TTL_SEC:
            _mensajes_procesados.popitem(last=False)
        else:
            break


def mensaje_ya_procesado(msg_id: str) -> bool:
    """True si msg_id ya se encoló/procesó (dedup Meta)."""
    if not msg_id:
        return False
    _purge_mensajes_procesados()
    if msg_id in _mensajes_procesados:
        return True
    _mensajes_procesados[msg_id] = time.monotonic()
    return False


def invalidar_cache_bd_mp() -> None:
    global _bd_mp_cache, _bd_mp_cache_at
    _bd_mp_cache = None
    _bd_mp_cache_at = 0.0


def leer_bd_mp_sistema(*, force_refresh: bool = False) -> list[dict]:
    """Filas de BD_MP_SISTEMA; cache en memoria 60s salvo force_refresh."""
    global _bd_mp_cache, _bd_mp_cache_at
    now = time.monotonic()
    if (
        not force_refresh
        and _bd_mp_cache is not None
        and (now - _bd_mp_cache_at) < BD_MP_CACHE_TTL_SEC
    ):
        return list(_bd_mp_cache)

    _, rows = leer_hoja_con_headers(
        "BD_MP_SISTEMA", "cod_mp_sistema", skip_after_header=1
    )
    out = [r for r in rows if (r.get("cod_mp_sistema") or "").strip()]
    _bd_mp_cache = out
    _bd_mp_cache_at = now
    return list(out)


def leer_hoja_con_headers(sheet_name: str, header_key: str, *, skip_after_header: int = 1) -> tuple[list[str], list[dict]]:
    """
    Lee una hoja y detecta la fila de headers buscando header_key.
    Retorna (headers, rows_as_dict).
    """
    sheet = conectar_sheets()
    ws = sheet.worksheet(sheet_name)
    values = ws.get_all_values()
    header_row = None
    for i, row in enumerate(values):
        if any((c or "").strip() == header_key for c in row):
            header_row = i
            break
    if header_row is None:
        return [], []
    headers = [(c or "").strip() for c in values[header_row]]
    data = values[header_row + skip_after_header :]
    out = []
    for row in data:
        if not any((c or "").strip() for c in row):
            continue
        if row and str(row[0]).strip().startswith("["):
            continue
        d = {headers[j]: (row[j] if j < len(row) else "").strip() for j in range(len(headers)) if headers[j]}
        out.append(d)
    return headers, out


# ── Búsqueda de MP (migrada desde consultas_chat_extendidas) ─
def _buscar_mp_por_nombre_o_codigo(texto: str) -> list[dict]:
    """
    Busca MPs en BD_MP_SISTEMA por nombre (substring) o código exacto.
    Usa leer_bd_mp_sistema() con cache — no re-autentica con Sheets.
    """
    texto_u = (texto or "").strip().lower()
    if len(texto_u) < 2:
        return []
    rows = leer_bd_mp_sistema()
    hits: list[dict] = []
    for r in rows:
        cod = (r.get("cod_mp_sistema") or "").strip()
        nom = (r.get("nombre_mp") or "").strip()
        if not cod:
            continue
        if texto_u == cod.lower():
            hits.insert(0, r)
            continue
        if texto_u in nom.lower():
            hits.append(r)
    return hits


# ── Consumo teórico (migrado desde consultas_chat_extendidas) ─
def _hist_ventas_para_consumo(fecha_ini: str, fecha_fin: str) -> list[dict]:
    """
    Líneas de venta con campos para cruzar con recetas.
    Equivalente a consultas_chat_extendidas._hist_ventas_en_rango_para_consumo.
    """
    sb = conectar_supabase()
    sel = (
        "nombre_producto,cantidad_vendida,fecha,cod_receta,cod_smart_menu,"
        "cod_producto,variedad_smart_menu,estado_match"
    )
    try:
        sb.table("hist_ventas").select("estado_documento").limit(1).execute()
        sel += ",estado_documento"
    except Exception:
        pass

    out: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("hist_ventas")
            .select(sel)
            .gte("fecha", fecha_ini)
            .lte("fecha", fecha_fin)
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000

    return [
        r for r in out
        if not estado_documento_excluye_neto_operativo(r.get("estado_documento"))
    ]


def calcular_consumo_teorico_mp(fecha_ini: str, fecha_fin: str, cod_mp_sistema: str) -> dict:
    """
    Consumo teórico de un cod_mp_sistema cruzando hist_ventas (PROCESADO) con BD_RECETAS_DETALLE.
    Migrada desde consultas_chat_extendidas.calcular_consumo_teorico_mp.
    """
    from descargo_inventario import (
        _resolver_cod_receta,
        calcular_consumo,
        get_ingredientes,
    )
    from recetas_detalle import filtrar_solo_mp

    cod_target = (cod_mp_sistema or "").strip()
    if not cod_target:
        return {"error": "cod_mp_sistema vacío"}

    rows = _hist_ventas_para_consumo(fecha_ini, fecha_fin)
    if not rows:
        return {
            "error": "sin_lineas",
            "mensaje": f"No hay líneas en hist_ventas entre {fecha_ini} y {fecha_fin}.",
        }

    por_plato: dict[str, dict[str, float]] = {}
    lineas_procesadas = 0
    lineas_omitidas_match = 0
    lineas_sin_receta = 0
    lineas_sin_ingrediente_en_receta = 0

    for r in rows:
        em = (r.get("estado_match") or "").strip().upper()
        if em and em != "PROCESADO":
            lineas_omitidas_match += 1
            continue

        venta = {
            "cod_receta": r.get("cod_receta"),
            "cod_smart_menu": r.get("cod_smart_menu"),
            "cod_producto": r.get("cod_producto"),
            "variedad_smart_menu": r.get("variedad_smart_menu"),
        }
        cod_receta = _resolver_cod_receta(venta)
        variedad = r.get("variedad_smart_menu")
        cant_v = _to_float(r.get("cantidad_vendida"))
        nombre_plato = (r.get("nombre_producto") or "").strip() or "(sin nombre)"

        if not cod_receta:
            lineas_sin_receta += 1
            continue

        ingredientes = filtrar_solo_mp(get_ingredientes(cod_receta, variedad))
        if not ingredientes:
            lineas_sin_receta += 1
            continue

        subtotal_linea = 0.0
        for ing in ingredientes:
            c_mp = (ing.get("cod_mp_sistema") or "").strip()
            if c_mp != cod_target:
                continue
            subtotal_linea += calcular_consumo(ing, cant_v)

        if subtotal_linea <= 0:
            lineas_sin_ingrediente_en_receta += 1
            continue

        lineas_procesadas += 1
        acc = por_plato.setdefault(
            nombre_plato,
            {"unidades_vendidas": 0.0, "consumo_mp": 0.0},
        )
        acc["unidades_vendidas"] += cant_v
        acc["consumo_mp"] += subtotal_linea

    total = sum(x["consumo_mp"] for x in por_plato.values())
    suma_unidades_en_desglose = sum(x["unidades_vendidas"] for x in por_plato.values())
    desglose = sorted(por_plato.items(), key=lambda kv: kv[1]["consumo_mp"], reverse=True)

    return {
        "fecha_ini": fecha_ini,
        "fecha_fin": fecha_fin,
        "cod_mp_sistema": cod_target,
        "total_consumo_teorico": round(total, 4),
        "num_platos_en_desglose": len(por_plato),
        "suma_unidades_vendidas_en_desglose": round(suma_unidades_en_desglose, 4),
        "por_plato": [
            {
                "nombre_producto": nombre,
                "unidades_vendidas": round(d["unidades_vendidas"], 4),
                "consumo_mp": round(d["consumo_mp"], 4),
            }
            for nombre, d in desglose
        ],
        "lineas_hist_usadas": lineas_procesadas,
        "lineas_omitidas_match": lineas_omitidas_match,
        "lineas_sin_receta_o_vacia": lineas_sin_receta,
        "lineas_plato_sin_ese_mp": lineas_sin_ingrediente_en_receta,
        "nota": (
            "Consumo teórico según BD_RECETAS_DETALLE y ventas con matching PROCESADO; "
            "equivale a lo que el descargo de inventario descontaría por esas ventas. "
            "Cada nombre_producto en por_plato es el texto exacto de hist_ventas para esa línea "
            "agrupada: si aparece un plato que no reconoces (ej. postre), revisa ventas y la receta "
            "que enlaza ese producto con este cod_mp_sistema."
        ),
    }


# ── Paginación Supabase ──────────────────────────────────────
def supabase_query_all(sb, table, select, filters=None):
    """Lee todas las filas paginando de 1000 en 1000."""
    rows = []
    offset = 0
    while True:
        q = sb.table(table).select(select)
        if filters:
            for method, *args in filters:
                q = getattr(q, method)(*args)
        chunk = q.range(offset, offset + 999).execute().data
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


# ── FACTURAS / PENDIENTES ────────────────────────────────────
def tool_facturas_parciales(args=None):
    """Facturas con estado PARCIAL en Supabase (facturas_procesadas)."""
    args = args or {}
    limit, offset = _paging(args, default_limit=30, max_limit=200)
    sb = conectar_supabase()
    try:
        q = (
            sb.table("facturas_procesadas")
            .select("num_factura,ruc_proveedor,fecha_factura,items_procesados,items_sin_match,estado,fecha_proceso")
            .eq("estado", "PARCIAL")
            .order("fecha_factura", desc=True)
            .range(offset, offset + limit - 1)
        )
        data = q.execute().data or []
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    return {"ok": True, "total_en_pagina": len(data), "items": data, "paging": {"limit": limit, "offset": offset}}


def tool_items_pendientes_factura(args):
    """
    Lista filas PENDIENTE en BD_ITEMS_PENDIENTES por num_factura o por ruc_proveedor.
    args: {num_factura?: str, ruc_proveedor?: str, limit?: int, offset?: int}
    """
    args = args or {}
    num = str(args.get("num_factura", "") or "").strip()
    ruc = str(args.get("ruc_proveedor", "") or "").strip()
    limit, offset = _paging(args, default_limit=50, max_limit=200)

    if not num and not ruc:
        return {"ok": False, "error": "Debes enviar num_factura o ruc_proveedor", "items": []}

    headers, rows = leer_hoja_con_headers("BD_ITEMS_PENDIENTES", "clave_unica", skip_after_header=1)
    if not headers:
        return {"ok": False, "error": "No pude leer BD_ITEMS_PENDIENTES", "items": []}

    out = []
    for r in rows:
        estado = (r.get("estado") or "").strip().upper()
        if estado and estado != "PENDIENTE":
            continue
        if num and (r.get("num_factura") or "").strip() != num:
            continue
        if ruc and (r.get("ruc_proveedor") or "").strip() != ruc:
            continue
        out.append(
            {
                "num_factura": (r.get("num_factura") or "").strip(),
                "ruc_proveedor": (r.get("ruc_proveedor") or "").strip(),
                "cod_item_xml": (r.get("cod_item_xml") or "").strip(),
                "descripcion_xml": (r.get("descripcion_xml") or "").strip(),
                "cantidad": (r.get("cantidad") or "").strip(),
                "precio_unitario_xml": (r.get("precio_unitario_xml") or "").strip(),
                "costo_efectivo": (r.get("costo_efectivo") or "").strip(),
                "cod_mp_asignado": (r.get("cod_mp_asignado") or "").strip(),
                "nombre_mp_desde_bd": (r.get("nombre_mp_desde_bd") or "").strip(),
                "link_xml": (r.get("link_xml") or "").strip(),
            }
        )

    total = len(out)
    out = out[offset : offset + limit]
    return {"ok": True, "total": total, "items": out, "paging": {"limit": limit, "offset": offset}}


def _es_mov_compra_factura(r: dict) -> bool:
    """Misma lógica relajada que reporte_semanal: compras por factura."""
    t = (r.get("tipo_mov") or "").strip().upper()
    o = (r.get("origen_documento") or "").strip().upper()
    return t in ("ENTRADA", "ENTRADA_COMPRA") and (o == "FACTURA" or o == "")


def tool_compras_facturas_rango(args):
    """
    Compras desde facturas ya registradas en inventario: mov_inventario
    (ENTRADA / ENTRADA_COMPRA por factura) entre dos fechas inclusive.
    Devuelve totales, top facturas, top productos (MP) y agregado por proveedor (RUC).
    """
    args = args or {}
    desde = str(args.get("fecha_desde") or "").strip()
    hasta = str(args.get("fecha_hasta") or "").strip()
    top_facturas = min(max(int(args.get("top_facturas", 40) or 40), 5), 100)
    top_productos = min(max(int(args.get("top_productos", 35) or 35), 5), 100)

    if not desde or not hasta:
        return {
            "ok": False,
            "error": "Obligatorio: fecha_desde y fecha_hasta como YYYY-MM-DD (ej. 2026-05-01).",
        }

    try:
        d0 = datetime.strptime(desde, "%Y-%m-%d").date()
        d1 = datetime.strptime(hasta, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "error": "Fechas invalidas. Usa formato YYYY-MM-DD."}

    if d0 > d1:
        return {"ok": False, "error": "fecha_desde no puede ser posterior a fecha_hasta."}

    if (d1 - d0).days > 400:
        return {"ok": False, "error": "Rango maximo 400 dias. Acorta el periodo."}

    sb = conectar_supabase()
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            sb.table("mov_inventario")
            .select(
                "fecha,tipo_mov,origen_documento,num_documento,cod_mp_sistema,nombre_mp,cantidad_mov,costo_total"
            )
            .gte("fecha", f"{desde}T00:00:00")
            .lte("fecha", f"{hasta}T23:59:59")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(r for r in chunk if _es_mov_compra_factura(r))
        if len(chunk) < 1000:
            break
        offset += 1000

    if not rows:
        return {
            "ok": True,
            "resumen": {
                "fecha_desde": desde,
                "fecha_hasta": hasta,
                "total_compras_usd": 0.0,
                "n_lineas_movimiento": 0,
                "n_facturas_distintas": 0,
                "n_productos_mp_distintos": 0,
                "nota": "Sin lineas ENTRADA por factura en mov_inventario en ese rango.",
            },
            "por_proveedor": [],
            "top_facturas": [],
            "top_productos": [],
        }

    by_doc: dict[str, dict] = {}
    by_mp: dict[str, dict] = defaultdict(
        lambda: {"nombre_mp": "", "cantidad": 0.0, "costo_total": 0.0}
    )
    total = 0.0

    for r in rows:
        ct = _to_float(r.get("costo_total"), 0.0)
        total += ct
        num = (r.get("num_documento") or "").strip() or "(sin_num_documento)"
        fe = (str(r.get("fecha") or "")[:10]) or desde

        if num not in by_doc:
            by_doc[num] = {
                "num_factura": num,
                "fecha_primera": fe,
                "total_usd": 0.0,
                "n_lineas": 0,
            }
        else:
            if fe < by_doc[num]["fecha_primera"]:
                by_doc[num]["fecha_primera"] = fe
        by_doc[num]["total_usd"] += ct
        by_doc[num]["n_lineas"] += 1

        cod = (r.get("cod_mp_sistema") or "").strip()
        if cod:
            mp = by_mp[cod]
            nom = (r.get("nombre_mp") or "").strip()
            if nom:
                mp["nombre_mp"] = nom
            mp["cantidad"] += _to_float(r.get("cantidad_mov"), 0.0)
            mp["costo_total"] += ct

    nums = sorted({n for n in by_doc if n != "(sin_num_documento)"})
    ruc_map: dict[str, str] = {}
    for i in range(0, len(nums), 80):
        part = nums[i : i + 80]
        try:
            res = (
                sb.table("facturas_procesadas")
                .select("num_factura,ruc_proveedor")
                .in_("num_factura", part)
                .execute()
            )
            for x in res.data or []:
                n = (x.get("num_factura") or "").strip()
                if n:
                    ruc_map[n] = (x.get("ruc_proveedor") or "").strip()
        except Exception:
            pass

    for d in by_doc.values():
        n = d["num_factura"]
        d["ruc_proveedor"] = ruc_map.get(n, "") if n != "(sin_num_documento)" else ""

    por_prov: dict[str, float] = defaultdict(float)
    for d in by_doc.values():
        ruc = (d.get("ruc_proveedor") or "").strip()
        clave = ruc if ruc else f"sin_ruc:{d['num_factura']}"
        por_prov[clave] += d["total_usd"]

    top_f = sorted(by_doc.values(), key=lambda x: x["total_usd"], reverse=True)[:top_facturas]
    for x in top_f:
        x["total_usd"] = round(x["total_usd"], 2)

    prod_list = []
    for cod, v in by_mp.items():
        prod_list.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": v["nombre_mp"] or cod,
                "cantidad_mov_total": round(v["cantidad"], 4),
                "costo_total_usd": round(v["costo_total"], 2),
            }
        )
    prod_list.sort(key=lambda x: x["costo_total_usd"], reverse=True)
    prod_list = prod_list[:top_productos]

    prov_out = [
        {"proveedor_clave": k, "total_usd": round(v, 2)}
        for k, v in sorted(por_prov.items(), key=lambda kv: -kv[1])[:30]
    ]

    return {
        "ok": True,
        "resumen": {
            "fecha_desde": desde,
            "fecha_hasta": hasta,
            "total_compras_usd": round(total, 2),
            "n_lineas_movimiento": len(rows),
            "n_facturas_distintas": len(by_doc),
            "n_productos_mp_distintos": len(by_mp),
            "nota": "Montos = suma costo_total por linea en mov_inventario. RUC desde facturas_procesadas por num_factura; si falta, aparece sin_ruc:numero.",
        },
        "por_proveedor": prov_out,
        "top_facturas": top_f,
        "top_productos": prod_list,
    }


def tool_mp_incompletas(args=None):
    """
    MPs con datos incompletos en BD_MP_SISTEMA.
    args:
      - tipo: 'sin_costo' | 'sin_par' | 'sin_bodega'
      - limit/offset
    """
    args = args or {}
    tipo = str(args.get("tipo", "sin_costo") or "sin_costo").strip().lower()
    limit, offset = _paging(args, default_limit=50, max_limit=200)

    rows = leer_bd_mp_sistema()
    out = []
    for r in rows:
        cod = str(r.get("cod_mp_sistema", "")).strip()
        if not cod:
            continue
        nombre = str(r.get("nombre_mp", cod)).strip()
        unidad = str(r.get("unidad_base", "")).strip()
        bod = str(r.get("cod_bodega", "")).strip()
        stock = _to_float(r.get("stock_actual", 0), 0.0)
        par = _to_float(r.get("par_level", 0), 0.0)
        costo = _to_float(r.get("costo_unitario_ref", 0), 0.0)

        match = False
        if tipo == "sin_par":
            match = par <= 0
        elif tipo == "sin_bodega":
            match = bod == ""
        else:  # sin_costo
            match = costo <= 0
        if not match:
            continue

        out.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": nombre,
                "stock_actual": round(stock, 4),
                "unidad": unidad,
                "par_level": round(par, 4),
                "costo_unitario_ref": round(costo, 6),
                "cod_bodega": bod,
            }
        )

    total = len(out)
    # Orden útil: sin_costo por stock_abs desc, sin_par por stock_abs desc, sin_bodega por nombre
    if tipo in {"sin_costo", "sin_par"}:
        out.sort(key=lambda x: abs(x["stock_actual"]), reverse=True)
    else:
        out.sort(key=lambda x: x["nombre_mp"])

    out = out[offset : offset + limit]
    return {"tipo": tipo, "total": total, "items": out, "paging": {"limit": limit, "offset": offset}}


def tool_resumen_operativo_hoy(args=None):
    """
    Resumen compacto para WhatsApp:
      - ventas hoy (total oficial + tickets)
      - bajo par (conteo + top N)
      - negativos (conteo + top N)
      - facturas parciales (conteo página)
    args:
      - top: int (default 10) para listados internos
    """
    args = args or {}
    top = _to_int(args.get("top", 10), 10)
    top = _clamp(top, 3, 30)

    ventas = tool_ventas_hoy()
    bajo_par = tool_stock_critico({"top": top})
    neg = tool_stocks_negativos({"top": top})
    parc = tool_facturas_parciales({"limit": 20, "offset": 0})

    return {
        "fecha": ventas.get("fecha"),
        "ventas": {"total_ventas": ventas.get("total_ventas"), "tickets": ventas.get("tickets"), "fuente": ventas.get("fuente", None) or ventas.get("nota", None)},
        "bajo_par": {"total": bajo_par.get("total_bajo_par"), "items": bajo_par.get("items", [])},
        "negativos": {"total": neg.get("total_negativos"), "items": neg.get("items", [])},
        "facturas_parciales": {"ok": parc.get("ok"), "total_en_pagina": parc.get("total_en_pagina"), "items": parc.get("items", [])},
    }


def _hist_ventas_sin_anulados(rows):
    """Excluye líneas de documentos anulados (hist_ventas.estado_documento = ANULADO)."""
    return [
        r
        for r in rows
        if not estado_documento_excluye_neto_operativo(r.get("estado_documento"))
    ]

# ── Total oficial via Smart Menu ─────────────────────────────
def total_smartmenu_dia(fecha_str):
    """Totales Smart Menu del día: dict con total (neto), total_bruto, total_descuentos, docs."""
    try:
        import importlib.util, sys
        # Importar dinámicamente desde el mismo directorio
        spec = importlib.util.spec_from_file_location(
            "ventas_smartmenu_total",
            os.path.join(os.path.dirname(__file__), "ventas_smartmenu_total.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.calcular_total_smartmenu(fecha_str, sin_iva=True)
    except Exception:
        return None  # fallback: indica que Smart Menu no disponible


def _total_desde_hist_ventas_docs(fecha_desde: str, fecha_hasta: str) -> tuple[float, int]:
    """Lee total y número de documentos desde hist_ventas cuando Smart Menu no está disponible."""
    sb = conectar_supabase()
    rows = supabase_query_all(
        sb, "hist_ventas",
        "num_documento,total,subtotal,descuento_valor,estado_documento",
        [("gte", "fecha", fecha_desde), ("lte", "fecha", fecha_hasta)]
    )
    rows = _hist_ventas_sin_anulados(rows)
    docs = set(r.get("num_documento") for r in rows if r.get("num_documento"))
    # Neto por línea: subtotal − descuento_valor (alineado a ventas netas Smart Menu)
    total = sum(
        _to_float(r.get("subtotal"), 0.0) - _to_float(r.get("descuento_valor"), 0.0)
        for r in rows
    )
    return round(total, 2), len(docs)


# ── TOOL 1 — ventas hoy ─────────────────────────────────────
def tool_ventas_hoy():
    hoy = date.today().isoformat()
    sm = total_smartmenu_dia(hoy)

    # Top platos desde hist_ventas (orden por monto total del día)
    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total,estado_documento",
        [("eq", "fecha", hoy)])
    rows = _hist_ventas_sin_anulados(rows)
    conteo = defaultdict(lambda: {"cantidad": 0, "total": 0})
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(sin nombre)"
        conteo[nombre]["cantidad"] += _to_float(r.get("cantidad_vendida"), 0)
        conteo[nombre]["total"] += _to_float(r.get("total"), 0.0)
    top5 = sorted(conteo.items(), key=lambda x: x[1]["total"], reverse=True)[:5]

    resultado = {
        "fecha": hoy,
        "top_platos": [
            {"plato": n, "cantidad": int(d["cantidad"]), "total_usd": round(d["total"], 2)}
            for n, d in top5
        ],
    }
    if sm is not None:
        resultado["total_ventas"] = round(sm.get("total", 0), 2)
        resultado["ventas_brutas"] = round(sm.get("total_bruto", sm.get("total", 0)), 2)
        resultado["descuentos"] = round(sm.get("total_descuentos", 0), 2)
        resultado["tickets"] = sm.get("docs", 0)
        resultado["fuente"] = "Smart Menu"
    else:
        total_hv, docs_hv = _total_desde_hist_ventas_docs(hoy, hoy)
        resultado["total_ventas"] = total_hv
        resultado["tickets"] = docs_hv
        resultado["fuente"] = "hist_ventas"
    return resultado

# ── TOOL 2 — ventas semana ──────────────────────────────────
def tool_ventas_semana():
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())

    # Total oficial: suma diaria via Smart Menu
    total_oficial = 0
    total_docs = 0
    sm_disponible = True
    d = lunes
    while d <= hoy:
        sm = total_smartmenu_dia(d.isoformat())
        if sm is None:
            sm_disponible = False
            break
        total_oficial += sm.get("total", 0)
        total_docs += (sm.get("docs") or 0)
        d += timedelta(days=1)

    # Fallback y ranking desde hist_ventas
    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total,fecha,estado_documento",
        [("gte", "fecha", lunes.isoformat()), ("lte", "fecha", hoy.isoformat())])
    rows = _hist_ventas_sin_anulados(rows)

    dias = len(set(r["fecha"] for r in rows))
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]

    if not sm_disponible:
        total_oficial, total_docs = _total_desde_hist_ventas_docs(lunes.isoformat(), hoy.isoformat())

    return {
        "periodo": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "total_ventas": round(total_oficial, 2),
        "dias_activos": dias,
        "tickets": total_docs,
        "promedio_diario": round(total_oficial/dias, 2) if dias else 0,
        "top_platos": [{"plato": n, "cantidad": int(c)} for n,c in top5],
        "fuente": "Smart Menu" if sm_disponible else "hist_ventas (aproximado)"
    }

# ── TOOL 3 — stock bajo par level ────────────────────────────
# Importante: NO truncar a top10 a menos que el usuario lo pida.
def tool_stock_critico(args=None):
    """
    Lista MPs bajo par level (par_level > 0 y stock_actual < par_level).
    args opcional: {"top": int} para truncar.
    """
    args = args or {}
    top = args.get("top", 0)
    try:
        top = int(top) if top else 0
    except Exception:
        top = 0

    rows = leer_bd_mp_sistema()
    criticos = []
    for r in rows:
        try:
            stock = _to_float(r.get("stock_actual", "0") or "0")
            par = _to_float(r.get("par_level", "0") or "0")
        except: continue
        if par > 0 and stock < par:
            cod = str(r.get("cod_mp_sistema","")).strip()
            criticos.append({
                "cod_mp_sistema": cod,
                "nombre": str(r.get("nombre_mp","")).strip(),
                "stock_actual": round(stock,1),
                "par_level": round(par,1),
                "unidad": str(r.get("unidad_base","")).strip(),
                "deficit_pct": round((1-stock/par)*100,1)
            })
    criticos.sort(key=lambda x: x["deficit_pct"], reverse=True)
    total = len(criticos)
    if top and top > 0:
        criticos = criticos[:top]
    return {"total_bajo_par": total, "items": criticos}


# ── TOOL 3B — stocks negativos (NO inventar) ─────────────────
def tool_stocks_negativos(args=None):
    """
    Lista MPs con stock_actual < 0 desde BD_MP_SISTEMA.
    args opcional: {"top": int} para truncar.
    """
    args = args or {}
    top = args.get("top", 0)
    try:
        top = int(top) if top else 0
    except Exception:
        top = 0

    rows = leer_bd_mp_sistema()
    negativos = []
    for r in rows:
        cod = str(r.get("cod_mp_sistema", "")).strip()
        nombre = str(r.get("nombre_mp", cod)).strip()
        unidad = str(r.get("unidad_base", "")).strip()
        try:
            stock = _to_float(r.get("stock_actual", "0") or "0")
        except Exception:
            continue
        if stock < 0:
            negativos.append(
                {
                    "cod_mp_sistema": cod,
                    "nombre_mp": nombre,
                    "stock_actual": round(stock, 4),
                    "unidad": unidad,
                }
            )
    negativos.sort(key=lambda x: x["stock_actual"])  # más negativo primero
    total = len(negativos)
    if top and top > 0:
        negativos = negativos[:top]
    return {"total_negativos": total, "items": negativos}

# ── TOOL 3C — inventario valorizado (USD) ────────────────────
def tool_inventario_valorizado(args=None):
    """
    Valorización usando BD_MP_SISTEMA: stock_actual * costo_unitario_ref.
    args opcional:
      - nombre_mp o buscar: texto para filtrar por nombre_mp (parcial, sin importar tildes/plural).
      - cod_bodega: filtra por bodega
      - top: int (top por valor absoluto)
      - incluir_cero: bool (default False)
      - incluir_negativos: bool (default False). Si False, negativos se tratan como 0 para valorización.
      - limit/offset: paginación del listado (si no usas top)

    Si nombre_mp está definido: devuelve solo MPs que coincidan e incluye filas sin costo o con stock 0
    (para que el usuario vea «existe pero no valoriza»).
    """
    args = args or {}
    cod_bod = str(args.get("cod_bodega", "") or "").strip()
    buscar = str(args.get("nombre_mp") or args.get("buscar") or "").strip()
    incluir_cero = str(args.get("incluir_cero", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}
    incluir_negativos = str(args.get("incluir_negativos", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}
    top = _to_int(args.get("top", 0), 0)
    limit, offset = _paging(args, default_limit=50, max_limit=200)

    rows = leer_bd_mp_sistema()
    items = []
    total_usd = 0.0
    sin_costo = 0
    for r in rows:
        bod = str(r.get("cod_bodega", "")).strip()
        if cod_bod and bod != cod_bod:
            continue
        if buscar and not _coincide_nombre_mp(str(r.get("nombre_mp", "")), buscar):
            continue

        stock = _to_float(r.get("stock_actual", 0), 0.0)
        costo = _to_float(r.get("costo_unitario_ref", 0), 0.0)

        if costo <= 0:
            sin_costo += 1
            if buscar:
                items.append(
                    {
                        "cod_mp_sistema": str(r.get("cod_mp_sistema", "")).strip(),
                        "nombre_mp": str(r.get("nombre_mp", "")).strip(),
                        "cod_bodega": bod,
                        "stock_actual": round(stock, 4),
                        "stock_valorizado": round(stock, 4),
                        "unidad": str(r.get("unidad_base", "")).strip(),
                        "costo_unitario_ref": 0.0,
                        "valor_usd": 0.0,
                        "sin_costo_unitario_ref": True,
                        "nota": "Sin costo_unitario_ref en BD_MP_SISTEMA: no se puede valorizar en USD.",
                    }
                )
            continue

        if (not incluir_cero) and abs(stock) < 1e-9 and not buscar:
            continue

        stock_val = stock if incluir_negativos else max(stock, 0.0)
        val = stock_val * costo
        total_usd += val
        row_item = {
            "cod_mp_sistema": str(r.get("cod_mp_sistema", "")).strip(),
            "nombre_mp": str(r.get("nombre_mp", "")).strip(),
            "cod_bodega": bod,
            "stock_actual": round(stock, 4),
            "stock_valorizado": round(stock_val, 4),
            "unidad": str(r.get("unidad_base", "")).strip(),
            "costo_unitario_ref": round(costo, 6),
            "valor_usd": round(val, 2),
        }
        if buscar and abs(stock) < 1e-9:
            row_item["stock_cero"] = True
        items.append(row_item)

    items.sort(key=lambda x: abs(x["valor_usd"]), reverse=True)
    total_items = len(items)
    if top and top > 0:
        items = items[: _clamp(top, 1, 200)]
    else:
        items = items[offset : offset + limit]

    out = {
        "filtro_nombre_mp": buscar or None,
        "filtro_bodega": cod_bod or None,
        "incluye_negativos": incluir_negativos,
        "total_items_con_costo": total_items,
        "mps_sin_costo_ref": sin_costo,
        "total_valor_usd": round(total_usd, 2),
        "items": items,
        "paging": None if top else {"limit": limit, "offset": offset},
    }
    if buscar and not items:
        out["mensaje"] = (
            "Ninguna fila en BD_MP_SISTEMA coincide con ese texto en nombre_mp "
            "(prueba sin tilde, singular, o parte del nombre como está en la hoja)."
        )
    return out


# ── TOOL 3D — inventario por bodega (resumen) ────────────────
def tool_inventario_por_bodega(args=None):
    """
    Resume stock y valor por bodega desde BD_MP_SISTEMA.
    args opcional:
      - incluir_sin_costo: bool (default False)
    """
    args = args or {}
    incluir_sin_costo = str(args.get("incluir_sin_costo", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}
    incluir_negativos = str(args.get("incluir_negativos", "false")).strip().lower() in {"1", "true", "si", "sí", "yes", "y"}

    rows = leer_bd_mp_sistema()
    bodegas: dict[str, dict] = {}
    for r in rows:
        bod = str(r.get("cod_bodega", "")).strip() or "SIN_BODEGA"
        stock = _to_float(r.get("stock_actual", 0), 0.0)
        costo = _to_float(r.get("costo_unitario_ref", 0), 0.0)
        if costo <= 0 and not incluir_sin_costo:
            continue
        stock_val = stock if incluir_negativos else max(stock, 0.0)
        val = stock_val * costo if costo > 0 else 0.0
        b = bodegas.setdefault(
            bod,
            {"cod_bodega": bod, "mps": 0, "valor_usd": 0.0, "stock_total": 0.0, "mps_sin_costo": 0},
        )
        b["mps"] += 1
        b["stock_total"] += stock
        if costo > 0:
            b["valor_usd"] += val
        else:
            b["mps_sin_costo"] += 1

    out = list(bodegas.values())
    for b in out:
        b["valor_usd"] = round(b["valor_usd"], 2)
        b["stock_total"] = round(b["stock_total"], 4)
    out.sort(key=lambda x: x["valor_usd"], reverse=True)
    return {"bodegas": out, "total_bodegas": len(out)}

# ── TOOL 4 — pedidos hoy ────────────────────────────────────
def tool_pedidos_hoy():
    sheet = conectar_sheets()
    hoy = date.today()
    DIA_MAP = {"LUN":0,"MAR":1,"MIE":2,"JUE":3,"VIE":4,"SAB":5,"DOM":6}
    PILOTO = {"ITALDELI","GALABDISTRI","MARAMAR","PACHECO","ELJURI"}
    ws_prov = sheet.worksheet("BD_PROV")
    all_prov = ws_prov.get_all_values()
    headers_prov = None
    proveedores = {}
    for row in all_prov:
        if "cod_proveedor" in row:
            headers_prov = [h.strip() for h in row]; continue
        if headers_prov is None: continue
        r = dict(zip(headers_prov, row))
        cod = str(r.get("cod_proveedor","")).strip()
        razon = str(r.get("razon_social","")).strip().upper()
        if not cod or r.get("proveedor_inventario","").strip().upper() != "SI": continue
        if not any(p in razon for p in PILOTO): continue
        ventana = str(r.get("ventana_pedido","")).strip()
        dias = [d.strip().upper() for d in ventana.split(",")] if ventana else []
        if hoy.weekday() not in [DIA_MAP[d] for d in dias if d in DIA_MAP]: continue
        proveedores[cod] = {
            "nombre": str(r.get("razon_social","")).strip(),
            "lead_time": int(r.get("lead_time_dias",1) or 1),
            "condicion_pago": str(r.get("condicion_pago","")).strip()
        }
    if not proveedores:
        dia = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][hoy.weekday()]
        return {"pedidos": [], "mensaje": f"Hoy es {dia}, no hay proveedores con ventana de pedido hoy."}
    rows_mp = leer_bd_mp_sistema()
    mps_bajo = {}
    for r in rows_mp:
        cod = str(r.get("cod_mp_sistema","")).strip()
        try:
            stock = _to_float(r.get("stock_actual", "0") or "0")
            par = _to_float(r.get("par_level", "0") or "0")
        except: continue
        if par > 0 and stock < par:
            mps_bajo[cod] = {"nombre_mp": str(r.get("nombre_mp",cod)).strip(), "stock": stock, "par": par}
    ws_items = sheet.worksheet("BD_ITEMS_PROV")
    all_items = ws_items.get_all_values()
    headers_items = None
    pedidos = defaultdict(list)
    seen = set()
    for row in all_items:
        if headers_items is None:
            if "cod_mp_sistema" in row: headers_items = [h.strip() for h in row]
            continue
        if str(row[0]).startswith("[FK]"): continue
        r = dict(zip(headers_items, row))
        cod_mp = str(r.get("cod_mp_sistema","")).strip()
        cod_prov = str(r.get("cod_proveedor","")).strip()
        if cod_mp not in mps_bajo or cod_prov not in proveedores: continue
        key = (cod_mp, cod_prov)
        if key in seen: continue
        seen.add(key)
        try: cant_uc = _to_float(r.get("cantidad_unidad_compra", "1") or "1", 1.0)
        except: cant_uc = 1
        falta = mps_bajo[cod_mp]["par"] - mps_bajo[cod_mp]["stock"]
        unidades = math.ceil(falta/cant_uc) if cant_uc > 0 else math.ceil(falta)
        pedidos[cod_prov].append({
            "nombre": mps_bajo[cod_mp]["nombre_mp"],
            "cantidad": unidades,
            "unidad_compra": str(r.get("unidad_compra","")).strip()
        })
    resultado = []
    for cod_prov, items in pedidos.items():
        resultado.append({
            "proveedor": proveedores[cod_prov]["nombre"],
            "condicion_pago": proveedores[cod_prov]["condicion_pago"],
            "items": items, "n_items": len(items)
        })
    return {"fecha": hoy.isoformat(), "pedidos": resultado}

# ── TOOL 5 — top platos semana ──────────────────────────────
def tool_plato_top_semana():
    sb = conectar_supabase()
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,estado_documento",
        [("gte", "fecha", lunes.isoformat()), ("lte", "fecha", hoy.isoformat())])
    rows = _hist_ventas_sin_anulados(rows)
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "semana": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "ranking": [{"posicion": i+1, "plato": n, "unidades": int(c)} for i,(n,c) in enumerate(top)]
    }

# ── TOOL 6 — buscar bodega ──────────────────────────────────
def tool_buscar_bodega(args):
    from bodegas_config import nombre_bodega, normalizar_cod_bodega

    nombre = args.get("nombre_mp", "").strip().lower()
    rows = leer_bd_mp_sistema()
    resultados = []
    for r in rows:
        if nombre in str(r.get("nombre_mp", "")).strip().lower():
            try:
                stock = _to_float(r.get("stock_actual", "0") or "0")
            except Exception:
                stock = 0
            bod = normalizar_cod_bodega(r.get("cod_bodega", ""))
            resultados.append({
                "cod_mp": str(r.get("cod_mp_sistema", "")).strip(),
                "nombre_mp": str(r.get("nombre_mp", "")).strip(),
                "cod_bodega": bod,
                "nombre_bodega": nombre_bodega(bod),
                "bodega": bod,
                "stock_actual": round(stock, 2),
                "unidad_base": str(r.get("unidad_base", "")).strip(),
            })
    if not resultados:
        return {"encontrado": False, "mensaje": f"No encontre '{args.get('nombre_mp')}' en el sistema."}
    por_mp: dict[str, float] = {}
    for x in resultados:
        por_mp[x["cod_mp"]] = por_mp.get(x["cod_mp"], 0.0) + x["stock_actual"]
    for x in resultados:
        x["stock_total_mp"] = round(por_mp.get(x["cod_mp"], 0.0), 2)
    return {"encontrado": True, "resultados": resultados}

# ── TOOL 7 — trasladar MP ───────────────────────────────────
def tool_trasladar_mp(args):
    from bodegas_config import (
        normalizar_cod_bodega,
        nombre_bodega,
        traslado_permitido,
    )

    cod_mp = args.get("cod_mp_sistema", "").strip()
    bodega_origen = normalizar_cod_bodega(args.get("bodega_origen", ""))
    bodega_destino = normalizar_cod_bodega(args.get("bodega_destino", ""))
    cantidad = float(args.get("cantidad", 0))
    confirmado = args.get("confirmado", False)

    if cantidad <= 0:
        return {"error": "La cantidad debe ser mayor que cero."}
    if not traslado_permitido(bodega_origen, bodega_destino):
        return {
            "error": (
                f"Traslado no permitido: {nombre_bodega(bodega_origen)} → "
                f"{nombre_bodega(bodega_destino)}. "
                "Válidos: cocina↔barra↔externa; consignación↔barra."
            )
        }

    rows = leer_bd_mp_sistema()
    unidad_base = "UNI"
    nombre_mp = ""
    stock_origen = None
    for r in rows:
        if str(r.get("cod_mp_sistema", "")).strip() != cod_mp:
            continue
        if normalizar_cod_bodega(r.get("cod_bodega", "")) == bodega_origen:
            unidad_base = str(r.get("unidad_base", "UNI")).strip()
            nombre_mp = str(r.get("nombre_mp", "")).strip()
            try:
                stock_origen = float(r.get("stock_actual") or 0)
            except (TypeError, ValueError):
                stock_origen = 0.0
            break

    if stock_origen is None:
        return {
            "error": f"No hay fila en maestro para MP {cod_mp} en {nombre_bodega(bodega_origen)}."
        }

    if not confirmado:
        return {
            "requiere_confirmacion": True,
            "stock_origen": round(stock_origen, 4),
            "mensaje": (
                f"Confirmas trasladar {cantidad} {unidad_base} de {cod_mp} "
                f"({nombre_mp}) de {nombre_bodega(bodega_origen)} "
                f"a {nombre_bodega(bodega_destino)}? "
                f"Stock origen actual: {round(stock_origen, 4)}. "
                "Responde 'si confirmo el traslado' para ejecutar."
            ),
        }

    sb = conectar_supabase()
    now = datetime.now(TZ)
    cod_base = f"TRA-{now.strftime('%Y%m%d%H%M%S')}"
    obs = f"Traslado WA {bodega_origen} → {bodega_destino}"

    sb.table("mov_inventario").insert({
        "cod_mov": cod_base + "-SAL",
        "fecha": now.isoformat(),
        "tipo_mov": "TRASLADO_SALIDA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": nombre_mp,
        "cod_bodega_origen": bodega_origen,
        "cod_bodega_destino": None,
        "cantidad_mov": cantidad,
        "unidad_base": unidad_base,
        "origen_documento": "TRASLADO",
        "num_documento": cod_base,
        "registrado_por": "AGENTE_WHATSAPP",
        "observaciones": obs,
    }).execute()

    sb.table("mov_inventario").insert({
        "cod_mov": cod_base + "-ENT",
        "fecha": now.isoformat(),
        "tipo_mov": "TRASLADO_ENTRADA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": nombre_mp,
        "cod_bodega_origen": None,
        "cod_bodega_destino": bodega_destino,
        "cantidad_mov": cantidad,
        "unidad_base": unidad_base,
        "origen_documento": "TRASLADO",
        "num_documento": cod_base,
        "registrado_por": "AGENTE_WHATSAPP",
        "observaciones": obs,
    }).execute()

    try:
        from recalcular_stock_sheets import recalcular_produccion

        recalcular_produccion(cod_mp_filtro=cod_mp)
    except Exception as e:
        print(f"  WARN: recalcular tras traslado: {e}")

    invalidar_cache_bd_mp()
    return {
        "ejecutado": True,
        "cod_mov": cod_base,
        "mensaje": (
            f"Traslado registrado: {cantidad} {unidad_base} de {cod_mp} "
            f"de {nombre_bodega(bodega_origen)} a {nombre_bodega(bodega_destino)}. "
            "Stock recalculado en Sheets."
        ),
    }

# ── TOOL 8 — ventas por plato ───────────────────────────────
def _limite_ranking(args: dict) -> int | None:
    """Si args['limite'] es entero > 0, trunca el ranking a ese tamaño; si no, None = sin truncar."""
    raw = args.get("limite")
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def tool_ventas_por_plato(args):
    from ventas_resumen_tools import (
        calcular_resumen_ventas,
        formatear_resumen_ventas_whatsapp,
    )

    sb = conectar_supabase()
    periodo = args.get("periodo", "semana")
    fecha_ini, fecha_fin, label = _rango_periodo_ventas(periodo)

    rows = supabase_query_all(
        sb,
        "hist_ventas",
        "nombre_producto,cantidad_vendida,total,estado_documento,cod_smart_menu",
        [("gte", "fecha", fecha_ini), ("lte", "fecha", fecha_fin)],
    )

    orden = (args.get("orden") or "usd").strip().lower()
    if orden not in ("usd", "cantidad"):
        orden = "usd"
    lim = _limite_ranking(args)

    resumen = calcular_resumen_ventas(rows, orden=orden, limite=lim)
    total_oficial, tickets = _total_desde_hist_ventas_docs(fecha_ini, fecha_fin)
    if total_oficial > 0:
        resumen["total_ventas_usd_oficial"] = total_oficial
        resumen["tickets"] = tickets

    texto = formatear_resumen_ventas_whatsapp(
        resumen,
        periodo_label=label,
        fecha_ini=fecha_ini,
        fecha_fin=fecha_fin,
    )
    return {
        "periodo": label,
        "fecha_ini": fecha_ini,
        "fecha_fin": fecha_fin,
        **resumen,
        "truncado_a": lim,
        "texto_whatsapp": texto,
    }


def _rango_periodo_ventas(periodo: str) -> tuple[str, str, str]:
    from ventas_resumen_tools import _rango_periodo

    return _rango_periodo(periodo)

# ── TOOL 9 — rotación baja ──────────────────────────────────
def tool_rotacion_baja(args):
    sb = conectar_supabase()
    hoy = date.today()
    dias = int(args.get("dias", 7))
    umbral = float(args.get("umbral_unidades", 0))
    fecha_ini = (hoy - timedelta(days=dias)).isoformat()

    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,estado_documento",
        [("gte", "fecha", fecha_ini), ("lte", "fecha", hoy.isoformat())])
    rows = _hist_ventas_sin_anulados(rows)

    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0

    baja = {k:v for k,v in conteo.items() if v <= umbral}
    ranking = sorted(baja.items(), key=lambda x: x[1])
    return {
        "periodo_dias": dias,
        "umbral": umbral,
        "total": len(ranking),
        "platos": [{"plato": n, "unidades_vendidas": round(v)} for n,v in ranking[:20]]
    }

# ── TOOL 10 — stock ingrediente ─────────────────────────────
def tool_stock_ingrediente(args):
    nombre = args.get("nombre_mp","").strip().lower()
    rows = leer_bd_mp_sistema()
    resultados = []
    for r in rows:
        if nombre in str(r.get("nombre_mp","")).strip().lower():
            try:
                stock = _to_float(r.get("stock_actual", "0") or "0")
                par = _to_float(r.get("par_level", "0") or "0")
            except: stock = par = 0
            resultados.append({
                "cod_mp": str(r.get("cod_mp_sistema","")).strip(),
                "nombre_mp": str(r.get("nombre_mp","")).strip(),
                "stock_actual": round(stock,2),
                "par_level": round(par,2),
                "unidad_base": str(r.get("unidad_base","")).strip(),
                "bodega": str(r.get("cod_bodega","")).strip(),
                "bajo_par": stock < par
            })
    if not resultados:
        return {"encontrado": False, "mensaje": f"No encontre '{args.get('nombre_mp')}' en el sistema."}
    return {"encontrado": True, "resultados": resultados}


def tool_consumo_ingrediente_recetas(args):
    """
    Consumo teórico de MP según ventas (PROCESADO) y BD_RECETAS_DETALLE.
    Usa funciones migradas en este módulo (sin consultas_chat_extendidas).
    """
    nombre_mp = (args.get("nombre_mp") or "").strip()
    if not nombre_mp:
        return {"error": "Falta nombre_mp (nombre o cod_mp_sistema en BD_MP_SISTEMA)."}

    fecha_ini = (args.get("fecha_ini") or "").strip()
    fecha_fin = (args.get("fecha_fin") or "").strip()
    periodo = (args.get("periodo") or "semana").strip().lower()

    hoy = date.today()
    if fecha_ini and fecha_fin:
        fi, ff = fecha_ini, fecha_fin
    elif periodo in ("hoy", "dia", "dia_actual"):
        fi = ff = hoy.isoformat()
    elif periodo in ("mes", "mes_actual"):
        fi = date(hoy.year, hoy.month, 1).isoformat()
        ff = hoy.isoformat()
    else:
        lunes = hoy - timedelta(days=hoy.weekday())
        fi, ff = lunes.isoformat(), hoy.isoformat()

    hits = _buscar_mp_por_nombre_o_codigo(nombre_mp)
    if not hits:
        return {"error": f"No encontre '{nombre_mp}' en BD_MP_SISTEMA."}

    if len(hits) > 1:
        return {
            "ambiguo": True,
            "opciones": [
                {
                    "cod_mp_sistema": (h.get("cod_mp_sistema") or "").strip(),
                    "nombre_mp": (h.get("nombre_mp") or "").strip(),
                }
                for h in hits[:15]
            ],
            "mensaje": "Varias MPs coinciden; pide cod_mp exacto o nombre mas especifico.",
        }

    cod = (hits[0].get("cod_mp_sistema") or "").strip()
    nom = (hits[0].get("nombre_mp") or "").strip()
    unidad = (hits[0].get("unidad_base") or "").strip()

    out = calcular_consumo_teorico_mp(fi, ff, cod)
    if not isinstance(out, dict):
        return {"error": "Respuesta invalida del calculo."}
    out["nombre_mp_resuelto"] = nom
    out["unidad_base"] = unidad
    out["periodo_solicitado"] = {"fecha_ini": fi, "fecha_fin": ff}
    return out


# ── Costo teórico platos (BD_RECETAS_DETALLE + MPs + subrecetas) ─
def _buscar_platos_receta(
    *,
    cod_receta: str = "",
    nombre_plato: str = "",
    variedad: str = "",
) -> list[tuple[str, list[dict]]]:
    from calcular_costo_recetas import cargar_contexto_costos
    from recetas_detalle import clave_plato, norm_cod_receta

    _, _, por_plato, _ = cargar_contexto_costos()
    cod = (cod_receta or "").strip()
    nom_q = (nombre_plato or "").strip().lower()
    var = (variedad or "").strip()

    if cod:
        nk = norm_cod_receta(cod)
        if var:
            key = clave_plato(cod, var)
            if key in por_plato:
                return [(key, por_plato[key])]
            return []
        out: list[tuple[str, list[dict]]] = []
        for key, lineas in por_plato.items():
            if lineas and norm_cod_receta(lineas[0].get("cod_receta") or "") == nk:
                out.append((key, lineas))
        return out

    if nom_q:
        hits: list[tuple[str, list[dict]]] = []
        for key, lineas in por_plato.items():
            if not lineas:
                continue
            nombre = (lineas[0].get("nombre_receta") or "").strip().lower()
            varied = (lineas[0].get("variedad_smart_menu") or "").strip().lower()
            if nom_q in nombre or nom_q in varied:
                if var and var.lower() not in varied:
                    continue
                hits.append((key, lineas))
        return hits

    return []


def tool_costo_plato(args):
    """
    Costo teórico por 1 plato vendido (MP + subrecetas), con desglose por línea.
    """
    from calcular_costo_recetas import cargar_contexto_costos, resumen_plato_costo

    cod = (args.get("cod_receta") or "").strip()
    nombre = (args.get("nombre_plato") or args.get("nombre_receta") or "").strip()
    variedad = (args.get("variedad_smart_menu") or args.get("variedad") or "").strip()

    if not cod and not nombre:
        return {"error": "Indica cod_receta o nombre_plato (nombre en BD_RECETAS_DETALLE)."}

    matches = _buscar_platos_receta(
        cod_receta=cod, nombre_plato=nombre, variedad=variedad
    )
    if not matches:
        return {
            "encontrado": False,
            "mensaje": "No encontre plato en BD_RECETAS_DETALLE con esos criterios.",
        }
    if len(matches) > 1 and not variedad and not (
        cod and len(matches) == 1
    ):
        opciones = []
        for key, lineas in matches[:20]:
            ln0 = lineas[0]
            opciones.append(
                {
                    "clave": key,
                    "cod_receta": (ln0.get("cod_receta") or "").strip(),
                    "variedad_smart_menu": (ln0.get("variedad_smart_menu") or "").strip(),
                    "nombre_receta": (ln0.get("nombre_receta") or "").strip(),
                }
            )
        return {
            "ambiguo": True,
            "opciones": opciones,
            "mensaje": "Varias variedades o coincidencias; repite con cod_receta y variedad_smart_menu.",
        }

    costos_mp, unitarios_sub, _, _ = cargar_contexto_costos()
    _key, lineas = matches[0]
    res = resumen_plato_costo(lineas, costos_mp, unitarios_sub)
    detalle = sorted(
        res.get("detalle_lineas") or [],
        key=lambda x: x.get("costo_linea", 0),
        reverse=True,
    )
    return {
        "encontrado": True,
        "cod_receta": res.get("cod_receta"),
        "variedad_smart_menu": res.get("variedad_smart_menu"),
        "nombre_receta": res.get("nombre_receta"),
        "costo_plato_estandar_usd": res.get("costo_plato_estandar"),
        "n_lineas_mp": res.get("n_lineas_mp"),
        "n_lineas_sub": res.get("n_lineas_sub"),
        "lineas_sin_costo": res.get("lineas_sin_costo"),
        "notas": res.get("notas_costo"),
        "desglose": detalle,
        "nota": "Costo por 1 unidad vendida; subrecetas recalculadas desde MPs. Recalcular: calcular_costo_recetas.py --produccion",
    }


def _norm_sub_cod_wa(cod: str) -> str:
    s = (cod or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return str(int(s))
    return s


def _buscar_subrecetas(
    *,
    cod_subreceta: str = "",
    nombre_subreceta: str = "",
) -> list[tuple[str, dict, list[dict]]]:
    from calcular_costo_subrecetas import cargar_contexto_subrecetas

    cab, por_padre, _, _ = cargar_contexto_subrecetas()
    cod = (cod_subreceta or "").strip()
    nom_q = (nombre_subreceta or "").strip().lower()
    hits: list[tuple[str, dict, list[dict]]] = []

    if cod:
        nk = _norm_sub_cod_wa(cod)
        for c, info in cab.items():
            if _norm_sub_cod_wa(c) == nk:
                hits.append((c, info, por_padre.get(c, [])))
        return hits

    if nom_q:
        for c, info in cab.items():
            nombre = (info.get("nombre_subreceta") or "").strip().lower()
            if nom_q in nombre:
                hits.append((c, info, por_padre.get(c, [])))
        return hits

    return []


def tool_costo_subreceta(args):
    """Costo teórico del lote estándar de una subreceta (MPs + hijos) con desglose."""
    from calcular_costo_subrecetas import (
        calcular_costos,
        cargar_contexto_subrecetas,
        resumen_subreceta_costo,
    )

    cod = (args.get("cod_subreceta") or "").strip()
    nombre = (args.get("nombre_subreceta") or "").strip()

    if not cod and not nombre:
        return {"error": "Indica cod_subreceta o nombre_subreceta."}

    matches = _buscar_subrecetas(cod_subreceta=cod, nombre_subreceta=nombre)
    if not matches:
        return {
            "encontrado": False,
            "mensaje": "No encontre subreceta en BD_SUBRECETAS con esos criterios.",
        }
    if len(matches) > 1 and not cod:
        opciones = [
            {
                "cod_subreceta": c,
                "nombre_subreceta": (info.get("nombre_subreceta") or "").strip(),
                "rendimiento_estandar": info.get("rendimiento_estandar"),
                "unidad": (info.get("unidad") or "").strip(),
            }
            for c, info, _ in matches[:20]
        ]
        return {
            "ambiguo": True,
            "opciones": opciones,
            "mensaje": "Varias subrecetas coinciden; repite con cod_subreceta.",
        }

    cab_all, por_padre, costos_mp, _ = cargar_contexto_subrecetas()
    resultados, _ = calcular_costos(cab_all, por_padre, costos_mp)
    cod_key, cab, lineas = matches[0]
    res = resumen_subreceta_costo(cod_key, cab, lineas, costos_mp, resultados)
    res["encontrado"] = True
    res["nota"] = (
        "Cantidades del lote estandar (rendimiento_estandar). "
        "Costo unitario = costo_lote / rendimiento. "
        "Recalcular: calcular_costo_subrecetas.py --produccion"
    )
    return res


def tool_receta_ingredientes(args):
    """Alias orientado a cantidades + costos por línea de un plato (misma fuente que costo_plato)."""
    out = tool_costo_plato(args)
    if out.get("encontrado"):
        out["tipo_consulta"] = "receta_plato"
        out["ingredientes"] = out.pop("desglose", [])
    return out


def tool_auditar_costos_recetas(args=None):
    """Resumen de platos con costo inflado y MPs sospechosos en recetas."""
    from auditar_costos_recetas import auditar

    args = args or {}
    umbral_plato = _to_float(args.get("umbral_plato"), 25.0)
    umbral_linea = _to_float(args.get("umbral_linea"), 20.0)
    top_p = int(args.get("top_platos") or 10)
    top_l = int(args.get("top_lineas") or 12)

    platos, lineas = auditar(
        umbral_plato=umbral_plato,
        umbral_linea=umbral_linea,
        umbral_cu_gr=_to_float(args.get("umbral_cu_gr"), 0.08),
    )
    return {
        "platos_inflados_total": len(platos),
        "lineas_mp_sospechosas_total": len(lineas),
        "umbral_plato_usd": umbral_plato,
        "umbral_linea_usd": umbral_linea,
        "platos_inflados": platos[:top_p],
        "lineas_mp_sospechosas": lineas[:top_l],
        "nota": (
            "Flags comunes: linea_mp_cara, costo_unitario_alto_gr_ml, "
            "posible_precio_kg_como_gr_x1000. CSV completo: auditar_costos_recetas.py"
        ),
    }


# ── TOOL — ventas día específico ─────────────────────────
def tool_ventas_dia(args):
    fecha = args.get("fecha", "").strip()
    if not fecha:
        fecha = date.today().isoformat()

    sm = total_smartmenu_dia(fecha)

    sb = conectar_supabase()
    rows = supabase_query_all(
        sb,
        "hist_ventas",
        "nombre_producto,cantidad_vendida,total,estado_documento,num_documento",
        [("eq", "fecha", fecha)],
    )
    rows = _hist_ventas_sin_anulados(rows)

    if not rows and sm is None:
        return {
            "fecha": fecha,
            "total_ventas": 0,
            "tickets": 0,
            "platos": [],
            "sin_datos": True,
        }

    conteo = defaultdict(lambda: {"cantidad": 0, "total": 0})
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(sin nombre)"
        conteo[nombre]["cantidad"] += _to_float(r.get("cantidad_vendida"), 0)
        conteo[nombre]["total"] += _to_float(r.get("total"), 0.0)
    # Ordenar por monto (USD neto) para que el "que se vendió" sea un ranking útil.
    ranking = sorted(conteo.items(), key=lambda x: x[1]["total"], reverse=True)
    lim = _limite_ranking(args)
    if lim is not None:
        ranking = ranking[:lim]

    resultado = {
        "fecha": fecha,
        "platos": [
            {"plato": n, "cantidad": round(d["cantidad"]), "total_usd": round(d["total"], 2)}
            for n, d in ranking
        ],
        "total_productos_distintos": len(conteo),
        "truncado_a": lim,
    }
    if sm is not None:
        resultado["total_ventas"] = round(sm.get("total", 0), 2)
        resultado["ventas_brutas"] = round(sm.get("total_bruto", sm.get("total", 0)), 2)
        resultado["descuentos"] = round(sm.get("total_descuentos", 0), 2)
        resultado["tickets"] = sm.get("docs", 0)
        resultado["fuente"] = "Smart Menu"
    else:
        total_hv, docs_hv = _total_desde_hist_ventas_docs(fecha, fecha)
        resultado["total_ventas"] = total_hv
        resultado["tickets"] = docs_hv
        resultado["fuente"] = "hist_ventas"
    return resultado


# ── TOOLS conteo físico (inicio de flujo vía WA) ───────────────
def tool_conteo_iniciar(args):
    cod_bodega = (args.get("cod_bodega") or "").strip()
    anio = args.get("anio")
    semana = args.get("semana_iso")
    try:
        return iniciar_conteo_wa(
            cod_bodega,
            anio=int(anio) if anio is not None else None,
            semana_iso=int(semana) if semana is not None else None,
            sheet_name=(args.get("sheet_name") or "").strip() or None,
            reemplazar_snapshot=bool(args.get("reemplazar_snapshot")),
            sobreescribir_hoja=bool(args.get("sobreescribir_hoja", True)),
            responsable_nombre=(args.get("responsable_nombre") or "").strip() or None,
            responsable_contacto=(args.get("responsable_contacto") or "").strip() or None,
            notas=(args.get("notas") or "").strip() or None,
        )
    except ConteoOperacionError as e:
        return {"ok": False, "error": e.code, "mensaje": e.message, "detalles": e.details}
    except Exception as e:
        return {"ok": False, "error": "ERROR", "mensaje": str(e)}


def tool_conteo_listar_ciclos(args):
    from conteo_fisico import listar_ciclos_api

    estado = (args.get("estado") or "").strip() or None
    cod_bodega = (args.get("cod_bodega") or "").strip() or None
    limit = int(args.get("limit") or 10)
    rows = listar_ciclos_api(estado=estado, cod_bodega=cod_bodega, limit=limit)
    return {
        "total": len(rows),
        "ciclos": [
            {
                "ciclo_id": r.get("id"),
                "estado": r.get("estado"),
                "cod_bodega": r.get("cod_bodega"),
                "anio": r.get("anio"),
                "semana_iso": r.get("semana_iso"),
                "sheet_name": r.get("sheet_name"),
                "snapshot_at": r.get("snapshot_at"),
            }
            for r in rows
        ],
    }


def tool_conteo_ciclos_abiertos(args=None):
    return resumen_ciclos_abiertos()


def _parse_iniciar_conteo_comando(texto: str) -> str | None:
    """INICIAR CONTEO BOD-001 → cod_bodega o None si no aplica."""
    t = (texto or "").strip().upper()
    if not t.startswith("INICIAR CONTEO"):
        return None
    resto = texto.strip()[len("INICIAR CONTEO") :].strip()
    if not resto:
        return ""
    return resto.split()[0].strip()


# ── Definición tools Claude API ──────────────────────────────
TOOLS = [
    {"name": "ventas_hoy", "description": "Ventas del dia actual: total oficial Smart Menu, tickets, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ventas_semana", "description": "Ventas de la semana actual lunes a hoy: total oficial, promedio diario, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "stock_critico", "description": "Listado de insumos bajo par level (par_level>0 y stock_actual<par_level) ordenado por deficit_pct. Por defecto devuelve TODO; puedes pasar top para truncar.", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}, "required": []}},
    {"name": "stocks_negativos", "description": "Listado de materias primas con stock_actual negativo en BD_MP_SISTEMA. No inventa datos.", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}, "required": []}},
    {"name": "inventario_valorizado", "description": "Valorización de inventario (stock_actual*costo_unitario_ref) desde BD_MP_SISTEMA. Si el usuario pide el valorizado de un insumo concreto (ej. camarones), pasa nombre_mp o buscar con esa palabra: busca por substring en nombre_mp sin importar tildes y singular/plural simple. Con nombre_mp se listan también MPs sin costo de referencia o con stock 0 para explicar por qué no hay valor.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}, "buscar": {"type": "string"}, "cod_bodega": {"type": "string"}, "top": {"type": "integer"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}, "incluir_cero": {"type": "boolean"}}, "required": []}},
    {"name": "inventario_por_bodega", "description": "Resumen de valor (USD) y stock total por bodega desde BD_MP_SISTEMA.", "input_schema": {"type": "object", "properties": {"incluir_sin_costo": {"type": "boolean"}}, "required": []}},
    {"name": "facturas_parciales", "description": "Facturas con estado PARCIAL en Supabase (facturas_procesadas).", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": []}},
    {"name": "items_pendientes_factura", "description": "Ítems pendientes (BD_ITEMS_PENDIENTES) filtrando por num_factura o ruc_proveedor.", "input_schema": {"type": "object", "properties": {"num_factura": {"type": "string"}, "ruc_proveedor": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": []}},
    {"name": "compras_facturas_rango", "description": "Valor y detalle de COMPRAS por facturas ya aplicadas a inventario (mov_inventario: entradas por factura) entre fecha_desde y fecha_hasta (YYYY-MM-DD inclusive). Devuelve total USD, cantidad de lineas y facturas, top facturas y top productos (MP), y totales por proveedor (RUC). Usar cuando pregunten compras a proveedores en un periodo, ej. desde 1 de mayo hasta hoy.", "input_schema": {"type": "object", "properties": {"fecha_desde": {"type": "string"}, "fecha_hasta": {"type": "string"}, "top_facturas": {"type": "integer"}, "top_productos": {"type": "integer"}}, "required": ["fecha_desde", "fecha_hasta"]}},
    {"name": "mp_incompletas", "description": "MPs con datos incompletos en BD_MP_SISTEMA (sin_costo, sin_par, sin_bodega).", "input_schema": {"type": "object", "properties": {"tipo": {"type": "string", "enum": ["sin_costo","sin_par","sin_bodega"]}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": []}},
    {"name": "resumen_operativo_hoy", "description": "Resumen compacto: ventas hoy + bajo par + negativos + facturas parciales.", "input_schema": {"type": "object", "properties": {"top": {"type": "integer"}}, "required": []}},
    {"name": "pedidos_hoy", "description": "Pedidos que corresponde hacer hoy segun ventana de cada proveedor.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "plato_top_semana", "description": "Top 10 platos mas vendidos esta semana por cantidad.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "buscar_bodega", "description": "En que bodega se encuentra un ingrediente o insumo.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "trasladar_mp", "description": "Trasladar un insumo de una bodega a otra. Siempre pedir confirmacion antes de ejecutar.", "input_schema": {"type": "object", "properties": {"cod_mp_sistema": {"type": "string"}, "bodega_origen": {"type": "string"}, "bodega_destino": {"type": "string"}, "cantidad": {"type": "number"}, "confirmado": {"type": "boolean"}}, "required": ["cod_mp_sistema","bodega_origen","bodega_destino","cantidad","confirmado"]}},
    {"name": "ventas_por_plato", "description": "Ventas al cliente (hist_ventas): total del periodo + ranking SOLO productos en BD_PRODUCTOS (carta). Nunca inventes platos que no esten en ranking. Periodo hoy/semana/mes. orden: usd (default) o cantidad. Devuelve texto_whatsapp: copialo tal cual al usuario. Incluye desglose_variedades para BAO y similares. PUBLICIDAD Y PROPAGANDA y otros en catalogo si aplican.", "input_schema": {"type": "object", "properties": {"periodo": {"type": "string", "enum": ["hoy","semana","mes"]}, "orden": {"type": "string", "enum": ["usd", "cantidad"]}, "limite": {"type": "integer"}}, "required": ["periodo"]}},
    {"name": "rotacion_baja", "description": "Productos con nula o baja rotacion en los ultimos N dias.", "input_schema": {"type": "object", "properties": {"dias": {"type": "integer"}, "umbral_unidades": {"type": "number"}}, "required": []}},
    {"name": "stock_ingrediente", "description": "Cuanto tengo en inventario de un ingrediente especifico.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "consumo_ingrediente_recetas", "description": "Consumo teorico de una materia prima segun ventas (hist_ventas estado_match PROCESADO) y gramajes en BD_RECETAS_DETALLE; misma logica que el descargo de inventario; NO es stock en bodega. Devuelve total_consumo_teorico y por_plato (lista completa por nombre_producto de venta). No inventes filas ni subtotales: la suma de consumo_mp en por_plato debe coincidir con total_consumo_teorico. nombre_mp obligatorio. Periodo: semana (default lunes a hoy), mes, hoy; o fecha_ini y fecha_fin ISO.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}, "periodo": {"type": "string", "enum": ["semana", "mes", "hoy"]}, "fecha_ini": {"type": "string"}, "fecha_fin": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "costo_plato", "description": "Costo teorico en USD de preparar 1 plato vendido (food cost estandar): suma MPs y subrecetas en BD_RECETAS_DETALLE. Usar cuando pregunten cuanto cuesta hacer un plato, margen de un producto, costo de hamburguesa/bao/etc. Pasa cod_receta (ej. 17) y opcional variedad_smart_menu, o nombre_plato (substring en nombre_receta). Devuelve costo_plato_estandar_usd y desglose por linea (MP/SUB, cantidad, unidad_base, costo_unitario, costo_linea).", "input_schema": {"type": "object", "properties": {"cod_receta": {"type": "string"}, "nombre_plato": {"type": "string"}, "nombre_receta": {"type": "string"}, "variedad_smart_menu": {"type": "string"}, "variedad": {"type": "string"}}, "required": []}},
    {"name": "receta_ingredientes", "description": "Ingredientes y costos de un plato vendido (cantidades por 1 unidad + USD por linea y total). Misma logica que costo_plato; usar cuando pidan receta, ingredientes, gramajes o desglose de un plato (ej. TARTA VASCA, BAO). cod_receta o nombre_plato; opcional variedad.", "input_schema": {"type": "object", "properties": {"cod_receta": {"type": "string"}, "nombre_plato": {"type": "string"}, "nombre_receta": {"type": "string"}, "variedad_smart_menu": {"type": "string"}, "variedad": {"type": "string"}}, "required": []}},
    {"name": "costo_subreceta", "description": "Costo teorico del lote estandar de una subreceta (BD_SUBRECETAS_DETALLE): MPs y subrecetas hijas con cantidades, unidad_base, costo_unitario y costo_linea; total lote y costo por unidad de rendimiento. Usar para salsas, masas, rellenos, etc. Pasa cod_subreceta (ej. 010) o nombre_subreceta (substring).", "input_schema": {"type": "object", "properties": {"cod_subreceta": {"type": "string"}, "nombre_subreceta": {"type": "string"}}, "required": []}},
    {"name": "auditar_costos_recetas", "description": "Auditoria de costos de platos inflados y lineas MP sospechosas en recetas (precio/kg mal como USD/gr, garnish caro en bebidas, sin costo). Usar cuando pidan revisar costos de carta, platos raros caros, o validar recetas vs costos. Devuelve top platos_inflados y lineas_mp_sospechosas con flags.", "input_schema": {"type": "object", "properties": {"umbral_plato": {"type": "number"}, "umbral_linea": {"type": "number"}, "top_platos": {"type": "integer"}, "top_lineas": {"type": "integer"}}, "required": []}},
    {"name": "ventas_dia", "description": "Ventas de un dia (fecha YYYY-MM-DD; default hoy): total, tickets, y TODOS los productos/platos distintos con cantidad y monto. Devuelve la lista `platos` ordenada por total_usd (USD neto) desc. Opcional limite solo si piden top N.", "input_schema": {"type": "object", "properties": {"fecha": {"type": "string"}, "limite": {"type": "integer"}}, "required": []}},
    {"name": "conteo_iniciar", "description": "Inicia inventario físico cíclico: crea conteo_ciclo en Supabase, carga snapshot de MPs de la bodega y genera pestaña CONTEO/CONTEO_BARRA en el maestro Sheets. Usar cuando pidan empezar conteo, toma de inventario, inventario físico de cocina o barra. cod_bodega obligatorio (BOD-001 cocina, BOD-002 barra). semana_iso/anio opcionales (default semana ISO actual). Devuelve ciclo_id, URL de la hoja e instrucciones.", "input_schema": {"type": "object", "properties": {"cod_bodega": {"type": "string"}, "anio": {"type": "integer"}, "semana_iso": {"type": "integer"}, "sheet_name": {"type": "string"}, "reemplazar_snapshot": {"type": "boolean"}, "sobreescribir_hoja": {"type": "boolean"}, "responsable_nombre": {"type": "string"}, "notas": {"type": "string"}}, "required": ["cod_bodega"]}},
    {"name": "conteo_listar_ciclos", "description": "Lista ciclos de inventario físico en Supabase (conteo_ciclo). Filtros opcionales estado y cod_bodega.", "input_schema": {"type": "object", "properties": {"estado": {"type": "string"}, "cod_bodega": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}},
    {"name": "conteo_ciclos_abiertos", "description": "Resumen de ciclos de conteo que NO están CONTABILIZADO ni ANULADO (borradores activos).", "input_schema": {"type": "object", "properties": {}, "required": []}},
]

TOOL_FNS = {
    "ventas_hoy":       lambda a: tool_ventas_hoy(),
    "ventas_semana":    lambda a: tool_ventas_semana(),
    "stock_critico":    tool_stock_critico,
    "stocks_negativos": tool_stocks_negativos,
    "inventario_valorizado": tool_inventario_valorizado,
    "inventario_por_bodega": tool_inventario_por_bodega,
    "facturas_parciales": tool_facturas_parciales,
    "items_pendientes_factura": tool_items_pendientes_factura,
    "compras_facturas_rango": tool_compras_facturas_rango,
    "mp_incompletas": tool_mp_incompletas,
    "resumen_operativo_hoy": tool_resumen_operativo_hoy,
    "pedidos_hoy":      lambda a: tool_pedidos_hoy(),
    "plato_top_semana": lambda a: tool_plato_top_semana(),
    "buscar_bodega":    tool_buscar_bodega,
    "trasladar_mp":     tool_trasladar_mp,
    "ventas_por_plato": tool_ventas_por_plato,
    "rotacion_baja":    tool_rotacion_baja,
    "stock_ingrediente": tool_stock_ingrediente,
    "consumo_ingrediente_recetas": tool_consumo_ingrediente_recetas,
    "costo_plato": tool_costo_plato,
    "receta_ingredientes": tool_receta_ingredientes,
    "costo_subreceta": tool_costo_subreceta,
    "auditar_costos_recetas": lambda a: tool_auditar_costos_recetas(a),
    "ventas_dia":        tool_ventas_dia,
    "conteo_iniciar": tool_conteo_iniciar,
    "conteo_listar_ciclos": tool_conteo_listar_ciclos,
    "conteo_ciclos_abiertos": lambda a: tool_conteo_ciclos_abiertos(),
}

SYSTEM = """Eres el agente de gestion de Tatami Bao Bar, gastrobar asiatico en Cuenca, Ecuador.
Respondes preguntas sobre ventas, inventario, bodegas y pedidos con datos reales del sistema.
Responde siempre en espanol, de forma clara y directa, como si hablaras con el socio del restaurante.
Usa los datos exactos de las tools. Si no hay datos dilo claramente.
Regla estricta VENTAS vs COMPRAS: si preguntan cuanto se vendio, ventas del mes/semana, productos mas vendidos al cliente, usa `ventas_por_plato` (periodo mes/semana/hoy). NO uses compras_facturas_rango salvo que pregunten explicitamente compras a proveedores o facturas de compra.
Para resumen de ventas del periodo con `ventas_por_plato`: responde copiando literalmente el campo `texto_whatsapp` de la tool, sin reescribir ni agregar platos.
NUNCA inventes productos (ej. TATAMI WINGS, EDAMAME) que no esten en `ranking` de la tool. Solo existen los platos en BD_PRODUCTOS que aparecen en el JSON.
Si preguntan detalle de UN solo dia, usa `ventas_dia` y el array `platos` exacto de la tool.
Si la lista es larga y no hay texto_whatsapp, continua en mensajes siguientes sin inventar filas.
Si te piden listados de stock negativo, usa la tool stocks_negativos (no adivines nombres ni cantidades).
Si te piden productos bajo par level, usa la tool stock_critico y devuelve el listado completo salvo que el usuario pida \"top N\".
Si te piden valorizacion de inventario, usa inventario_valorizado (y si preguntan por bodegas usa inventario_por_bodega).
Si piden el valorizado de un producto o materia prima por nombre (ej. camarones, aceite), llama inventario_valorizado con nombre_mp o buscar igual al texto que dio el usuario; no listes solo el top global sin filtrar por nombre.
Si te piden facturas pendientes/parciales, usa facturas_parciales e items_pendientes_factura.
Si preguntan compras a proveedores, valor de compras, productos comprados o cantidades en un rango de fechas (facturas ya registradas en inventario), usa compras_facturas_rango con fecha_desde y fecha_hasta en formato YYYY-MM-DD (usa el contexto temporal para 'hoy' y calcula el primero del mes si dicen 'desde mayo').
Si el listado es largo y el usuario pidio TODO el detalle (ej. todos los platos vendidos con cantidades y montos), enumera el listado COMPLETO que devuelve la tool sin acortar a top 10. Si no cabe en un mensaje, continua en mensajes siguientes numerados.
Para resumenes cortos puede bastar un parrafo; para pedidos explicitos de detalle completo, no resumas.
Si preguntan cuanto se consumio de un ingrediente o materia prima en un periodo segun las recetas de los platos vendidos (no el stock en bodega), usa la tool consumo_ingrediente_recetas. No digas que el sistema no puede cruzar ventas con recetas: esa tool existe.
Con consumo_ingrediente_recetas: enumera TODAS las filas de por_plato que devuelve la tool (nombres vienen de hist_ventas). El total de consumo en gramos debe ser exactamente total_consumo_teorico; no sumes de cabeza cifras inventadas ni mezcles con otros periodos. Si un nombre de plato no corresponde al menu real, dilo: los datos vienen de ventas y recetas enlazadas; puede haber producto mal nombrado, receta incorrecta o matching viejo.
Si preguntan cuanto cuesta hacer/preparar un plato (food cost, costo de receta, margen teorico del plato), usa costo_plato con cod_receta o nombre_plato; muestra el desglose que devuelve la tool.
Si piden ingredientes, gramajes, cantidades o desglose con costos de un plato (receta de venta), usa receta_ingredientes (o costo_plato; mismo resultado).
Si preguntan costo, ingredientes o cantidades de una subreceta o semi (salsa, masa, relleno), usa costo_subreceta con cod_subreceta o nombre_subreceta; enumera todas las lineas del desglose (MP y SUB hijo) con cantidad, unidad y USD.
Si piden revisar platos con costos muy altos, bebidas caras en costo, o MPs mal valorados en recetas, usa auditar_costos_recetas.
Inventario físico / conteo cíclico: para INICIAR un nuevo conteo (crear ciclo + snapshot + hoja Sheets), usa conteo_iniciar con cod_bodega BOD-001 (cocina, hoja CONTEO) o BOD-002 (barra, hoja CONTEO_BARRA). No pidas ejecutar scripts de terminal al usuario. Para ver borradores activos usa conteo_ciclos_abiertos. Tras capturar en Sheets, el envío es menú Conteo → Enviar a Tatami; la aprobación por WA es APROBAR TODO cuando exista sesión de revisión.
Comando directo (sin tool): el usuario puede escribir INICIAR CONTEO BOD-001.
No uses markdown, asteriscos ni negritas. Solo texto plano.
Para traslados entre bodegas usa trasladar_mp. Bodegas: BOD-001 cocina, BOD-002 barra, BOD-003 consignacion, BOD-005 externa (BOD-004 limpieza inactiva). Traslados permitidos: cocina<->barra<->externa; consignacion<->barra. SIEMPRE pide confirmacion antes de ejecutar.
Stock y PAR: el stock es por bodega; el par_level es global por materia prima (suma stock en todas las bodegas para comparar).
Descargo de ventas solo afecta cocina o barra segun cod_bodega en la receta.
Cuando la fuente sea hist_ventas aclaralo como aproximado.
Nunca inventes ni calcules fechas de memoria: siempre usa el bloque "Contexto temporal" que recibes y las fechas ISO indicadas al llamar ventas_dia."""

_MESES = (
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


def _contexto_fechas_ecuador() -> str:
    """Evita que el modelo alucine 'ayer' (p. ej. 18 de enero) o use UTC."""
    ahora = datetime.now(TZ)
    hoy = ahora.date()
    ayer = hoy - timedelta(days=1)
    dias = (
        "lunes",
        "martes",
        "miercoles",
        "jueves",
        "viernes",
        "sabado",
        "domingo",
    )
    return (
        "Contexto temporal (America/Guayaquil, Ecuador):\n"
        f"- hoy: {hoy.isoformat()} ({dias[hoy.weekday()]} {hoy.day} de {_MESES[hoy.month]} de {hoy.year})\n"
        f"- ayer: {ayer.isoformat()} ({dias[ayer.weekday()]} {ayer.day} de {_MESES[ayer.month]} de {ayer.year})\n"
        "Reglas: si el usuario dice 'ayer', llama ventas_dia con fecha exactamente "
        f'"{ayer.isoformat()}". Si dice "hoy", usa "{hoy.isoformat()}". '
        "No uses otra fecha para ayer/hoy."
    )


historiales = {}


def _asegurar_texto_whatsapp(texto: str | None, *, max_len: int = 3800) -> str:
    """WhatsApp no debe quedar en silencio: respuesta vacía confunde al usuario."""
    s = (texto or "").strip()
    if not s:
        return (
            "No obtuve texto de respuesta. Prueba de nuevo, en una sola línea, "
            "o reformula la pregunta."
        )
    if len(s) > max_len:
        return s[: max_len - 30] + "\n...[mensaje recortado]"
    return s


def _system_completo() -> str:
    return SYSTEM + "\n\n" + _contexto_fechas_ecuador()

def llamar_agente(mensaje, telefono):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    if telefono not in historiales:
        historiales[telefono] = []
    historiales[telefono].append({"role": "user", "content": mensaje})
    messages = list(historiales[telefono])
    max_tool_rounds = 48
    n_round = 0
    while True:
        n_round += 1
        if n_round > max_tool_rounds:
            msg = (
                "Demasiadas herramientas en una sola petición. "
                "Escribe de nuevo la consulta en partes más pequeñas."
            )
            historiales[telefono].append({"role": "assistant", "content": msg})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            return msg
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=_system_completo(),
            tools=TOOLS,
            messages=messages,
        )
        texto = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text": texto += block.text
            elif block.type == "tool_use": tool_calls.append(block)
        if response.stop_reason == "end_turn" or not tool_calls:
            out = (texto or "").strip() or _asegurar_texto_whatsapp("")
            historiales[telefono].append({"role": "assistant", "content": out})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            return out
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        texto_ventas_directo = ""
        for tc in tool_calls:
            fn = TOOL_FNS.get(tc.name)
            try:
                result = fn(tc.input) if fn else {"error": f"Tool {tc.name} no encontrada"}
            except Exception as e:
                result = {"error": str(e)}
            if (
                tc.name == "ventas_por_plato"
                and isinstance(result, dict)
                and (result.get("texto_whatsapp") or "").strip()
            ):
                texto_ventas_directo = (result.get("texto_whatsapp") or "").strip()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False)
            })
        if texto_ventas_directo and len(tool_calls) == 1:
            out = _asegurar_texto_whatsapp(texto_ventas_directo)
            historiales[telefono].append({"role": "assistant", "content": out})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            return out
        messages.append({"role": "user", "content": tool_results})


# ── Meta WhatsApp Cloud API (verify + webhook URL típica /webhook) ──
@app.get("/webhook")
async def verificar_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected = (os.getenv("WHATSAPP_VERIFY_TOKEN") or "").strip()
    if mode == "subscribe" and token and expected and token.strip() == expected:
        return PlainTextResponse(str(challenge) if challenge is not None else "")
    return PlainTextResponse("Forbidden", status_code=403)


async def enviar_mensaje_meta(telefono: str, texto: str) -> bool:
    phone_number_id = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not phone_number_id or not token:
        print("WARN: falta WHATSAPP_PHONE_NUMBER_ID o WHATSAPP_ACCESS_TOKEN en .env")
        return False

    url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        print(f"[Meta] Enviado a {telefono}: {resp.status_code}")
        if resp.status_code >= 400:
            try:
                print(f"[Meta] Error body: {resp.text[:500]}")
            except Exception:
                pass
        return resp.status_code == 200
    except Exception as e:
        print(f"[Meta] Error enviando mensaje: {e}")
        return False


async def enviar_documento_meta(
    telefono: str,
    contenido: bytes,
    nombre_archivo: str,
    mime: str = "application/octet-stream",
) -> bool:
    phone_number_id = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not phone_number_id or not token:
        return False
    try:
        upload_url = f"https://graph.facebook.com/v25.0/{phone_number_id}/media"
        async with httpx.AsyncClient(timeout=30) as client:
            upload_resp = await client.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (nombre_archivo, contenido, mime)},
                data={"messaging_product": "whatsapp"},
            )
        media_id = upload_resp.json().get("id")
        if not media_id:
            print(f"[Meta] No se obtuvo media_id: {upload_resp.text[:300]}")
            return False
        send_url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "document",
            "document": {"id": media_id, "filename": nombre_archivo},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                send_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        return resp.status_code == 200
    except Exception as e:
        print(f"[Meta] Error enviando documento: {e}")
        return False


MENSAJE_PROCESANDO_FACTURA = "Procesando factura... ⏳"


async def encolar_wa_mensaje(wa_id: str, msg: dict) -> None:
    """
    Serializa procesamiento por wa_id: Lock + cola de pendientes.
    Si ya hay uno en curso, avisa una vez y encola el resto.
    """
    _wa_pending[wa_id].append(msg)
    lock = _wa_locks.setdefault(wa_id, asyncio.Lock())
    if lock.locked():
        if wa_id not in _wa_cola_avisado and len(_wa_pending[wa_id]) > 1:
            _wa_cola_avisado.add(wa_id)
            await enviar_mensaje_meta(wa_id, MSG_COLA_ESPERA)
        return
    asyncio.create_task(_wa_runner(wa_id))


async def _wa_runner(wa_id: str) -> None:
    lock = _wa_locks.setdefault(wa_id, asyncio.Lock())
    async with lock:
        try:
            while _wa_pending[wa_id]:
                msg = _wa_pending[wa_id].popleft()
                await procesar_mensaje(wa_id, msg)
        finally:
            _wa_cola_avisado.discard(wa_id)
            if _wa_pending[wa_id]:
                asyncio.create_task(_wa_runner(wa_id))


async def procesar_mensaje(wa_id: str, msg: dict) -> None:
    """Procesa un mensaje de Meta en background (POST /webhook ya respondió 200)."""
    try:
        mtype = (msg.get("type") or "").strip()

        if mtype == "text":
            texto = (msg.get("text", {}).get("body") or "").strip()
            if not texto:
                return
            print(f"[Meta] {wa_id}: {texto}")

            texto_upper = texto.strip().upper()

            # Comandos de conteo físico
            sesion_conteo = get_sesion_activa(wa_id)
            if sesion_conteo:
                sid = sesion_conteo["id"]
                envio_id = sesion_conteo["envio_id"]

                if texto_upper == "APROBAR TODO":
                    try:
                        aprobar_items(sid, cods=None)
                        sb = conectar_supabase()
                        sb.table("conteo_envio").update(
                            {"estado_aprobacion": "APROBADO_TOTAL"}
                        ).eq("id", envio_id).execute()
                        sb.table("conteo_envio_detalle").update(
                            {"estado_linea": "APROBADO"}
                        ).eq("envio_id", envio_id).eq(
                            "estado_linea", "PENDIENTE_APROBACION"
                        ).execute()
                        res_cont = contabilizar_envio(
                            sb,
                            envio_id,
                            registrado_por=wa_id,
                            cerrar_ciclo=True,
                            recalcular_sheets=True,
                        )
                        cerrar_sesion(sid)
                        numero_moises = (os.getenv("ALERTA_WA_MOISES") or "").strip()
                        numero_felipe = (os.getenv("ALERTA_WA_FELIPE") or "").strip()
                        wa_norm = wa_id.lstrip("+").strip()
                        otro = (
                            numero_felipe
                            if wa_norm == numero_moises.lstrip("+")
                            else numero_moises
                        )
                        if otro:
                            sesion_otro = get_sesion_activa(otro)
                            if sesion_otro:
                                cerrar_sesion(sesion_otro["id"])
                        out = (
                            "Conteo aprobado y contabilizado.\n"
                            f"Movimientos insertados: {res_cont['movimientos_insertados']}\n"
                            f"Sin ajuste (delta < umbral): {res_cont['saltadas_umbral']}"
                        )
                        if res_cont.get("advertencias"):
                            out += "\nAvisos: " + "; ".join(res_cont["advertencias"])
                    except Exception as e:
                        out = f"Error al contabilizar: {e}"
                    await enviar_mensaje_meta(wa_id, out)
                    return

                if texto_upper.startswith("APROBAR "):
                    nombre = texto[8:].strip()
                    resultado = aprobar_items(sid, cods=[nombre])
                    if resultado["aprobados"]:
                        pendientes = len(resultado["pendientes"])
                        out = (
                            f"Aprobado: {nombre}.\nPendientes: {pendientes} ítems.\n"
                            "Responde APROBAR TODO cuando termines."
                        )
                    else:
                        out = (
                            f"No encontré '{nombre}' en los pendientes. "
                            "Verifica el nombre exacto."
                        )
                    await enviar_mensaje_meta(wa_id, out)
                    return

                if texto_upper.startswith("RECHAZAR "):
                    nombre = texto[9:].strip()
                    resultado = rechazar_items(sid, cods=[nombre])
                    if resultado["rechazados"]:
                        pendientes = len(resultado["pendientes"])
                        out = f"Rechazado: {nombre}.\nPendientes: {pendientes} ítems."
                    else:
                        out = f"No encontré '{nombre}' en los pendientes."
                    await enviar_mensaje_meta(wa_id, out)
                    return

                if texto_upper.startswith("KARDEX "):
                    nombre = texto[7:].strip()
                    deltas = json.loads(sesion_conteo["deltas_pendientes"])
                    delta_item = next(
                        (d for d in deltas if nombre.upper() in d["nombre_mp"].upper()),
                        None,
                    )
                    if not delta_item:
                        await enviar_mensaje_meta(
                            wa_id,
                            f"No encontré '{nombre}' en las diferencias del conteo.",
                        )
                        return
                    fecha_hasta = date.today().isoformat()
                    fecha_desde = (date.today() - timedelta(days=30)).isoformat()
                    try:
                        kardex = get_kardex(
                            delta_item["cod_mp_sistema"], fecha_desde, fecha_hasta
                        )
                        texto_kardex = formatear_kardex_wa(
                            kardex,
                            stock_snapshot=delta_item["stock_snapshot"],
                            conteo_fisico=delta_item["conteo_fisico"],
                            costo_ref=delta_item.get("costo_ref"),
                        )
                    except Exception as e:
                        texto_kardex = f"Error generando kardex: {e}"
                    await enviar_mensaje_meta(wa_id, texto_kardex)
                    return

                if texto_upper.startswith("CSV "):
                    nombre = texto[4:].strip()
                    deltas = json.loads(sesion_conteo["deltas_pendientes"])
                    delta_item = next(
                        (d for d in deltas if nombre.upper() in d["nombre_mp"].upper()),
                        None,
                    )
                    if not delta_item:
                        await enviar_mensaje_meta(
                            wa_id,
                            f"No encontré '{nombre}' en las diferencias del conteo.",
                        )
                        return
                    fecha_hasta = date.today().isoformat()
                    fecha_desde = (date.today() - timedelta(days=30)).isoformat()
                    try:
                        kardex = get_kardex(
                            delta_item["cod_mp_sistema"], fecha_desde, fecha_hasta
                        )
                        xlsx_bytes = generar_xlsx(kardex)
                        nombre_archivo = (
                            f"kardex_{delta_item['cod_mp_sistema']}_{fecha_desde}.xlsx"
                        )
                        await enviar_documento_meta(
                            wa_id,
                            xlsx_bytes,
                            nombre_archivo,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    except Exception as e:
                        await enviar_mensaje_meta(wa_id, f"Error generando Excel: {e}")
                    return

            # Comando rápido: INICIAR CONTEO BOD-001
            cod_bod_rapido = _parse_iniciar_conteo_comando(texto)
            if cod_bod_rapido is not None:
                if not cod_bod_rapido:
                    ay, aw = semana_iso_actual()
                    out = (
                        "Uso: INICIAR CONTEO BOD-001\n"
                        "o INICIAR CONTEO BOD-002 (barra).\n"
                        f"Semana actual: W{aw} {ay}."
                    )
                else:
                    try:
                        r = iniciar_conteo_wa(cod_bod_rapido, sobreescribir_hoja=True)
                        out = (
                            f"Conteo iniciado — {r['cod_bodega']} W{r['semana_iso']}/{r['anio']}\n"
                            f"ciclo_id: {r['ciclo_id']}\n"
                            f"Hoja: {r['sheet_name']} ({r['mps_en_snapshot']} MPs)\n"
                            f"URL: {r.get('url_hoja', '')}\n\n"
                            "Pasos:\n"
                            + "\n".join(f"• {x}" for x in r.get("instrucciones", []))
                        )
                    except ConteoOperacionError as e:
                        out = f"No se pudo iniciar conteo: {e.message}"
                    except Exception as e:
                        out = f"Error: {e}"
                await enviar_mensaje_meta(wa_id, out)
                return

            # Sesión de factura activa
            if hay_sesion_activa(wa_id):
                conf = parse_confirmacion_factura(texto)
                if conf.get("action") in ("cancel", "apply"):
                    try:
                        resp = await handle_confirmacion(texto, wa_id)
                    except Exception as e:
                        print(f"[Meta] handle_confirmacion: {e}")
                        resp = (
                            f"Error al confirmar: {e!s}. Reenvía el archivo o escribe CANCELAR."
                        )
                    out = _asegurar_texto_whatsapp(resp)
                    await enviar_mensaje_meta(wa_id, out)
                    return

            # Agente general
            try:
                respuesta = llamar_agente(texto, wa_id)
            except Exception as e:
                print(f"[Meta] llamar_agente: {e}")
                respuesta = (
                    "Error al contactar el modelo. "
                    f"Detalle técnico: {e!s}. Intenta en unos minutos."
                )
            out = _asegurar_texto_whatsapp(respuesta)
            ok_send = await enviar_mensaje_meta(wa_id, out)
            if not ok_send:
                print(f"[Meta] enviar_mensaje_meta fallo para wa_id={wa_id!r}")
            return

        if mtype in ("image", "document"):
            await enviar_mensaje_meta(wa_id, MENSAJE_PROCESANDO_FACTURA)
            try:
                respuesta = await handle_mensaje_media(msg, wa_id)
            except Exception as e:
                print(f"[Meta] handle_mensaje_media: {e}")
                respuesta = f"No pude procesar el archivo: {e!s}"
            out = _asegurar_texto_whatsapp(respuesta)
            ok_send = await enviar_mensaje_meta(wa_id, out)
            if not ok_send:
                print(f"[Meta] enviar_mensaje_meta fallo (media) wa_id={wa_id!r}")
    except Exception as e:
        print(f"[Meta] procesar_mensaje wa_id={wa_id!r}: {e}")


@app.post("/webhook")
async def recibir_webhook_meta(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return {"status": "ok"}

    print(f"[Meta webhook] {json.dumps(data, ensure_ascii=False)[:300]}")
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    msg_id = (msg.get("id") or "").strip()
                    if msg_id and mensaje_ya_procesado(msg_id):
                        continue
                    wa_id = (msg.get("from") or "").strip()
                    if not wa_id:
                        continue
                    background_tasks.add_task(encolar_wa_mensaje, wa_id, msg)
    except Exception as e:
        print(f"[Meta webhook] Error: {e}")
    return {"status": "ok"}


@app.post("/whatsapp")
async def webhook(Body: str = Form(...), From: str = Form(...)):
    telefono = (From or "").strip()
    body = (Body or "").strip()
    print(f"[{telefono}] {body}")
    lock = _wa_locks.setdefault(telefono, asyncio.Lock())
    async with lock:
        try:
            respuesta = llamar_agente(body, telefono)
        except Exception as e:
            respuesta = f"Error interno: {str(e)}"
    print(f"[Agente] {respuesta}")
    twiml = MessagingResponse()
    twiml.message(_asegurar_texto_whatsapp(respuesta))
    return PlainTextResponse(str(twiml), media_type="application/xml")

@app.get("/")
def health():
    return {"status": "ok", "agente": "Tatami Bao Bar v4", "tools": len(TOOLS)}
