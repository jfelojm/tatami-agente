import re
import subprocess
import sys


def _run_reporte() -> str:
    """Ejecuta reporte_semanal.py y retorna stdout como texto."""
    cmd = [sys.executable, "reporte_semanal.py", "--dry-run"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        out = (p.stdout or "").strip()
        raise RuntimeError(f"Error ejecutando reporte_semanal.py\n{out}\n{err}")
    return p.stdout or ""


def _extraer_bloque(texto: str, titulo: str) -> str:
    """
    Extrae el bloque de una sección del reporte.
    titulo: '1. VENTAS' / '2. COSTOS' / '3. ALERTAS DE PRECIO' / '4. STOCK CRITICO (top 10)'
    """
    lines = texto.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.strip() == titulo:
            start = i
            break
    if start is None:
        return ""

    # avanzar 2 líneas (título + separador)
    i = start + 2
    out = []
    while i < len(lines):
        if re.match(r"^\s*\d+\.\s+", lines[i]):
            break
        if lines[i].strip().startswith("=") and out:
            break
        out.append(lines[i])
        i += 1
    return "\n".join([l for l in out if l.strip() != ""]).strip()


def responder(pregunta: str) -> str:
    q = (pregunta or "").strip().lower()
    if not q:
        return ""

    rep = _run_reporte()

    # Intents simples
    if any(k in q for k in ["venta", "ventas", "semana", "semanal", "tickets"]):
        bloque = _extraer_bloque(rep, "1. VENTAS")
        return bloque or "No pude extraer la sección de ventas."

    if any(k in q for k in ["plato", "producto", "vende", "vendido", "top"]):
        bloque = _extraer_bloque(rep, "1. VENTAS")
        # ya trae Top 5 vendidos
        return bloque or "No pude extraer el top de vendidos."

    if any(k in q for k in ["bodega", "stock", "falt", "falta", "critico", "insumo"]):
        bloque = _extraer_bloque(rep, "4. STOCK CRITICO (top 10)")
        return bloque or "No pude extraer la sección de stock crítico."

    if any(k in q for k in ["costo", "costos", "food", "beverage", "f&b"]):
        bloque = _extraer_bloque(rep, "2. COSTOS")
        return bloque or "No pude extraer la sección de costos."

    if any(k in q for k in ["precio", "precios", "variacion", "alerta"]):
        bloque = _extraer_bloque(rep, "3. ALERTAS DE PRECIO")
        return bloque or "No pude extraer la sección de alertas de precio."

    return (
        "Puedo responder cosas como:\n"
        "- 'como van las ventas esta semana?'\n"
        "- 'que plato se vende mas?'\n"
        "- 'que falta en bodega?'\n"
        "- 'costos de la semana'\n"
        "- 'alertas de precio'\n"
    )


def main():
    print("AGENTE CHAT TATAMI (local)")
    print("Escribe tu pregunta. 'salir' para terminar.\n")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSaliendo.")
            return
        if q.lower() in {"salir", "exit", "quit"}:
            print("Saliendo.")
            return
        try:
            r = responder(q)
        except Exception as e:
            print(f"ERROR: {e}")
            continue
        if r:
            print(r)
        print()


if __name__ == "__main__":
    main()

import os, json
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from supabase import create_client
import gspread
from google.oauth2.service_account import Credentials
import anthropic
import pytz

load_dotenv()

TZ = pytz.timezone("America/Guayaquil")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

def conectar_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def conectar_sheets():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))

def tool_ventas_hoy():
    sb = conectar_supabase()
    hoy = date.today().isoformat()
    res = sb.table("hist_ventas").select("nombre_producto, cantidad_vendida, total, num_documento").eq("fecha", hoy).execute()
    rows = res.data
    if not rows:
        return {"fecha": hoy, "total_ventas": 0, "tickets": 0, "top_platos": [], "sin_datos": True}
    total = sum(r["total"] or 0 for r in rows)
    tickets = len(set(r.get("num_documento","") for r in rows if r.get("num_documento")))
    from collections import defaultdict
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]
    return {
        "fecha": hoy,
        "total_ventas": round(total, 2),
        "tickets": tickets,
        "top_platos": [{"plato": n, "cantidad": int(c)} for n, c in top5],
    }

