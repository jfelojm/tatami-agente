# whatsapp_webhook.py v3 — 11 tools, totales via Smart Menu, paginacion correcta, traslados alineados
import os, json, math, uuid
from datetime import date, timedelta, datetime
from collections import defaultdict
from dotenv import load_dotenv
from supabase import create_client
import gspread
from google.oauth2.service_account import Credentials
import anthropic
import pytz
from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()
TZ = pytz.timezone("America/Guayaquil")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
app = FastAPI()

# ── Conexiones ───────────────────────────────────────────────
def conectar_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def conectar_sheets():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))

def leer_bd_mp_sistema():
    sheet = conectar_sheets()
    ws = sheet.worksheet("BD_MP_SISTEMA")
    all_values = ws.get_all_values()
    headers = [h.strip() for h in all_values[2]]
    rows = []
    for row in all_values[3:]:
        if not any(row): continue
        r = dict(zip(headers, row))
        if not r.get("cod_mp_sistema","").strip(): continue
        rows.append(r)
    return rows

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

# ── Total oficial via Smart Menu ─────────────────────────────
def total_smartmenu_dia(fecha_str):
    """Llama a calcular_total_smartmenu para obtener el total oficial del día."""
    try:
        import importlib.util, sys
        # Importar dinámicamente desde el mismo directorio
        spec = importlib.util.spec_from_file_location(
            "ventas_smartmenu_total",
            os.path.join(os.path.dirname(__file__), "ventas_smartmenu_total.py")
        )
        mod = importlib.util.load_from_spec(spec)
        spec.loader.exec_module(mod)
        resultado = mod.calcular_total_smartmenu(fecha_str, sin_iva=True)
        return resultado.get("total", 0), resultado.get("docs", 0)
    except Exception as e:
        return None, None  # fallback: indica que Smart Menu no disponible

# ── TOOL 1 — ventas hoy ─────────────────────────────────────
def tool_ventas_hoy():
    hoy = date.today().isoformat()
    total_sm, docs = total_smartmenu_dia(hoy)

    # Top platos desde hist_ventas (ranking, no total $)
    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida",
        [("eq", "fecha", hoy)])
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]

    resultado = {
        "fecha": hoy,
        "top_platos": [{"plato": n, "cantidad": int(c)} for n,c in top5]
    }
    if total_sm is not None:
        resultado["total_ventas"] = round(total_sm, 2)
        resultado["tickets"] = docs
    else:
        # Fallback a hist_ventas si Smart Menu no disponible
        total_hv = sum(r.get("total",0) or 0 for r in rows)
        tickets = len(set(r.get("num_documento","") for r in rows if r.get("num_documento")))
        resultado["total_ventas"] = round(total_hv, 2)
        resultado["tickets"] = tickets
        resultado["nota"] = "Total aproximado desde hist_ventas (Smart Menu no disponible)"
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
        t, docs = total_smartmenu_dia(d.isoformat())
        if t is None:
            sm_disponible = False
            break
        total_oficial += t
        total_docs += (docs or 0)
        d += timedelta(days=1)

    # Fallback y ranking desde hist_ventas
    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total,fecha",
        [("gte", "fecha", lunes.isoformat()), ("lte", "fecha", hoy.isoformat())])

    dias = len(set(r["fecha"] for r in rows))
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]

    if not sm_disponible:
        total_oficial = sum(r.get("total",0) or 0 for r in rows)
        total_docs = len(set(r.get("num_documento","") for r in rows if r.get("num_documento")))

    return {
        "periodo": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "total_ventas": round(total_oficial, 2),
        "dias_activos": dias,
        "tickets": total_docs,
        "promedio_diario": round(total_oficial/dias, 2) if dias else 0,
        "top_platos": [{"plato": n, "cantidad": int(c)} for n,c in top5],
        "fuente": "Smart Menu" if sm_disponible else "hist_ventas (aproximado)"
    }

