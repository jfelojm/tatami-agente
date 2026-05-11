import os
import re
from collections import defaultdict
from datetime import date, timedelta

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from supabase import create_client

from config_sheets import cfg_tokens
from consultas_chat_extendidas import intento_consultas_extendidas
from ventas_smartmenu_total import calcular_total_smartmenu

load_dotenv(override=True)

# IDs: BD_CONFIG chat_habilitar_tipos y CHAT_HABILITAR_TIPOS (coma)
CHAT_TIPOS = frozenset(
    {
        "ventas_dia",
        "ventas_semana",
        "stock_critico",
        "bodega_producto",
        "traslado_bodegas",
        "ventas_por_plato",
        "rotacion_productos",
        "inventario_ingrediente",
        "consumo_ingrediente",
    }
)

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


def _parse_fecha_en_texto(q: str) -> str | None:
    """
    Extrae una fecha desde texto en formatos comunes:
    - YYYY-MM-DD
    - DD/MM/YYYY o D/M/YYYY
    Devuelve YYYY-MM-DD o None.
    """
    q = (q or "").strip()
    if not q:
        return None

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", q)
    if m:
        dd = int(m.group(1))
        mm = int(m.group(2))
        yyyy = int(m.group(3))
        return f"{yyyy:04d}-{mm:02d}-{dd:02d}"

    # Variante "6 5 2026"
    m = re.search(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b", q)
    if m:
        dd = int(m.group(1))
        mm = int(m.group(2))
        yyyy = int(m.group(3))
        return f"{yyyy:04d}-{mm:02d}-{dd:02d}"

    return None


def _parse_fecha_relativa(q: str) -> str | None:
    """
    Reconoce palabras como 'hoy', 'ayer', 'anteayer' y retorna YYYY-MM-DD.
    """
    q = (q or "").strip().lower()
    if not q:
        return None

    if re.search(r"\bhoy\b", q):
        return date.today().isoformat()
    if re.search(r"\banteayer\b", q):
        return (date.today() - timedelta(days=2)).isoformat()
    if re.search(r"\bayer\b", q):
        return (date.today() - timedelta(days=1)).isoformat()
    return None


def ventas_dia(fecha_iso: str) -> str:
    sb = conectar_supabase()

    total_sm = calcular_total_smartmenu(fecha_iso, sin_iva=True)
    total_ventas = float(total_sm.get("total") or 0.0)
    docs = int(total_sm.get("docs") or 0)

    sel = "nombre_producto,cantidad_vendida,num_documento"
    try:
        sb.table("hist_ventas").select("estado_documento").limit(1).execute()
        sel += ",estado_documento"
    except Exception:
        pass

    res = sb.table("hist_ventas").select(sel, count="exact").eq("fecha", fecha_iso).execute()
    rows = res.data or []
    rows = [
        r
        for r in rows
        if (r.get("estado_documento") or "ACTIVO").strip().upper() != "ANULADO"
    ]
    tickets = len(
        set(r.get("num_documento") for r in rows if (r.get("num_documento") or "").strip())
    )

    conteo = defaultdict(float)
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(SIN NOMBRE)"
        try:
            conteo[nombre] += float(r.get("cantidad_vendida") or 0)
        except Exception:
            pass
    top5 = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:5]

    lineas = [
        f"Ventas {fecha_iso} (SUBTOTAL sin IVA, Smart Menu): ${total_ventas:,.2f}",
        f"Documentos (Smart Menu): {docs}",
        f"Tickets (hist_ventas): {tickets}",
    ]
    if top5:
        lineas.append("Top 5 vendidos:")
        for n, c in top5:
            lineas.append(f"- {n}: {int(c)} unidades")
    else:
        lineas.append("Top 5 vendidos: sin datos en hist_ventas (¿ya importaste ese día?)")

    return "\n".join(lineas)


def ventas_semana_actual() -> str:
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    total = 0.0
    d = lunes
    while d <= hoy:
        total += calcular_total_smartmenu(d.isoformat(), sin_iva=True).get("total", 0.0)
        d += timedelta(days=1)
    return f"Ventas semana (SUBTOTAL sin IVA, Smart Menu): ${total:,.2f}"