def tool_ventas_semana():
    sb = conectar_supabase()
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    res = sb.table("hist_ventas").select("nombre_producto, cantidad_vendida, total, fecha, num_documento").gte("fecha", lunes.isoformat()).lte("fecha", hoy.isoformat()).execute()
    rows = res.data
    if not rows:
        return {"periodo": f"{lunes} al {hoy}", "total_ventas": 0, "dias_activos": 0, "top_platos": []}
    total = sum(r["total"] or 0 for r in rows)
    dias_activos = len(set(r["fecha"] for r in rows))
    tickets = len(set(r.get("num_documento","") for r in rows if r.get("num_documento")))
    from collections import defaultdict
    conteo = defaultdict(float)
    for r in rows:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]
    return {
        "periodo": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "total_ventas": round(total, 2),
        "dias_activos": dias_activos,
        "tickets": tickets,
        "promedio_diario": round(total / dias_activos, 2) if dias_activos else 0,
        "top_platos": [{"plato": n, "cantidad": int(c)} for n, c in top5],
    }

def tool_stock_critico():
    sheet = conectar_sheets()
    ws = sheet.worksheet("BD_MP_SISTEMA")
    all_values = ws.get_all_values()
    headers = [h.strip() for h in all_values[2]]
    criticos = []
    for row in all_values[3:]:
        if not any(row):
            continue
        r = dict(zip(headers, row))
        cod = str(r.get("cod_mp_sistema","")).strip()
        if not cod:
            continue
        try:
            stock = float(str(r.get("stock_actual","0") or "0").replace(",","."))
            par = float(str(r.get("par_level","0") or "0").replace(",","."))
        except ValueError:
            continue
        if par > 0 and stock < par:
            criticos.append({
                "nombre": str(r.get("nombre_mp",cod)).strip(),
                "stock_actual": round(stock, 1),
                "par_level": round(par, 1),
                "unidad": str(r.get("unidad_base","")).strip(),
                "deficit_pct": round((1 - stock/par)*100, 1),
            })
    criticos.sort(key=lambda x: x["deficit_pct"], reverse=True)
    return {"total_bajo_par": len(criticos), "top10_criticos": criticos[:10]}

def tool_pedidos_hoy():
    import math
    from collections import defaultdict
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
            headers_prov = [h.strip() for h in row]
            continue
        if headers_prov is None:
            continue
        r = dict(zip(headers_prov, row))
        cod = str(r.get("cod_proveedor","")).strip()
        razon = str(r.get("razon_social","")).strip().upper()
        if not cod or r.get("proveedor_inventario","").strip().upper() != "SI":
            continue
        if not any(p in razon for p in PILOTO):
            continue
        ventana = str(r.get("ventana_pedido","")).strip()
        dias = [d.strip().upper() for d in ventana.split(",")] if ventana else []
        if hoy.weekday() not in [DIA_MAP[d] for d in dias if d in DIA_MAP]:
            continue
        proveedores[cod] = {
            "nombre": str(r.get("razon_social","")).strip(),
            "lead_time": int(r.get("lead_time_dias",1) or 1),
            "condicion_pago": str(r.get("condicion_pago","")).strip(),
        }

    if not proveedores:
        dia_nombre = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][hoy.weekday()]
        return {"pedidos": [], "mensaje": f"Hoy es {dia_nombre} — no hay proveedores con ventana de pedido hoy."}

    ws_mp = sheet.worksheet("BD_MP_SISTEMA")
    all_mp = ws_mp.get_all_values()
    headers_mp = [h.strip() for h in all_mp[2]]
    mps_bajo = {}
    for row in all_mp[3:]:
        if not any(row):
            continue
        r = dict(zip(headers_mp, row))
        cod = str(r.get("cod_mp_sistema","")).strip()
        if not cod:
            continue
        try:
            stock = float(str(r.get("stock_actual","0") or "0").replace(",","."))
            par = float(str(r.get("par_level","0") or "0").replace(",","."))
        except ValueError:
            continue
        if par > 0 and stock < par:
            mps_bajo[cod] = {"nombre_mp": str(r.get("nombre_mp",cod)).strip(), "stock": stock, "par": par, "unidad": str(r.get("unidad_base","")).strip()}

    ws_items = sheet.worksheet("BD_ITEMS_PROV")
    all_items = ws_items.get_all_values()
    headers_items = None
    pedidos = defaultdict(list)
    seen = set()
    for row in all_items:
        if headers_items is None:
            if "cod_mp_sistema" in row:
                headers_items = [h.strip() for h in row]
            continue
        if str(row[0]).startswith("[FK]"):
            continue
        r = dict(zip(headers_items, row))
        cod_mp = str(r.get("cod_mp_sistema","")).strip()
        cod_prov = str(r.get("cod_proveedor","")).strip()
        if cod_mp not in mps_bajo or cod_prov not in proveedores:
            continue
        key = (cod_mp, cod_prov)
        if key in seen:
            continue
        seen.add(key)
        try:
            cant_uc = float(str(r.get("cantidad_unidad_compra","1") or "1").replace(",","."))
        except:
            cant_uc = 1
        falta = mps_bajo[cod_mp]["par"] - mps_bajo[cod_mp]["stock"]
        unidades = math.ceil(falta / cant_uc) if cant_uc > 0 else math.ceil(falta)
        pedidos[cod_prov].append({
            "nombre": mps_bajo[cod_mp]["nombre_mp"],
            "cantidad": unidades,
            "unidad_compra": str(r.get("unidad_compra","")).strip(),
        })

    resultado = []
    for cod_prov, items in pedidos.items():
        resultado.append({
            "proveedor": proveedores[cod_prov]["nombre"],
            "condicion_pago": proveedores[cod_prov]["condicion_pago"],
            "items": items,
            "n_items": len(items),
        })
    return {"fecha": hoy.isoformat(), "pedidos": resultado}