# ── TOOL 3 — stock crítico ──────────────────────────────────
def tool_stock_critico():
    rows = leer_bd_mp_sistema()
    criticos = []
    for r in rows:
        try:
            stock = float(str(r.get("stock_actual","0") or "0").replace(",","."))
            par = float(str(r.get("par_level","0") or "0").replace(",","."))
        except: continue
        if par > 0 and stock < par:
            criticos.append({
                "nombre": str(r.get("nombre_mp","")).strip(),
                "stock_actual": round(stock,1),
                "par_level": round(par,1),
                "unidad": str(r.get("unidad_base","")).strip(),
                "deficit_pct": round((1-stock/par)*100,1)
            })
    criticos.sort(key=lambda x: x["deficit_pct"], reverse=True)
    return {"total_bajo_par": len(criticos), "top10_criticos": criticos[:10]}

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
            stock = float(str(r.get("stock_actual","0") or "0").replace(",","."))
            par = float(str(r.get("par_level","0") or "0").replace(",","."))
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
        try: cant_uc = float(str(r.get("cantidad_unidad_compra","1") or "1").replace(",","."))
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
        "nombre_producto,cantidad_vendida",
        [("gte", "fecha", lunes.isoformat()), ("lte", "fecha", hoy.isoformat())])
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
    nombre = args.get("nombre_mp","").strip().lower()
    rows = leer_bd_mp_sistema()
    resultados = []
    for r in rows:
        if nombre in str(r.get("nombre_mp","")).strip().lower():
            try: stock = float(str(r.get("stock_actual","0") or "0").replace(",","."))
            except: stock = 0
            resultados.append({
                "cod_mp": str(r.get("cod_mp_sistema","")).strip(),
                "nombre_mp": str(r.get("nombre_mp","")).strip(),
                "bodega": str(r.get("cod_bodega","")).strip(),
                "stock_actual": round(stock,2),
                "unidad_base": str(r.get("unidad_base","")).strip()
            })
    if not resultados:
        return {"encontrado": False, "mensaje": f"No encontre '{args.get('nombre_mp')}' en el sistema."}
    return {"encontrado": True, "resultados": resultados}

# ── TOOL 7 — trasladar MP ───────────────────────────────────
def tool_trasladar_mp(args):
    cod_mp = args.get("cod_mp_sistema","").strip()
    bodega_origen = args.get("bodega_origen","").strip()
    bodega_destino = args.get("bodega_destino","").strip()
    cantidad = float(args.get("cantidad", 0))
    confirmado = args.get("confirmado", False)

    if not confirmado:
        return {
            "requiere_confirmacion": True,
            "mensaje": f"Confirmas trasladar {cantidad} unidades de {cod_mp} de {bodega_origen} a {bodega_destino}? Responde 'si confirmo el traslado' para ejecutar."
        }

    # Obtener unidad_base desde BD_MP_SISTEMA
    rows = leer_bd_mp_sistema()
    unidad_base = "UNI"
    for r in rows:
        if str(r.get("cod_mp_sistema","")).strip() == cod_mp:
            unidad_base = str(r.get("unidad_base","UNI")).strip()
            break

    sb = conectar_supabase()
    now = datetime.now(TZ)
    cod_base = f"TRA-{now.strftime('%Y%m%d%H%M%S')}"

    # Salida desde bodega origen
    sb.table("mov_inventario").insert({
        "cod_mov": cod_base + "-SAL",
        "fecha": now.isoformat(),
        "tipo_mov": "SALIDA_VENTA",  # tipo existente en el esquema
        "cod_mp_sistema": cod_mp,
        "nombre_mp": next((r.get("nombre_mp","") for r in rows if r.get("cod_mp_sistema","").strip() == cod_mp), ""),
        "cod_bodega_origen": bodega_origen,
        "cod_bodega_destino": bodega_destino,
        "cantidad_mov": cantidad,
        "unidad_base": unidad_base,
        "origen_documento": "TRASLADO",
        "num_documento": cod_base,
        "registrado_por": "AGENTE_WHATSAPP",
        "observaciones": f"Traslado de {bodega_origen} a {bodega_destino}"
    }).execute()

    # Entrada en bodega destino
    sb.table("mov_inventario").insert({
        "cod_mov": cod_base + "-ENT",
        "fecha": now.isoformat(),
        "tipo_mov": "ENTRADA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": next((r.get("nombre_mp","") for r in rows if r.get("cod_mp_sistema","").strip() == cod_mp), ""),
        "cod_bodega_origen": bodega_origen,
        "cod_bodega_destino": bodega_destino,
        "cantidad_mov": cantidad,
        "unidad_base": unidad_base,
        "origen_documento": "TRASLADO",
        "num_documento": cod_base,
        "registrado_por": "AGENTE_WHATSAPP",
        "observaciones": f"Traslado de {bodega_origen} a {bodega_destino}"
    }).execute()

    return {
        "ejecutado": True,
        "cod_mov": cod_base,
        "mensaje": f"Traslado registrado: {cantidad} {unidad_base} de {cod_mp} movidas de {bodega_origen} a {bodega_destino}."
    }