def stock_critico() -> str:
    sheet = conectar_sheets()
    ws = sheet.worksheet("BD_MP_SISTEMA")
    all_values = ws.get_all_values()
    headers = [h.strip() for h in all_values[2]]

    criticos = []
    for row in all_values[3:]:
        if not any(row):
            continue
        r = dict(zip(headers, row))
        cod = str(r.get("cod_mp_sistema", "")).strip()
        if not cod:
            continue
        try:
            from sheet_numbers import parse_sheet_number

            stock = parse_sheet_number(r.get("stock_actual", "0") or "0", 0.0)
            par = parse_sheet_number(r.get("par_level", "0") or "0", 0.0)
        except ValueError:
            continue
        if par > 0 and stock < par:
            criticos.append(
                (
                    str(r.get("nombre_mp", cod)).strip(),
                    stock,
                    par,
                    str(r.get("unidad_base", "")).strip(),
                )
            )

    if not criticos:
        return "Todos los insumos están sobre par level."

    criticos.sort(key=lambda x: x[1] / x[2] if x[2] else 0)
    out = ["Stock crítico (top 10):"]
    for nombre, stock, par, unidad in criticos[:10]:
        out.append(f"- {nombre}: {stock:.0f}/{par:.0f} {unidad}")
    if len(criticos) > 10:
        out.append(f"... y {len(criticos) - 10} más bajo par level")
    return "\n".join(out)


def chat_tipos_habilitados() -> frozenset[str]:
    """
    Qué capacidades del chat están activas (BD_CONFIG chat_habilitar_tipos).
    Variable de entorno CHAT_HABILITAR_TIPOS (coma) tiene prioridad si está definida.

    Valores: ventas_dia, ventas_semana, stock_critico, bodega_producto, traslado_bodegas,
    ventas_por_plato, rotacion_productos, inventario_ingrediente, consumo_ingrediente
    Si falta la clave / está vacío / solo valores inválidos → todas habilitadas.
    """
    raw_env = (os.getenv("CHAT_HABILITAR_TIPOS") or "").strip()
    if raw_env:
        parts = {
            p.strip().lower()
            for p in raw_env.replace(";", ",").split(",")
            if p.strip()
        }
        sel = parts & CHAT_TIPOS
        return frozenset(CHAT_TIPOS if not sel else sel)

    toks = cfg_tokens("chat_habilitar_tipos", set())
    if not toks:
        return frozenset(CHAT_TIPOS)
    sel = {t.lower() for t in toks} & CHAT_TIPOS
    return frozenset(CHAT_TIPOS if not sel else sel)


def _mensaje_no_habilitado(tipo_pedido: str, hab: frozenset[str]) -> str:
    nombres = {
        "ventas_dia": "ventas por día (fechas, hoy, ayer)",
        "ventas_semana": "ventas de la semana",
        "stock_critico": "stock crítico / qué falta en bodega",
        "bodega_producto": "en qué bodega está un producto",
        "traslado_bodegas": "traslado entre bodegas",
        "ventas_por_plato": "ventas por plato ($ y cantidad)",
        "rotacion_productos": "rotación baja / productos que casi no vendieron",
        "inventario_ingrediente": "cuánto hay de un ingrediente",
        "consumo_ingrediente": "consumo teórico de un ingrediente según recetas y ventas",
    }
    activos = sorted(hab)
    if not activos:
        return "Las consultas por chat están desactivadas. Revisa BD_CONFIG (clave chat_habilitar_tipos)."
    lineas_ej = _lineas_ejemplo_ayuda(hab)
    return (
        f"«{nombres.get(tipo_pedido, tipo_pedido)}» no está habilitado ahora.\n"
        "Puedes preguntar, por ejemplo:\n"
        + "\n".join(f"- {x}" for x in lineas_ej)
    )


def _lineas_ejemplo_ayuda(hab: frozenset[str]) -> list[str]:
    lineas: list[str] = []
    if "ventas_dia" in hab:
        lineas.extend(
            [
                "'ventas 2026-05-06' o 'ventas 6/5/2026'",
                "'ventas hoy' / 'ventas ayer'",
            ]
        )
    if "ventas_semana" in hab:
        lineas.append("'ventas semana'")
    if "stock_critico" in hab:
        lineas.append("'qué falta en bodega?' / 'stock crítico'")
    if "ventas_por_plato" in hab:
        lineas.append(
            "'ventas por plato esta semana' o 'cuánto vendimos de cada plato en dólares'"
        )
    if "rotacion_productos" in hab:
        lineas.append(
            "'productos que no rotaron esta semana' o 'rotación menor a 5'"
        )
    if "inventario_ingrediente" in hab:
        lineas.append("'cuánto tengo de harina' / 'inventario de aceite'")
    if "consumo_ingrediente" in hab:
        lineas.append(
            "'¿cuánto lomo X se ha consumido esta semana?' / 'consumo teórico de aceite según recetas'"
        )
    if "bodega_producto" in hab:
        lineas.append("'¿en qué bodega está el aceite?'")
    if "traslado_bodegas" in hab:
        lineas.append(
            "'traslada 10 aceite de PRINCIPAL a COCINA' (simulación si no activas ejecución)"
        )
    if not lineas:
        lineas.append("(configura chat_habilitar_tipos en BD_CONFIG)")
    return lineas


