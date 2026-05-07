import os
import re
from collections import defaultdict
from datetime import date, timedelta

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from supabase import create_client

from ventas_smartmenu_total import calcular_total_smartmenu

load_dotenv(override=True)

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

    res = (
        sb.table("hist_ventas")
        .select("nombre_producto,cantidad_vendida,num_documento", count="exact")
        .eq("fecha", fecha_iso)
        .execute()
    )
    rows = res.data or []
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
            stock = float(str(r.get("stock_actual", "0") or "0").replace(",", "."))
            par = float(str(r.get("par_level", "0") or "0").replace(",", "."))
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

def responder(pregunta: str) -> str:
    q = (pregunta or "").strip().lower()
    if not q:
        return ""

    # Palabras que suelen significar "ventas" aunque el usuario no diga 'ventas'
    es_consulta_ventas = (
        any(k in q for k in ["venta", "ventas", "cierre", "cerraron", "total"])
        or bool(re.search(r"\bvend", q))  # vendio/vendió/vendido/vendimos/vendieron...
        or ("cuanto" in q and any(k in q for k in ["hoy", "ayer", "anteayer"]))
    )

    # 1) Fecha explícita, respondemos ese día.
    fecha = _parse_fecha_en_texto(q)
    if fecha and es_consulta_ventas:
        return ventas_dia(fecha)

    # 2) Lenguaje natural: hoy/ayer/anteayer.
    rel = _parse_fecha_relativa(q)
    if rel and (es_consulta_ventas or any(k in q for k in ["actualiza", "actualizar"])):
        return ventas_dia(rel)

    if any(k in q for k in ["semana", "semanal"]) and any(k in q for k in ["venta", "ventas"]):
        return ventas_semana_actual()

    if any(k in q for k in ["bodega", "stock", "falt", "falta", "critico", "insumo"]):
        return stock_critico()

    return (
        "Puedes pedir, por ejemplo:\n"
        "- 'ventas 2026-05-06'\n"
        "- 'ventas 6/5/2026'\n"
        "- 'ventas hoy'\n"
        "- 'ventas semana'\n"
        "- 'que falta en bodega?'\n"
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