# ── TOOL 8 — ventas por plato ───────────────────────────────
def tool_ventas_por_plato(args):
    sb = conectar_supabase()
    periodo = args.get("periodo","semana")
    hoy = date.today()
    if periodo == "hoy":
        fecha_ini = fecha_fin = hoy.isoformat(); label = "hoy"
    elif periodo == "mes":
        fecha_ini = hoy.replace(day=1).isoformat(); fecha_fin = hoy.isoformat()
        label = hoy.strftime("%B %Y")
    else:
        lunes = hoy - timedelta(days=hoy.weekday())
        fecha_ini = lunes.isoformat(); fecha_fin = hoy.isoformat()
        label = f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}"

    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total",
        [("gte", "fecha", fecha_ini), ("lte", "fecha", fecha_fin)])

    conteo = defaultdict(lambda: {"cantidad": 0, "total": 0})
    for r in rows:
        conteo[r["nombre_producto"]]["cantidad"] += r["cantidad_vendida"] or 0
        conteo[r["nombre_producto"]]["total"] += r["total"] or 0

    ranking = sorted(conteo.items(), key=lambda x: x[1]["total"], reverse=True)
    return {
        "periodo": label,
        "total_platos": len(ranking),
        "ranking": [
            {"posicion": i+1, "plato": n, "cantidad": round(d["cantidad"]), "total_usd": round(d["total"],2)}
            for i,(n,d) in enumerate(ranking[:15])
        ]
    }

# ── TOOL 9 — rotación baja ──────────────────────────────────
def tool_rotacion_baja(args):
    sb = conectar_supabase()
    hoy = date.today()
    dias = int(args.get("dias", 7))
    umbral = float(args.get("umbral_unidades", 0))
    fecha_ini = (hoy - timedelta(days=dias)).isoformat()

    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida",
        [("gte", "fecha", fecha_ini), ("lte", "fecha", hoy.isoformat())])

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
                stock = float(str(r.get("stock_actual","0") or "0").replace(",","."))
                par = float(str(r.get("par_level","0") or "0").replace(",","."))
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

# ── TOOL 11 — ventas día específico ─────────────────────────
def tool_ventas_dia(args):
    fecha = args.get("fecha","").strip()
    if not fecha: fecha = date.today().isoformat()

    # Total oficial via Smart Menu
    total_sm, docs = total_smartmenu_dia(fecha)

    sb = conectar_supabase()
    rows = supabase_query_all(sb, "hist_ventas",
        "nombre_producto,cantidad_vendida,total",
        [("eq", "fecha", fecha)])

    if not rows and total_sm is None:
        return {"fecha": fecha, "total_ventas": 0, "tickets": 0, "platos": [], "sin_datos": True}

    conteo = defaultdict(lambda: {"cantidad": 0, "total": 0})
    for r in rows:
        conteo[r["nombre_producto"]]["cantidad"] += r["cantidad_vendida"] or 0
        conteo[r["nombre_producto"]]["total"] += r["total"] or 0
    top = sorted(conteo.items(), key=lambda x: x[1]["cantidad"], reverse=True)[:10]

    resultado = {
        "fecha": fecha,
        "top_platos": [{"plato": n, "cantidad": round(d["cantidad"]), "total_usd": round(d["total"],2)} for n,d in top]
    }
    if total_sm is not None:
        resultado["total_ventas"] = round(total_sm, 2)
        resultado["tickets"] = docs
        resultado["fuente"] = "Smart Menu"
    else:
        resultado["total_ventas"] = round(sum(r.get("total",0) or 0 for r in rows), 2)
        resultado["tickets"] = len(set(r.get("num_documento","") for r in rows if r.get("num_documento")))
        resultado["fuente"] = "hist_ventas (aproximado)"
    return resultado