def tool_plato_top_semana():
    sb = conectar_supabase()
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    res = sb.table("hist_ventas").select("nombre_producto, cantidad_vendida").gte("fecha", lunes.isoformat()).lte("fecha", hoy.isoformat()).execute()
    from collections import defaultdict
    conteo = defaultdict(float)
    for r in res.data:
        conteo[r["nombre_producto"]] += r["cantidad_vendida"] or 0
    top = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "semana": f"{lunes.strftime('%d/%m')} al {hoy.strftime('%d/%m/%Y')}",
        "ranking": [{"posicion": i+1, "plato": n, "unidades": int(c)} for i, (n,c) in enumerate(top)],
    }

TOOLS = [
    {"name": "ventas_hoy", "description": "Ventas del dia actual: total en dolares, tickets emitidos, top 5 platos mas vendidos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "ventas_semana", "description": "Ventas de la semana actual lunes a hoy: total, promedio diario, top 5 platos.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "stock_critico", "description": "Insumos bajo par level ordenados por deficit. Muestra que falta en bodega.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "pedidos_hoy", "description": "Pedidos que corresponde hacer hoy segun ventana de cada proveedor.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "plato_top_semana", "description": "Top 10 platos mas vendidos esta semana.", "input_schema": {"type": "object", "properties": {}, "required": []}},
]

TOOL_FNS = {
    "ventas_hoy": tool_ventas_hoy,
    "ventas_semana": tool_ventas_semana,
    "stock_critico": tool_stock_critico,
    "pedidos_hoy": tool_pedidos_hoy,
    "plato_top_semana": tool_plato_top_semana,
}

SYSTEM = """Eres el agente de gestion de Tatami Bao Bar, gastrobar asiatico en Cuenca, Ecuador.
Respondes preguntas sobre ventas, inventario y pedidos con datos reales del sistema.
Responde siempre en espanol, de forma clara y directa, como si hablaras con el socio del restaurante.
Usa los datos exactos de las tools. Si no hay datos para hoy dilo y ofrece datos de la semana.
No expliques como funcionas internamente. Se util y concreto."""

def chat():
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    historial = []
    now = datetime.now(TZ)
    hora = now.hour
    saludo = "Buenos dias" if hora < 12 else ("Buenas tardes" if hora < 19 else "Buenas noches")

    print("\n" + "="*55)
    print("  TATAMI BAO BAR - Agente IA")
    print("="*55)
    print(f"  {saludo}. Soy el agente de Tatami.")
    print("  Preguntame sobre ventas, stock o pedidos.")
    print("  Escribe 'salir' para terminar.")
    print("="*55 + "\n")

    while True:
        try:
            user_input = input("Tu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nHasta luego.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("salir","exit","quit"):
            print("Agente: Hasta luego.")
            break

        historial.append({"role": "user", "content": user_input})
        messages = list(historial)

        while True:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
            texto = ""
            tool_calls = []
            for block in response.content:
                if block.type == "text":
                    texto += block.text
                elif block.type == "tool_use":
                    tool_calls.append(block)

            if response.stop_reason == "end_turn" or not tool_calls:
                if texto:
                    print(f"\nAgente: {texto}\n")
                    historial.append({"role": "assistant", "content": texto})
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tc in tool_calls:
                fn = TOOL_FNS.get(tc.name)
                try:
                    result = fn() if fn else {"error": f"Tool {tc.name} no encontrada"}
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": tool_results})

if __name__ == "__main__":
    chat()
