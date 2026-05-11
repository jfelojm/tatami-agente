"""
Consultas avanzadas del agente (WhatsApp): bodega por producto, traslados,
ventas por plato, rotación baja, inventario por ingrediente,
consumo teórico de materia prima según recetas (BD_RECETAS_DETALLE) y ventas.

Datos: BD_MP_SISTEMA (Sheets), BD_RECETAS_DETALLE (Sheets), hist_ventas (Supabase).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, timedelta

from gspread.utils import rowcol_to_a1

from config_sheets import cfg

# Import lazy para evitar ciclo: agente_chat.conectar_*


def _sb():
    from agente_chat import conectar_supabase

    return conectar_supabase()


def _sheet():
    from agente_chat import conectar_sheets

    return conectar_sheets()


def _safe_float(v) -> float:
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(v, 0.0)


def _rango_semana_actual() -> tuple[str, str]:
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    return lunes.isoformat(), hoy.isoformat()


def _parse_rango_fechas_en_texto(q: str) -> tuple[str, str] | None:
    """Si hay dos fechas explícitas YYYY-MM-DD o DD/MM/YYYY, retorna (ini, fin)."""
    nums_iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", q)
    if len(nums_iso) >= 2:
        a, b = nums_iso[0], nums_iso[1]
        return (a, b) if a <= b else (b, a)

    dmy = re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", q)
    if len(dmy) >= 2:

        def iso(t):
            dd, mm, yyyy = int(t[0]), int(t[1]), int(t[2])
            return f"{yyyy:04d}-{mm:02d}-{dd:02d}"

        i1, i2 = iso(dmy[0]), iso(dmy[1])
        return (i1, i2) if i1 <= i2 else (i2, i1)
    return None


def _parse_fecha_en_texto_aux(q: str) -> str | None:
    from agente_chat import _parse_fecha_en_texto

    return _parse_fecha_en_texto(q)


def _hist_ventas_en_rango(fecha_ini: str, fecha_fin: str) -> list[dict]:
    sb = _sb()
    sel = "nombre_producto,cantidad_vendida,total,fecha"
    try:
        sb.table("hist_ventas").select("estado_documento").limit(1).execute()
        sel += ",estado_documento"
    except Exception:
        pass

    out: list[dict] = []
    offset = 0
    while True:
        r = (
            sb.table("hist_ventas")
            .select(sel)
            .gte("fecha", fecha_ini)
            .lte("fecha", fecha_fin)
            .range(offset, offset + 999)
            .execute()
        )
        chunk = r.data or []
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return [
        r
        for r in out
        if (r.get("estado_documento") or "ACTIVO").strip().upper() != "ANULADO"
    ]  # sin columna: estado_documento ausente → se cuenta como ACTIVO


def _hist_ventas_en_rango_para_consumo(fecha_ini: str, fecha_fin: str) -> list[dict]:
    """Líneas de venta con datos para cruzar con recetas (misma lógica que descargo_inventario)."""
    sb = _sb()
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
        r = (
            sb.table("hist_ventas")
            .select(sel)
            .gte("fecha", fecha_ini)
            .lte("fecha", fecha_fin)
            .range(offset, offset + 999)
            .execute()
        )
        chunk = r.data or []
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return [
        row
        for row in out
        if (row.get("estado_documento") or "ACTIVO").strip().upper() != "ANULADO"
    ]


def calcular_consumo_teorico_mp(
    fecha_ini: str,
    fecha_fin: str,
    cod_mp_sistema: str,
) -> dict:
    """
    Consumo teórico de un cod_mp_sistema: suma cantidad_vendida × receta (gr/porción, merma, etc.).
    Solo líneas hist_ventas con estado_match=PROCESADO (como el descargo automático).
    """
    from descargo_inventario import (
        _resolver_cod_receta,
        calcular_consumo,
        get_ingredientes,
    )

    cod_target = (cod_mp_sistema or "").strip()
    if not cod_target:
        return {"error": "cod_mp_sistema vacío"}

    rows = _hist_ventas_en_rango_para_consumo(fecha_ini, fecha_fin)
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
        cant_v = _safe_float(r.get("cantidad_vendida"))
        nombre_plato = (r.get("nombre_producto") or "").strip() or "(sin nombre)"

        if not cod_receta:
            lineas_sin_receta += 1
            continue

        ingredientes = get_ingredientes(cod_receta, variedad)
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
    desglose = sorted(
        por_plato.items(),
        key=lambda kv: kv[1]["consumo_mp"],
        reverse=True,
    )

    return {
        "fecha_ini": fecha_ini,
        "fecha_fin": fecha_fin,
        "cod_mp_sistema": cod_target,
        "total_consumo_teorico": round(total, 4),
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
            "equivale a lo que el descargo de inventario descontaría por esas ventas."
        ),
    }


def _es_consulta_consumo_recetas(q: str) -> bool:
    ql = (q or "").strip().lower()
    if re.search(
        r"cu[aá]nt[oa]\s+.+\s+se\s+ha(?:n)?\s+consumido",
        ql,
    ):
        return True
    if re.search(
        r"cu[aá]nt[oa]\s+.+\s+se\s+consumi[oó]",
        ql,
    ):
        return True
    if re.search(
        r"\bconsumo\s+(?:te[oó]rico\s+)?(?:de|del|de\s+la)\s+",
        ql,
    ):
        return True
    if re.search(
        r"\b(?:basad[oa]s?|seg[uú]n)\s+en\s+(?:las\s+)?recetas",
        ql,
    ) and re.search(
        r"\b(?:consum|insumo|ingredient|materia|lomo|harina|aceite)\w*",
        ql,
    ):
        return True
    return False


def _extraer_nombre_mp_consumo(q_raw: str) -> str | None:
    q = (q_raw or "").strip()
    low = q.lower()

    m = re.search(
        r"cu[aá]nt[oa]\s+(.+?)\s+se\s+ha(?:n)?\s+consumido",
        low,
    )
    if not m:
        m = re.search(
            r"cu[aá]nt[oa]\s+(.+?)\s+se\s+consumi[oó]",
            low,
        )
    if m:
        tail = m.group(1).strip()
        tail = re.sub(
            r"\s+en\s+(?:la\s+)?(?:u[lú]ltima\s+)?(?:semana|quincena|mes|semanas)\b.*$",
            "",
            tail,
            flags=re.I,
        )
        tail = re.sub(
            r"\s+del\s+\d{1,2}\s+de\s+.+\s+al\s+.*$",
            "",
            tail,
            flags=re.I,
        )
        tail = re.sub(r"\s+basad[oa]s?\s+en\s+.*$", "", tail, flags=re.I).strip()
        tail = re.sub(r"^[¿?¡!\s]+|[¿?¡!\s]+$", "", tail).strip()
        return tail or None

    m = re.search(
        r"\bconsumo\s+(?:te[oó]rico\s+)?(?:de|del|de\s+la)\s+(.+?)(?:\s+en\s+|\s+esta\s+|\s+del\s+\d|\s+basad|$)",
        low,
    )
    if m:
        tail = m.group(1).strip()
        tail = re.sub(r"\s*\(.*?\)\s*$", "", tail).strip()
        return tail or None

    if re.search(r"\b(?:basad[oa]s?|seg[uú]n)\s+en\s+(?:las\s+)?recetas", low):
        m = re.search(
            r"(?:ingredient|insumo|materia\s+prima|mp)\s+(.+)$",
            low,
        )
        if m:
            return re.sub(r"\s*\(.*?\)\s*$", "", m.group(1)).strip() or None

    return None


def consulta_consumo_ingrediente_recetas(pregunta: str) -> str | None:
    """Consumo teórico de una MP cruzando hist_ventas (PROCESADO) con BD_RECETAS_DETALLE."""
    q_raw = (pregunta or "").strip()
    if not _es_consulta_consumo_recetas(q_raw):
        return None

    nombre_extraido = _extraer_nombre_mp_consumo(q_raw)
    q_lower = q_raw.lower()
    candidato = nombre_extraido or ""

    if len(candidato) < 2:
        # Fallback: despues de "consumo de"
        m = re.search(r"\bconsumo\s+(?:de|del)\s+(.+)$", q_lower)
        if m:
            candidato = re.sub(
                r"\s+en\s+.*$",
                "",
                m.group(1),
                flags=re.I,
            ).strip()

    if len(candidato) < 2:
        return (
            "Indica la materia prima: por ejemplo «consumo teórico de lomo de res Piggis esta semana» "
            "o «¿cuánto aceite se ha consumido en mayo?»."
        )

    rango_ex = _parse_rango_fechas_en_texto(q_lower)
    if rango_ex:
        fecha_ini, fecha_fin = rango_ex
    elif "semana" in q_lower or "última semana" in q_lower or "ultima semana" in q_lower:
        fecha_ini, fecha_fin = _rango_semana_actual()
    elif "mes" in q_lower:
        hoy = date.today()
        fecha_ini = date(hoy.year, hoy.month, 1).isoformat()
        fecha_fin = hoy.isoformat()
    elif mfecha := _parse_fecha_en_texto_aux(q_raw):
        fecha_ini = fecha_fin = mfecha
    else:
        fecha_ini, fecha_fin = _rango_semana_actual()

    hits = _buscar_mp_por_nombre_o_codigo(candidato)
    if not hits:
        return (
            f"No encontré «{candidato}» en BD_MP_SISTEMA. "
            "Revisa el nombre o usa el código cod_mp_sistema como en la hoja."
        )

    if len(hits) > 1:
        opts = [f"- {h.get('nombre_mp', '')} ({h.get('cod_mp_sistema', '')})" for h in hits[:10]]
        mas = f"\n... y {len(hits) - 10} más." if len(hits) > 10 else ""
        return (
            f"Hay varias materias primas que coinciden con «{candidato}». "
            "Indica el código exacto o una frase más específica:\n"
            + "\n".join(opts)
            + mas
        )

    h = hits[0]
    cod = (h.get("cod_mp_sistema") or "").strip()
    nom = (h.get("nombre_mp") or "").strip()
    unidad = (h.get("unidad_base") or "").strip()

    data = calcular_consumo_teorico_mp(fecha_ini, fecha_fin, cod)
    if data.get("error") == "sin_lineas":
        return data.get("mensaje", "Sin datos en el periodo.")

    total = float(data.get("total_consumo_teorico") or 0)
    por_plato = data.get("por_plato") or []

    lineas = [
        f"Consumo teórico de «{nom}» ({cod}) — {fecha_ini} al {fecha_fin}:",
        f"Total: {total:g} {unidad or '(unidad en BD_MP_SISTEMA)'}",
        "",
        "Desglose por plato (solo ventas con matching PROCESADO y receta que incluye este MP):",
    ]
    for p in por_plato[:40]:
        lineas.append(
            f"- {p['nombre_producto']}: {p['unidades_vendidas']:g} und. vendidas → "
            f"{p['consumo_mp']:g} {unidad or ''} de este insumo"
        )
    if len(por_plato) > 40:
        lineas.append(f"... +{len(por_plato) - 40} platos más en el detalle.")

    lineas.append("")
    lineas.append(
        f"Líneas histórico usadas: {data.get('lineas_hist_usadas', 0)} | "
        f"omitidas (match≠PROCESADO): {data.get('lineas_omitidas_match', 0)} | "
        f"sin receta o receta vacía: {data.get('lineas_sin_receta_o_vacia', 0)} | "
        f"plato sin este MP en receta: {data.get('lineas_plato_sin_ese_mp', 0)}"
    )
    if total <= 0 and not por_plato:
        lineas.append(
            "No hubo consumo calculado: revisa que el MP esté en BD_RECETAS_DETALLE en las recetas "
            "de los platos vendidos, y que las ventas tengan estado_match=PROCESADO."
        )
    return "\n".join(lineas)


def _cargar_mp_todas_filas() -> tuple[list[str], list[list[str]], int]:
    """Headers, rows datos (desde fila tras header), índice fila header (0-based)."""
    sh = _sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()
    header_row_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break
    if header_row_idx is None:
        return [], [], -1
    headers = [(c or "").strip() for c in values[header_row_idx]]
    rows = values[header_row_idx + 1 :]
    return headers, rows, header_row_idx


def _buscar_mp_por_nombre_o_codigo(texto: str) -> list[dict]:
    texto_u = (texto or "").strip().lower()
    if len(texto_u) < 2:
        return []
    headers, rows, _ = _cargar_mp_todas_filas()
    if not headers:
        return []
    try:
        icod = headers.index("cod_mp_sistema")
        inom = headers.index("nombre_mp")
    except ValueError:
        return []

    def row_dict(row: list[str]) -> dict:
        return {
            headers[i]: (row[i] if i < len(row) else "").strip()
            for i in range(len(headers))
        }

    hits: list[dict] = []
    for row in rows:
        if not any((c or "").strip() for c in row):
            continue
        cod = (row[icod] if icod < len(row) else "").strip()
        nom = (row[inom] if inom < len(row) else "").strip()
        if not cod:
            continue
        if texto_u == cod.lower():
            hits.insert(0, row_dict(row))
            continue
        if texto_u in nom.lower():
            hits.append(row_dict(row))
    return hits


def consulta_bodega_producto(pregunta: str) -> str | None:
    """
    'en qué bodega está X', 'dónde está el producto X'
    """
    q = (pregunta or "").strip().lower()
    if not (
        re.search(
            r"(?:en\s+que\s+bodega|qu[eé]\s+bodega|d[oó]nde\s+(?:est[aá]|queda|se\s+encuentra)|ubicaci[oó]n\s+(?:del\s+)?(?:producto|insumo|item))",
            q,
        )
        or re.search(r"se\s+encuentra\s+(?:el\s+)?(?:producto|insumo|item)", q)
        or ("bodega" in q and ("donde" in q or "dónde" in q))
    ):
        return None

    # texto después de está/codigo/producto
    m = re.search(
        r"(?:est[aá]|est[aá]n|est[eé]|producto|insumo|item|cod(?:igo)?)\s+(.+)$",
        q,
        re.I,
    )
    candidato = (m.group(1) if m else "").strip()
    candidato = re.sub(
        r"^[¿?¡!\s]+|[¿?¡!\s]+$", "", candidato
    )
    candidato = re.sub(
        r"\s*\(.*?\)\s*$", "", candidato
    ).strip()

    if len(candidato) < 2:
        m2 = re.search(
            r"bodega\s+(?:tiene|hay|guardan)\s+(.+)$", q, re.I
        )
        candidato = (m2.group(1) if m2 else "").strip()

    if len(candidato) < 2:
        return "Indica el nombre o código del producto, por ejemplo: «¿en qué bodega está la harina?»."

    hits = _buscar_mp_por_nombre_o_codigo(candidato)
    if not hits:
        return f"No encontré «{candidato}» en BD_MP_SISTEMA (revisa nombre o código)."

    lineas = []
    for h in hits[:8]:
        cod = h.get("cod_mp_sistema", "")
        nom = h.get("nombre_mp", "")
        bod = (h.get("cod_bodega") or h.get("cod_bodega_destino") or "").strip()
        lineas.append(f"- {nom} ({cod}) → bodega: {bod or '(sin código en hoja)'}")
    if len(hits) > 8:
        lineas.append(f"... y {len(hits) - 8} coincidencias más.")
    return "Ubicación según BD_MP_SISTEMA:\n" + "\n".join(lineas)


def consulta_inventario_ingrediente(pregunta: str) -> str | None:
    """'cuánto tengo de harina', 'inventario de X', 'stock del aceite' (un ítem)."""
    q_raw = (pregunta or "").strip()
    q = q_raw.lower()

    if re.search(r"\b(?:critico|crítico|falta|urgencia|par\s*level)\b", q):
        return None

    m = re.search(
        r"(?:cu[aá]nto\s+(?:tengo|hay)|inventario\s+de|stock\s+(?:del|de\s+la|de)?)\s*(.+)$",
        q,
        re.I,
    )
    if not m:
        if not re.search(r"\btengo\s+de\s+", q):
            return None
        m = re.search(r"tengo\s+de\s+(.+)$", q, re.I)
    if not m:
        return None

    tail = (m.group(1) or "").strip()
    tail = re.sub(r"^[¿?¡!\s]+|[¿?¡!\s]+$", "", tail)
    tail = re.sub(r"\s*\(.*?\)\s*$", "", tail).strip()
    # cortar en conectores
    tail = re.split(
        r"\s+(?:en|para|con|y|o)\s+", tail, maxsplit=1
    )[0].strip()

    if len(tail) < 2:
        return None

    hits = _buscar_mp_por_nombre_o_codigo(tail)
    if not hits:
        return f"No encontré «{tail}» en BD_MP_SISTEMA."

    lineas = []
    for h in hits[:6]:
        cod = h.get("cod_mp_sistema", "")
        nom = h.get("nombre_mp", "")
        st = _safe_float(h.get("stock_actual", 0))
        u = (h.get("unidad_base") or "").strip()
        bod = (h.get("cod_bodega") or "").strip()
        lineas.append(
            f"- {nom} ({cod}): {st:g} {u} — bodega {bod or '—'}"
        )
    if len(hits) > 6:
        lineas.append(f"... +{len(hits) - 6} más.")
    return "Inventario:\n" + "\n".join(lineas)


def consulta_ventas_por_plato(pregunta: str) -> str | None:
    """Ventas desglosadas por plato ($ y unidades) en un periodo."""
    q = (pregunta or "").strip().lower()
    ok = (
        any(
            k in q
            for k in (
                "plato",
                "platos",
                "por plato",
                "desglose",
                "detalle",
                "producto vendido",
            )
        )
        or re.search(r"cada\s+plato", q)
        or (
            re.search(r"vendid", q)
            and ("dolar" in q or "dólar" in q or "$" in q)
            and ("cantidad" in q or "unidad" in q or "und" in q)
        )
    )
    if not ok:
        return None

    rango_ex = _parse_rango_fechas_en_texto(q)
    if rango_ex:
        fecha_ini, fecha_fin = rango_ex
    elif "semana" in q or "esta semana" in q or "semana actual" in q:
        fecha_ini, fecha_fin = _rango_semana_actual()
    elif "mes" in q:
        hoy = date.today()
        fecha_ini = date(hoy.year, hoy.month, 1).isoformat()
        fecha_fin = hoy.isoformat()
    elif m := _parse_fecha_en_texto_aux(q):
        fecha_ini = fecha_fin = m
    else:
        fecha_ini, fecha_fin = _rango_semana_actual()

    rows = _hist_ventas_en_rango(fecha_ini, fecha_fin)
    if not rows:
        return f"Sin líneas en hist_ventas entre {fecha_ini} y {fecha_fin}."

    agg: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(sin nombre)"
        cant = _safe_float(r.get("cantidad_vendida"))
        total = _safe_float(r.get("total"))
        ac, at = agg[nombre]
        agg[nombre] = (ac + cant, at + total)

    items = sorted(agg.items(), key=lambda x: x[1][1], reverse=True)
    lineas = [
        f"Ventas por plato ({fecha_ini} al {fecha_fin}) — cantidad y $ (hist_ventas):",
        f"{'Plato':<42} {'Und':>8} {'Total $':>12}",
        "-" * 64,
    ]
    for nombre, (cant, total) in items:
        nom_c = nombre[:41]
        lineas.append(f"{nom_c:<42} {cant:>8.2f} ${total:>10,.2f}")
    sum_c = sum(x[1][0] for x in items)
    sum_t = sum(x[1][1] for x in items)
    lineas.append("-" * 64)
    lineas.append(f"{'TOTAL':<42} {sum_c:>8.2f} ${sum_t:>10,.2f}")
    return "\n".join(lineas)


def consulta_rotacion_baja(pregunta: str) -> str | None:
    """
    Platos con pocas ventas o sin ventas en el periodo.
    Umbral: 'menor a 5', 'menos de 3', default 1.
    """
    q = (pregunta or "").strip().lower()
    if not any(
        k in q
        for k in (
            "rotacion",
            "rotación",
            "rotaron",
            "rotado",
            "no han vendido",
            "sin ventas",
            "no vendieron",
            "poca venta",
            "baja rotacion",
            "baja rotación",
            "menor a",
            "menos de",
        )
    ):
        return None

    umbral = 1.0
    if m := re.search(r"(?:menor\s+a|menos\s+de|<)\s*(\d+(?:[.,]\d+)?)", q):
        umbral = _safe_float(m.group(1))

    rango_ex = _parse_rango_fechas_en_texto(q)
    if rango_ex:
        fecha_ini, fecha_fin = rango_ex
    elif "semana" in q:
        fecha_ini, fecha_fin = _rango_semana_actual()
    else:
        fecha_ini, fecha_fin = _rango_semana_actual()

    rows = _hist_ventas_en_rango(fecha_ini, fecha_fin)
    cant_por_plato: dict[str, float] = defaultdict(float)
    for r in rows:
        nombre = (r.get("nombre_producto") or "").strip() or "(sin nombre)"
        cant_por_plato[nombre] += _safe_float(r.get("cantidad_vendida"))

    bajos = [(n, c) for n, c in cant_por_plato.items() if c < umbral]
    bajos.sort(key=lambda x: x[1])

    if not bajos:
        return (
            f"Ningún plato con ventas < {umbral:g} und. entre {fecha_ini} y {fecha_fin} "
            "(o no hay datos en hist_ventas)."
        )

    lineas = [
        f"Platos con rotación < {umbral:g} und. ({fecha_ini} al {fecha_fin}):",
    ]
    for n, c in bajos[:35]:
        lineas.append(f"- {n}: {c:g} und.")
    if len(bajos) > 35:
        lineas.append(f"... +{len(bajos) - 35} más.")
    lineas.append(
        "(Solo platos con al menos una línea en hist_ventas; cantidad 0 acumulada si aplica.)"
    )
    return "\n".join(lineas)


def _parse_traslado(pregunta: str) -> dict | None:
    q = (pregunta or "").strip()
    low = q.lower()
    if not re.search(
        r"\b(?:traslad|mover|pasar|transfer)\w*", low
    ):
        return None

    qty_m = re.search(
        r"(?:^|\s)(\d+(?:[.,]\d+)?)\s*(?:kg|g\b|und(?:\.|idades)?|unid(?:\.|ades)?)?\s+",
        low,
    )
    qty = _safe_float(qty_m.group(1)) if qty_m else None

    bd_m = re.search(
        r"de\s+(?:la\s+)?(?:bodega\s+)?([A-Za-z0-9_.-]+)\s+(?:a|hacia|para)\s+(?:la\s+)?(?:bodega\s+)?([A-Za-z0-9_.-]+)",
        low,
        re.I,
    )
    if not bd_m:
        return {"error": "Indica bodegas así: de PRINCIPAL a COCINA (códigos sin espacios)."}

    orig, dest = bd_m.group(1).strip(), bd_m.group(2).strip()

    segment = low[: bd_m.start()]
    product_chunk = segment
    if qty_m:
        product_chunk = segment[qty_m.end() :].strip()
    product_chunk = re.sub(
        r"^(?:traslad|mover|pasar|transfer)\w*\s*", "", product_chunk
    )
    product_chunk = product_chunk.strip(" ,.;")

    nombre_mp = product_chunk
    if not nombre_mp or len(nombre_mp) < 2:
        return {"error": "Indica qué producto trasladar, antes de «de … a …»."}

    return {"qty": qty, "nombre": nombre_mp.strip(), "origen": orig, "destino": dest}


def consulta_traslado(pregunta: str) -> str | None:
    """
    Traslado entre bodegas: por defecto solo simula.
    BD_CONFIG chat_traslados_ejecutar = true para aplicar (actualiza cod_bodega en Sheets).
    """
    parsed = _parse_traslado(pregunta)
    if not parsed:
        return None
    if parsed.get("error"):
        return parsed["error"]

    nombre = parsed["nombre"]
    orig = parsed["origen"]
    dest = parsed["destino"]
    qty = parsed.get("qty")

    ejecutar = bool(cfg("chat_traslados_ejecutar", False)) or (
        (os.getenv("CHAT_TRASLADOS_EJECUTAR") or "").strip().lower()
        in ("1", "true", "si", "sí", "yes")
    )

    hits = _buscar_mp_por_nombre_o_codigo(nombre)
    if not hits:
        return f"No encontré «{nombre}» en BD_MP_SISTEMA para el traslado."

    h = hits[0]
    cod = h.get("cod_mp_sistema", "").strip()
    nom = h.get("nombre_mp", "")
    bod_actual = (h.get("cod_bodega") or "").strip()

    lines = [
        "Traslado (según tu mensaje):",
        f"- Producto: {nom} ({cod})",
        f"- Cantidad indicada: {qty if qty is not None else '(no indicada — en modelo actual solo cambia bodega por ítem)'}",
        f"- De bodega: {orig} → a bodega: {dest}",
        f"- Bodega actual en hoja: {bod_actual or '—'}",
    ]

    if not ejecutar:
        lines.append("")
        lines.append(
            "Modo simulación. Para ejecutar en Sheets (campo cod_bodega → destino), "
            "pon chat_traslados_ejecutar = true en BD_CONFIG o CHAT_TRASLADOS_EJECUTAR=1."
        )
        return "\n".join(lines)

    headers, rows_data, header_row_idx = _cargar_mp_todas_filas()
    try:
        icod = headers.index("cod_mp_sistema")
        ibod = headers.index("cod_bodega")
    except ValueError:
        return (
            "No encuentro columnas cod_mp_sistema / cod_bodega en BD_MP_SISTEMA; "
            "no se actualizó."
        )

    row_idx = None
    for i, row in enumerate(rows_data):
        if icod < len(row) and row[icod].strip() == cod:
            row_idx = header_row_idx + i + 2
            break
    if not row_idx:
        return "No localicé la fila en Sheets."

    sh = _sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    rng = rowcol_to_a1(row_idx, ibod + 1)
    ws.update(range_name=rng, values=[[dest]])
    lines.append(f"Actualizado: {cod} ahora en bodega {dest} (celda {rng}).")
    return "\n".join(lines)


def intento_consultas_extendidas(pregunta: str) -> tuple[str | None, str | None]:
    """
    Evalúa todas las consultas extendidas en orden fijo.
    Retorna (respuesta, tipo_intento) donde tipo_intento es el id para CHAT_TIPOS o None.
    """
    # Orden: más específicas primero
    for fn, tid in (
        (consulta_traslado, "traslado_bodegas"),
        (consulta_bodega_producto, "bodega_producto"),
        (consulta_consumo_ingrediente_recetas, "consumo_ingrediente"),
        (consulta_ventas_por_plato, "ventas_por_plato"),
        (consulta_rotacion_baja, "rotacion_productos"),
        (consulta_inventario_ingrediente, "inventario_ingrediente"),
    ):
        try:
            out = fn(pregunta)
        except Exception as e:
            return (f"Error consultando datos: {e}", tid)
        if out:
            return (out, tid)
    return (None, None)