# ── Definición tools Claude API ──────────────────────────────
TOOLS = [
    {"name": "ventas_hoy", "description": "Ventas del dia actual: total oficial Smart Menu, tickets, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ventas_semana", "description": "Ventas de la semana actual lunes a hoy: total oficial, promedio diario, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "stock_critico", "description": "Insumos bajo par level ordenados por deficit.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "pedidos_hoy", "description": "Pedidos que corresponde hacer hoy segun ventana de cada proveedor.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "plato_top_semana", "description": "Top 10 platos mas vendidos esta semana por cantidad.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "buscar_bodega", "description": "En que bodega se encuentra un ingrediente o insumo.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "trasladar_mp", "description": "Trasladar un insumo de una bodega a otra. Siempre pedir confirmacion antes de ejecutar.", "input_schema": {"type": "object", "properties": {"cod_mp_sistema": {"type": "string"}, "bodega_origen": {"type": "string"}, "bodega_destino": {"type": "string"}, "cantidad": {"type": "number"}, "confirmado": {"type": "boolean"}}, "required": ["cod_mp_sistema","bodega_origen","bodega_destino","cantidad","confirmado"]}},
    {"name": "ventas_por_plato", "description": "Cuanto vendimos de cada plato en dolares y cantidad. Periodo: hoy, semana o mes.", "input_schema": {"type": "object", "properties": {"periodo": {"type": "string", "enum": ["hoy","semana","mes"]}}, "required": ["periodo"]}},
    {"name": "rotacion_baja", "description": "Productos con nula o baja rotacion en los ultimos N dias.", "input_schema": {"type": "object", "properties": {"dias": {"type": "integer"}, "umbral_unidades": {"type": "number"}}, "required": []}},
    {"name": "stock_ingrediente", "description": "Cuanto tengo en inventario de un ingrediente especifico.", "input_schema": {"type": "object", "properties": {"nombre_mp": {"type": "string"}}, "required": ["nombre_mp"]}},
    {"name": "ventas_dia", "description": "Ventas de un dia especifico. Si no se indica fecha usa hoy. Fecha en formato YYYY-MM-DD.", "input_schema": {"type": "object", "properties": {"fecha": {"type": "string"}}, "required": []}},
]

TOOL_FNS = {
    "ventas_hoy":       lambda a: tool_ventas_hoy(),
    "ventas_semana":    lambda a: tool_ventas_semana(),
    "stock_critico":    lambda a: tool_stock_critico(),
    "pedidos_hoy":      lambda a: tool_pedidos_hoy(),
    "plato_top_semana": lambda a: tool_plato_top_semana(),
    "buscar_bodega":    tool_buscar_bodega,
    "trasladar_mp":     tool_trasladar_mp,
    "ventas_por_plato": tool_ventas_por_plato,
    "rotacion_baja":    tool_rotacion_baja,
    "stock_ingrediente":tool_stock_ingrediente,
    "ventas_dia":       tool_ventas_dia,
}

SYSTEM = """Eres el agente de gestion de Tatami Bao Bar, gastrobar asiatico en Cuenca, Ecuador.
Respondes preguntas sobre ventas, inventario, bodegas y pedidos con datos reales del sistema.
Responde siempre en espanol, de forma clara y directa, como si hablaras con el socio del restaurante.
Usa los datos exactos de las tools. Si no hay datos dilo claramente.
Tus respuestas deben ser cortas y claras — es WhatsApp, no un informe.
No uses markdown, asteriscos ni negritas. Solo texto plano.
Para traslados: SIEMPRE pide confirmacion explicitamente antes de ejecutar.
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


def _system_completo() -> str:
    return SYSTEM + "\n\n" + _contexto_fechas_ecuador()

def llamar_agente(mensaje, telefono):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    if telefono not in historiales:
        historiales[telefono] = []
    historiales[telefono].append({"role": "user", "content": mensaje})
    messages = list(historiales[telefono])
    while True:
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
            historiales[telefono].append({"role": "assistant", "content": texto})
            if len(historiales[telefono]) > 20:
                historiales[telefono] = historiales[telefono][-20:]
            return texto
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tc in tool_calls:
            fn = TOOL_FNS.get(tc.name)
            try:
                result = fn(tc.input) if fn else {"error": f"Tool {tc.name} no encontrada"}
            except Exception as e:
                result = {"error": str(e)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False)
            })
        messages.append({"role": "user", "content": tool_results})

@app.post("/whatsapp")
async def webhook(Body: str = Form(...), From: str = Form(...)):
    print(f"[{From}] {Body}")
    try:
        respuesta = llamar_agente(Body.strip(), From)
    except Exception as e:
        respuesta = f"Error interno: {str(e)}"
    print(f"[Agente] {respuesta}")
    twiml = MessagingResponse()
    twiml.message(respuesta)
    return PlainTextResponse(str(twiml), media_type="application/xml")

@app.get("/")
def health():
    return {"status": "ok", "agente": "Tatami Bao Bar v3", "tools": len(TOOLS)}