def _mensaje_ayuda_chat() -> str:
    hab = chat_tipos_habilitados()
    ej = _lineas_ejemplo_ayuda(hab)
    return "Puedes pedir, por ejemplo:\n" + "\n".join(f"- {x}" for x in ej)


def _split_mensaje_en_preguntas(texto: str) -> list[str]:
    """
    Divide un mensaje en varias consultas (mismo bubble de WhatsApp / una entrada en consola).
    Orden: párrafos, líneas sueltas, varias oraciones con '?', un solo bloque.
    """
    t = (texto or "").strip()
    if not t:
        return []

    bloques = [b.strip() for b in re.split(r"\n\s*\n+", t) if b.strip()]
    if len(bloques) >= 2:
        return bloques

    t = bloques[0] if bloques else t

    lineas = [ln.strip() for ln in t.split("\n") if ln.strip()]
    if len(lineas) >= 2 and all(len(ln) >= 4 for ln in lineas):
        return lineas

    # Varias preguntas en una línea: "¿ventas ayer? qué falta en bodega?"
    partes_con_signo = re.findall(r"[^?]+\?", t)
    resto = re.sub(r"[^?]+\?", "", t).strip()
    trozos = [p.strip() for p in partes_con_signo if p.strip()]
    if resto:
        trozos.append(resto)
    if len(trozos) >= 2:
        return trozos

    return [t]


def _es_consulta_stock_critico(q: str) -> bool:
    """Evita confundir 'cuánto tengo de X' / 'stock de harina' con el listado crítico."""
    if re.search(r"(?:cu[aá]nto|cu[aá]nta)\s+tengo\s+de\s+", q):
        return False
    if re.search(r"\binventario\s+de\s+", q):
        return False
    if re.search(r"\bstock\s+de\s+", q) and not re.search(
        r"critico|cr[ií]tico|falta|alerta|par", q
    ):
        return False
    if re.search(r"\b(?:falta|critico|cr[ií]tico|urgencia|par\s*level)\b", q):
        return True
    if "que falta" in q or "qué falta" in q:
        return True
    if q.strip() in ("stock", "bodega", "inventario", "insumo", "insumos"):
        return True
    if "insumo" in q and ("critico" in q or "crítico" in q or "falta" in q):
        return True
    return False


def _responder_un_fragmento(pregunta: str) -> str:
    q_orig = (pregunta or "").strip()
    q = q_orig.lower()
    if not q:
        return ""

    hab = chat_tipos_habilitados()

    # Palabras que suelen significar "ventas" aunque el usuario no diga 'ventas'
    es_consulta_ventas = (
        any(k in q for k in ["venta", "ventas", "cierre", "cerraron", "total"])
        or bool(re.search(r"\bvend", q))  # vendio/vendió/vendido/vendimos/vendieron...
        or ("cuanto" in q and any(k in q for k in ["hoy", "ayer", "anteayer"]))
    )

    # 1) Fecha explícita, respondemos ese día.
    fecha = _parse_fecha_en_texto(q)
    if fecha and es_consulta_ventas:
        if "ventas_dia" not in hab:
            return _mensaje_no_habilitado("ventas_dia", hab)
        return ventas_dia(fecha)

    # 2) Lenguaje natural: hoy/ayer/anteayer.
    rel = _parse_fecha_relativa(q)
    if rel and (es_consulta_ventas or any(k in q for k in ["actualiza", "actualizar"])):
        if "ventas_dia" not in hab:
            return _mensaje_no_habilitado("ventas_dia", hab)
        return ventas_dia(rel)

    ext, tid = intento_consultas_extendidas(q_orig)
    if ext:
        if tid not in hab:
            return _mensaje_no_habilitado(tid, hab)
        return ext

    if any(k in q for k in ["semana", "semanal"]) and any(k in q for k in ["venta", "ventas"]):
        if "ventas_semana" not in hab:
            return _mensaje_no_habilitado("ventas_semana", hab)
        return ventas_semana_actual()

    if _es_consulta_stock_critico(q):
        if "stock_critico" not in hab:
            return _mensaje_no_habilitado("stock_critico", hab)
        return stock_critico()

    return _mensaje_ayuda_chat()


def responder(pregunta: str) -> str:
    partes = _split_mensaje_en_preguntas(pregunta)
    if not partes:
        return ""
    if len(partes) == 1:
        return _responder_un_fragmento(partes[0])

    salidas: list[str] = []
    for frag in partes:
        r = _responder_un_fragmento(frag)
        if r:
            salidas.append(r)

    if not salidas:
        return ""
    if len(salidas) == 1:
        return salidas[0]

    return "\n\n────────\n\n".join(
        f"{n}) {r}" for n, r in enumerate(salidas, start=1)
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
