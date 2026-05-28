import os
import argparse
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from matching_productos import cargar_bd_productos, construir_lookup, resolver_match
from supabase import create_client
from xml.etree import ElementTree as ET

load_dotenv(override=True)


def _required_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return val


SUPABASE_URL = _required_env("SUPABASE_URL")
SUPABASE_KEY = _required_env("SUPABASE_KEY")
SMART_MENU_URL = _required_env("SMART_MENU_BASE_URL")
SMART_MENU_PHPSESSID = (os.getenv("SMART_MENU_PHPSESSID") or "").strip()

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

_http = requests.Session()
_http.headers.update(
    {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
    }
)
if SMART_MENU_PHPSESSID:
    _http.cookies.set("PHPSESSID", SMART_MENU_PHPSESSID)

# ── LOOKUP GLOBAL (se carga una vez por ejecución) ────────────
_lookup_cache = None
_codbarras_by_id_cache = None


def _get_lookup() -> dict:
    global _lookup_cache
    if _lookup_cache is None:
        print("  Cargando BD_PRODUCTOS para matching...")
        productos = cargar_bd_productos()
        _lookup_cache = construir_lookup(productos)
        print(f"  {len(_lookup_cache)} productos en lookup")
    return _lookup_cache


def _get_codbarras_by_id() -> dict[str, str]:
    """
    Construye lookup: idIngrediente (idPlato) -> codbarras (Cod. Barras del reporte).
    Fuente: listaPlatos.php?sucursal=...
    """
    global _codbarras_by_id_cache
    if _codbarras_by_id_cache is not None:
        return _codbarras_by_id_cache

    sucursal = (os.getenv("SMART_MENU_SUCURSAL") or "1").strip() or "1"
    url = f"{SMART_MENU_URL}/listaPlatos.php?sucursal={sucursal}"
    print(f"  Cargando lookup idPlato->codbarras desde: {url}")

    try:
        resp = _http.get(url, timeout=25)
        resp.raise_for_status()
        rows = _parse_dhtmlx_xml(resp.text or "")
    except Exception as e:
        print(f"  WARN no se pudo cargar listaProductos.php: {e}")
        _codbarras_by_id_cache = {}
        return _codbarras_by_id_cache

    m: dict[str, str] = {}
    for r in rows:
        # Por observación: cell[0]=idIngrediente, cell[1]=codbarras
        if len(r) < 2:
            continue
        id_ing = (r[0] or "").strip()
        cb = (r[1] or "").strip()
        if id_ing and cb:
            m[id_ing] = cb

    _codbarras_by_id_cache = m
    print(f"  Lookup productos cargado: {len(m)} ids con codbarras")
    return _codbarras_by_id_cache


def _normalize_fecha_input(raw: str) -> str:
    """
    Acepta:
    - YYYY-MM-DD
    - YYYY-MM-DD HH:MM:SS
    - DD/MM/YYYY
    - DD/MM/YYYY HH:MM:SS
    Devuelve:
    - YYYY-MM-DD  (si no trae hora)
    - YYYY-MM-DD HH:MM:SS (si trae hora)
    """
    raw = (raw or "").strip()
    if not raw:
        return date.today().strftime("%Y-%m-%d")

    fmts = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            if "H" in fmt:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Si viene algo raro pero util (ej: ya viene con hora en otro formato),
    # lo dejamos pasar y que el endpoint responda.
    return raw


def _smartmenu_dt_range(fecha: str) -> tuple[str, str]:
    # Si viene con hora, usamos ese mismo timestamp como inicio/fin
    if " " in fecha:
        return (fecha, fecha)
    return (f"{fecha} 00:00:00", f"{fecha} 23:59:59")


def _parse_dhtmlx_xml(text: str) -> list[list[str]]:
    """
    Parse básico de DHTMLX Grid XML:
    <rows><row id="..."><cell>...</cell>...</row>...</rows>
    """
    root = ET.fromstring(text)
    rows = []
    for row in root.findall(".//row"):
        cells = []
        for cell in row.findall("./cell"):
            cells.append((cell.text or "").strip())
        rows.append(cells)
    return rows


def descargar_detalle_factura(id_documento: str) -> list[dict]:
    """
    Retorna lista de items (detalle) para un idDocumento.
    Endpoint observado en Smart Menu: data/detallesFactura.php (POST id=...)
    """
    url = f"{SMART_MENU_URL}/detallesFactura.php"
    try:
        resp = _http.post(url, data={"id": str(id_documento)}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ERROR obteniendo detalle (idDocumento={id_documento}): {e}")
        return []

    if not isinstance(data, list):
        print(f"  WARN detalle no es lista (idDocumento={id_documento})")
        return []
    return data


def _safe_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _parse_descuento_valor(raw: str) -> float:
    # A veces viene como "0000000000000000000000"
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if not s:
        return 0.0
    if re.fullmatch(r"0+", s):
        return 0.0
    return _safe_float(s)


def _estado_documento_desde_texto_grid(raw: str) -> str:
    """
    Interpreta la celda de estado del grid (típicamente col 9 en comprasloadVentas.php).
    - ANULADO: no cuenta en netos / no descarga inventario.
    - NO_AUTORIZADO: en Smart Menu suele ser nota de venta (efectivo, sin factura electrónica);
      **sí** cuenta en netos, cuadre y descargo de inventario (hubo cobro y salida de producto).
    """
    u = (raw or "").strip().upper()
    if "ANULADO" in u:
        return "ANULADO"
    # "NO AUTORIZADO", "NO AUTORIZADA", sin tilde, etc.
    if "NO AUTORIZ" in u:
        return "NO_AUTORIZADO"
    return "ACTIVO"


def estado_documento_excluye_neto_operativo(estado: str | None) -> bool:
    """True solo para ANULADO: no suma en netos ni descarga inventario."""
    return (estado or "ACTIVO").strip().upper() == "ANULADO"


def _venta_header_from_row(row: list[str]) -> dict:
    # Indices observados en comprasloadVentas.php (DHTMLX grid)
    id_documento = (row[0] if len(row) > 0 else "").strip()
    num_documento = (row[1] if len(row) > 1 else "").strip()
    tipo_documento = (row[2] if len(row) > 2 else "").strip()
    fecha_hora = (row[3] if len(row) > 3 else "").strip()
    total_doc = (row[7] if len(row) > 7 else "").strip()
    info_pago = (row[30] if len(row) > 30 else "").strip()
    # Col 9: vacío o "0" = venta activa; "ANULADO" / "NO AUTORIZADO" según Smart Menu.
    raw_estado_venta = (row[9] if len(row) > 9 else "").strip()
    detalle_anulacion = (row[11] if len(row) > 11 else "").strip()
    if not detalle_anulacion and len(row) > 24:
        detalle_anulacion = (row[24] or "").strip()
    estado_documento = _estado_documento_desde_texto_grid(raw_estado_venta)

    fecha_part = fecha_hora[:10] if len(fecha_hora) >= 10 else ""
    hora_part = fecha_hora[11:16] if len(fecha_hora) >= 16 else None
    if fecha_part:
        fecha_obj = datetime.strptime(fecha_part, "%Y-%m-%d")
        periodo_semana = fecha_obj.strftime("%G-W%V")
        periodo_mes = fecha_obj.strftime("%Y-%m")
    else:
        periodo_semana = ""
        periodo_mes = ""

    return {
        "id_documento": id_documento,
        "num_documento": num_documento,
        "tipo_documento": tipo_documento or "VENTA",
        "fecha": fecha_part,
        "hora": hora_part,
        "periodo_semana": periodo_semana,
        "periodo_mes": periodo_mes,
        "total_documento": _safe_float(total_doc),
        "forma_pago": info_pago,
        "estado_documento": estado_documento,
        "detalle_anulacion": detalle_anulacion or None,
    }


def construir_lineas_hist_ventas(header: dict, detalles: list[dict]) -> list[dict]:
    fecha_part = header.get("fecha") or ""
    fecha_tag = fecha_part.replace("-", "") if fecha_part else "00000000"
    lookup = _get_lookup()
    codbarras_by_id = _get_codbarras_by_id()
    lineas = []

    for det in detalles:
        det_id = str(det.get("id", "")).strip()
        idplato = det.get("idPlato")
        cantidad = _safe_float(det.get("cantidad", 0))
        precio_u = _safe_float(det.get("precioUnitario", 0))
        subtotal = cantidad * precio_u
        total = _safe_float(det.get("total", subtotal))
        detalle_plato = (det.get("detallePlato") or "").strip()
        descuento = _parse_descuento_valor(det.get("descuentoValor"))

        # Matching
        idplato_str = str(idplato).strip() if idplato is not None else ""
        codbarras = codbarras_by_id.get(idplato_str) if idplato_str else None
        # El cod_smart_menu real (Sheets) es el codbarras; idPlato es ID interno.
        match = resolver_match(str(codbarras or ""), detalle_plato, lookup)

        cod_venta = f"VTA-{fecha_tag}-{header.get('id_documento','')}-{det_id}"
        lineas.append(
            {
                "cod_venta": cod_venta,
                "num_documento": header.get("num_documento", ""),
                "tipo_documento": header.get("tipo_documento", "VENTA"),
                "fecha": header.get("fecha", ""),
                "hora": header.get("hora"),
                "periodo_semana": header.get("periodo_semana", ""),
                "periodo_mes": header.get("periodo_mes", ""),
                "idplato_sm": idplato,
                "cod_smart_menu": str(codbarras or ""),
                "variedad_smart_menu": match["variedad_matched"] or detalle_plato or None,
                "cod_producto": match["cod_producto"],
                "nombre_producto": match["nombre_producto"],
                "cod_receta": match.get("cod_receta"),
                "cantidad_vendida": cantidad,
                "precio_unitario": precio_u,
                "subtotal": subtotal,
                "descuento_valor": descuento,
                "total": total,
                "bodega_origen": None,
                "mesa": None,
                "forma_pago": header.get("forma_pago", ""),
                "propina": 0.0,
                "estado_match": match["estado_match"],
                "estado_documento": header.get("estado_documento") or "ACTIVO",
                "detalle_anulacion": header.get("detalle_anulacion"),
            }
        )
    return lineas


# ── DESCARGA VENTAS DESDE SMART MENU (DHTMLX XML) ─────────────
def descargar_ventas_grid(fecha: str) -> list[list[str]]:
    """
    Grid comprasloadVentas.php: incluye ventas activas, anuladas y notas (NO AUTORIZADO en UI) en la misma respuesta.
    Columna 9: ANULADO, NO AUTORIZADO, etc. (ver _estado_documento_desde_texto_grid).
    """
    fecha_inicial, fecha_final = _smartmenu_dt_range(fecha)
    url = f"{SMART_MENU_URL}/comprasloadVentas.php"
    suc = os.getenv("SMART_MENU_SUCURSAL", "1")
    caj = os.getenv("SMART_MENU_CAJA", "1")
    params = {
        "fechaInicial": fecha_inicial,
        "fechaFinal": fecha_final,
        "tipo": "0",
        "campo": "",
        "valor": "",
        "sucursal": suc,
        "caja": caj,
        "empleado": os.getenv("SMART_MENU_EMPLEADO", "0"),
        "tipopago": os.getenv("SMART_MENU_TIPOPAGO", "-1"),
        "tipodoc": os.getenv("SMART_MENU_TIPODOC", "-1"),
        "tipoaut": os.getenv("SMART_MENU_TIPOAUT", "T"),
        "estadoEnvio": os.getenv("SMART_MENU_ESTADO_ENVIO", "0"),
        # el parámetro dhxr* no es necesario desde requests
    }

    print(f"  Consultando: {url}")
    print(
        f"  Params (debe coincidir con VENTAS TOTALES en Smart Menu): "
        f"sucursal={suc} | caja={caj} | rango={fecha_inicial} .. {fecha_final}"
    )
    try:
        resp = _http.get(url, params=params, timeout=20)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        print("  WARN: Smart Menu no accesible (servidor apagado o fuera de red)")
        return []
    except requests.exceptions.Timeout:
        print("  WARN: Timeout consultando Smart Menu")
        return []
    except Exception as e:
        print(f"  ERROR consultando Smart Menu: {e}")
        return []

    ct = (resp.headers.get("content-type") or "").lower()
    body = resp.text or ""
    if "xml" not in ct and not body.lstrip().startswith("<"):
        snippet = body[:400].replace("\r", " ").replace("\n", " ")
        print(f"  INFO: content-type={ct}")
        print(f"  INFO: Body snippet: {snippet}")
        return []

    try:
        rows = _parse_dhtmlx_xml(body)
    except Exception as e:
        snippet = body[:400].replace("\r", " ").replace("\n", " ")
        print(f"  ERROR parseando XML: {e}")
        print(f"  INFO: Body snippet: {snippet}")
        return []

    return rows


# ── DESCARGA VENTAS DESDE SMART MENU ─────────────────────────
def descargar_ventas(fecha: str, filtro: str = "0") -> list:
    """
    Endpoint legacy (documentosEmitidos JSON). No usa el pipeline ni pipeline_diario.

    fecha: YYYY-MM-DD. Retorna líneas sin matching ni estado_documento.
    cod_venta incluye timestamp — no es determinístico; re-ejecutar crea filas nuevas.
    Flujo de producción: descargar_ventas_grid + construir_lineas_hist_ventas.
    """
    fecha_param = quote(fecha, safe="")
    filtro_param = quote(str(filtro), safe="")
    url = f"{SMART_MENU_URL}/documentosEmitidos/{fecha_param}/{fecha_param}/{filtro_param}"
    print(f"  Consultando: {url}")
    try:
        resp = _http.get(url, timeout=15)
        resp.raise_for_status()
        resp_text = resp.text
        data = resp.json()
    except requests.exceptions.ConnectionError:
        print("  WARN: Smart Menu no accesible (servidor apagado o fuera de red)")
        return []
    except requests.exceptions.Timeout:
        print("  WARN: Timeout consultando Smart Menu")
        return []
    except Exception as e:
        print(f"  ERROR consultando Smart Menu: {e}")
        return []

    lineas = []
    if isinstance(data, list):
        documentos = data
    else:
        # Variantes observadas en Smart Menu:
        # - {"documentos": [...]}
        # - {"documentoList": [...]}
        documentos = data.get("documentos") or data.get("documentoList") or []
        if not documentos:
            print(f"  INFO: Respuesta keys={list(data.keys())[:10]}")
            snippet = (resp_text or "")[:400].replace("\r", " ").replace("\n", " ")
            if snippet:
                print(f"  INFO: Body snippet: {snippet}")

    for doc in documentos:
        fecha_doc = doc.get("documentoFecha", "")
        fecha_part = fecha_doc[:10] if fecha_doc else fecha
        hora_part = fecha_doc[11:16] if len(fecha_doc) > 10 else None

        # Calcula periodo
        fecha_obj = datetime.strptime(fecha_part, "%Y-%m-%d")
        periodo_semana = fecha_obj.strftime("%G-W%V")
        periodo_mes = fecha_obj.strftime("%Y-%m")

        for detalle in doc.get("documentoDetalleList", []):
            cod_sm = str(detalle.get("codigo", "")).strip()
            variedad = str(detalle.get("detallePlato", "")).strip()
            idplato = detalle.get("idplato")
            cantidad = detalle.get("cantidad", 0)
            precio_u = detalle.get("precioUnitario", 0)
            subtotal = detalle.get("subtotal", 0)
            descuento = detalle.get("descuentoValor", 0)
            total = detalle.get("total", subtotal)
            bodega = detalle.get("bodega")

            # Genera cod_venta único
            ts = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
            cod_venta = f"VTA-{fecha_part.replace('-','')}-{cod_sm}-{ts}"

            lineas.append(
                {
                    "cod_venta": cod_venta,
                    "num_documento": str(doc.get("documentoNumero", "")),
                    "tipo_documento": doc.get("documentoTipo", "TICKET"),
                    "fecha": fecha_part,
                    "hora": hora_part,
                    "periodo_semana": periodo_semana,
                    "periodo_mes": periodo_mes,
                    "idplato_sm": idplato,
                    "cod_smart_menu": cod_sm,
                    "variedad_smart_menu": variedad if variedad else None,
                    "cod_producto": None,
                    "nombre_producto": None,
                    "cantidad_vendida": cantidad,
                    "precio_unitario": precio_u,
                    "subtotal": subtotal,
                    "descuento_valor": descuento,
                    "total": total,
                    "bodega_origen": bodega,
                    "mesa": doc.get("documentoMesa"),
                    "forma_pago": _forma_pago(doc),
                    "propina": _propina(doc),
                    "estado_match": "PENDIENTE_MATCH",
                }
            )

    return lineas


def _forma_pago(doc: dict) -> str:
    pagos = doc.get("documentoTipoPagoList", [])
    if pagos:
        return pagos[0].get("formaPagoNombre", "") or ""
    return ""


def _propina(doc: dict) -> float:
    return float(doc.get("propinaEfectivo", 0) or 0) + float(doc.get("propinaTarjeta", 0) or 0)


# ── GUARDA EN SUPABASE ────────────────────────────────────────
def _es_error_duplicado(exc: Exception) -> bool:
    msg = str(exc)
    return "duplicate" in msg.lower() or "23505" in msg


def _guardar_ventas_fila_a_fila(lineas: list) -> dict:
    insertadas = duplicadas = errores = 0
    for linea in lineas:
        try:
            supabase.table("hist_ventas").insert(linea).execute()
            insertadas += 1
        except Exception as e:
            if _es_error_duplicado(e):
                duplicadas += 1
            else:
                errores += 1
                print(f"  ERROR insertando {linea.get('cod_venta', '?')}: {e}")
    return {"insertadas": insertadas, "duplicadas": duplicadas, "errores": errores}


def guardar_ventas(lineas: list) -> dict:
    if not lineas:
        return {"insertadas": 0, "duplicadas": 0, "errores": 0}

    try:
        supabase.table("hist_ventas").insert(lineas).execute()
        return {"insertadas": len(lineas), "duplicadas": 0, "errores": 0}
    except Exception as e:
        if _es_error_duplicado(e):
            return _guardar_ventas_fila_a_fila(lineas)
        print(f"  ERROR insert batch ({len(lineas)} lineas): {e}")
        return {"insertadas": 0, "duplicadas": 0, "errores": len(lineas)}


_hist_ventas_tiene_estado_documento: bool | None = None


def _hist_ventas_columna_estado_disponible() -> bool:
    """Cache: existe columna estado_documento en hist_ventas (tras migración SQL)."""
    global _hist_ventas_tiene_estado_documento
    if _hist_ventas_tiene_estado_documento is not None:
        return _hist_ventas_tiene_estado_documento
    try:
        supabase.table("hist_ventas").select("estado_documento").limit(1).execute()
        _hist_ventas_tiene_estado_documento = True
    except Exception:
        _hist_ventas_tiene_estado_documento = False
    return _hist_ventas_tiene_estado_documento


def auditar_hist_ventas_dia(fecha: str) -> dict:
    """
    Lee hist_ventas en Supabase para la fecha y devuelve totales (cuadre post-carga).
    Si existe estado_documento: netos excluyen solo ANULADO (NO_AUTORIZADO cuenta como venta operativa).
    """
    fecha = (fecha or "").strip().split()[0]
    con_estado = _hist_ventas_columna_estado_disponible()
    sel = (
        "subtotal,descuento_valor,total,estado_documento"
        if con_estado
        else "subtotal,descuento_valor,total"
    )
    out = {
        "lineas": 0,
        "sum_subtotal": 0.0,
        "sum_desc": 0.0,
        "sum_total": 0.0,
        "lineas_anuladas": 0,
        "sum_subtotal_neto": 0.0,
        "sum_desc_neto": 0.0,
        "sum_total_neto": 0.0,
        "columna_estado_ok": con_estado,
    }
    offset = 0
    while True:
        r = (
            supabase.table("hist_ventas")
            .select(sel)
            .eq("fecha", fecha)
            .range(offset, offset + 999)
            .execute()
        )
        chunk = r.data or []
        for row in chunk:
            es_excl_neto = False
            if con_estado:
                es_excl_neto = estado_documento_excluye_neto_operativo(
                    row.get("estado_documento")
                )
            out["sum_subtotal"] += float(row.get("subtotal") or 0)
            out["sum_desc"] += float(row.get("descuento_valor") or 0)
            out["sum_total"] += float(row.get("total") or 0)
            if not es_excl_neto:
                out["sum_subtotal_neto"] += float(row.get("subtotal") or 0)
                out["sum_desc_neto"] += float(row.get("descuento_valor") or 0)
                out["sum_total_neto"] += float(row.get("total") or 0)
            else:
                out["lineas_anuladas"] += 1
        out["lineas"] += len(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    if not con_estado:
        out["sum_subtotal_neto"] = out["sum_subtotal"]
        out["sum_desc_neto"] = out["sum_desc"]
        out["sum_total_neto"] = out["sum_total"]
    return out


def borrar_hist_ventas_dia(fecha: str) -> int:
    """
    Borra TODAS las filas de hist_ventas para una fecha (YYYY-MM-DD).
    Útil para "refrescar" el día desde Smart Menu.
    """
    try:
        res = (
            supabase.table("hist_ventas")
            .delete()
            .eq("fecha", fecha)
            .execute()
        )
        # supabase-py no siempre expone count; devolvemos len(data) como aproximación.
        return len(res.data or [])
    except Exception as e:
        print(f"  ERROR borrando hist_ventas fecha={fecha}: {e}")
        return 0


def procesar_un_dia(fecha: str, reemplazar: bool = False) -> dict:
    fecha = _normalize_fecha_input(fecha)
    if " " in fecha:
        fecha = fecha.split(" ", 1)[0]

    print(f"\n{'=' * 50}")
    print(f"MODULO VENTAS — {fecha}")
    print(f"{'=' * 50}")

    if reemplazar:
        print("\n[0] Reemplazo activado: borrando hist_ventas del día...")
        borradas = borrar_hist_ventas_dia(fecha)
        print(f"  -> filas borradas (aprox): {borradas}")

    print("\n[1] Descargando cabeceras de ventas (Smart Menu)...")
    rows = descargar_ventas_grid(fecha)
    n_grid_total = len(rows)
    print(f"  -> {n_grid_total} filas encontradas (grid XML)")
    try:
        from ventas_smartmenu_total import calcular_total_smartmenu

        tg = calcular_total_smartmenu(fecha, sin_iva=True)
        if (tg.get("total_descuentos") or 0) > 0:
            print(
                f"  Grid oficial: brutas ${tg['total_bruto']:.2f} - desc. ${tg['total_descuentos']:.2f} "
                f"= netas ${tg['total']:.2f} ({tg['docs']} tickets)"
            )
        else:
            print(f"  Grid oficial netas: ${tg['total']:.2f} ({tg['docs']} tickets)")
    except Exception as e:
        print(f"  WARN: no se pudo calcular total grid: {e}")

    if not rows:
        print("\n  Sin datos para procesar.")
        print(f"\n{'=' * 50}")
        return {"insertadas": 0, "duplicadas": 0, "errores": 0, "docs": 0}

    _max_docs_env = os.getenv("SMART_MENU_MAX_DOCS")
    max_docs = int(_max_docs_env) if _max_docs_env else 999999
    rows = rows[:max_docs]

    print("\n[2] Descargando detalle por venta y guardando en Supabase...")
    insertadas = duplicadas = errores = 0
    headers_parsed = [_venta_header_from_row(row) for row in rows]
    docs_anulados = 0
    docs_no_aut = 0

    for idx, (row, header) in enumerate(zip(rows, headers_parsed), start=1):
        estado = header.get("estado_documento")
        if estado == "ANULADO":
            docs_anulados += 1
        elif estado == "NO_AUTORIZADO":
            docs_no_aut += 1

        id_doc = header.get("id_documento") or ""
        if not id_doc:
            print(f"  WARN fila sin idDocumento (idx={idx})")
            continue

        detalles = descargar_detalle_factura(id_doc)
        lineas = construir_lineas_hist_ventas(header, detalles)

        res = guardar_ventas(lineas)
        insertadas += res["insertadas"]
        duplicadas += res["duplicadas"]
        errores += res["errores"]

        print(
            f"  doc {idx}/{len(rows)} idDocumento={id_doc} items={len(lineas)} "
            f"(ins={res['insertadas']} dup={res['duplicadas']} err={res['errores']})"
        )
    print("\nResumen:")
    print(f"  Documentos:  {len(rows)}")
    if docs_anulados:
        print(f"  Anulados (grid): {docs_anulados} (hist_ventas estado_documento=ANULADO)")
    if docs_no_aut:
        print(
            f"  Notas / sin factura (grid): {docs_no_aut} "
            "(estado_documento=NO_AUTORIZADO; cuentan en neto y descargo de inventario)"
        )
    print(f"  Insertadas:  {insertadas}")
    print(f"  Duplicadas:  {duplicadas}")
    print(f"  Errores:     {errores}")

    audit = auditar_hist_ventas_dia(fecha)
    print(f"\n[AUDITORIA hist_ventas en Supabase — fecha {fecha}]")
    print(f"  Lineas en tabla:     {audit['lineas']}")
    print(f"  Suma subtotal:       {audit['sum_subtotal']:.2f}")
    print(f"  Suma descuentos:     {audit['sum_desc']:.2f}")
    print(f"  Suma total (lineas): {audit['sum_total']:.2f}")
    if audit.get("columna_estado_ok"):
        print(
            f"  (Neto operativo) Subtotal: {audit['sum_subtotal_neto']:.2f} | "
            f"Total: {audit['sum_total_neto']:.2f} | "
            f"Lineas anuladas (excl. neto): {audit['lineas_anuladas']}"
        )
    elif not audit.get("columna_estado_ok"):
        print(
            "  INFO: columna estado_documento no existe en hist_ventas; "
            "ejecuta sql/add_hist_ventas_estado_documento.sql en Supabase para cuadre neto/anulados."
        )
    if errores > 0:
        print(
            "  WARN: hubo errores al guardar lineas; revisar logs arriba "
            "y considerar --reemplazar si los datos quedaron incompletos."
        )
    if len(rows) > 0 and audit["lineas"] == 0:
        print(
            "  WARN: se procesaron documentos del grid pero no hay filas en hist_ventas; "
            "revisar Supabase, permisos o consistencia de fecha."
        )
    max_docs_used = int(os.getenv("SMART_MENU_MAX_DOCS") or 0)
    if max_docs_used and n_grid_total >= max_docs_used:
        print(
            f"  WARN: SMART_MENU_MAX_DOCS={max_docs_used} limita documentos "
            f"(grid tenia {n_grid_total}); puede haber ventas sin cargar."
        )

    print(f"\n{'=' * 50}")
    return {
        "insertadas": insertadas,
        "duplicadas": duplicadas,
        "errores": errores,
        "docs": len(rows),
        "audit": audit,
        "n_grid_total": n_grid_total,
    }


def _main_un_dia():
    raw = input("Fecha a procesar (YYYY-MM-DD) [Enter = hoy]: ").strip()
    fecha = raw or date.today().strftime("%Y-%m-%d")
    procesar_un_dia(fecha)


def _main_carga_historica(
    fecha_inicio: str | None = None, fecha_fin: str | None = None
):
    if fecha_inicio is None:
        fecha_inicio = input(
            "Fecha inicio (YYYY-MM-DD) [Enter = hoy]: "
        ).strip()
    if fecha_fin is None:
        fecha_fin = input(
            "Fecha fin    (YYYY-MM-DD) [Enter = misma fecha inicio]: "
        ).strip()

    if not fecha_inicio:
        fecha_inicio = date.today().strftime("%Y-%m-%d")
    if not fecha_fin:
        fecha_fin = fecha_inicio

    try:
        d_inicio = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
        d_fin = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
    except ValueError as e:
        print(f"ERROR: fecha invalida ({e})")
        return

    if d_fin < d_inicio:
        d_inicio, d_fin = d_fin, d_inicio
        fecha_inicio, fecha_fin = d_inicio.strftime("%Y-%m-%d"), d_fin.strftime(
            "%Y-%m-%d"
        )

    fechas = []
    d = d_inicio
    while d <= d_fin:
        fechas.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    print(f"\n{'=' * 50}")
    print(
        f"CARGA HISTORICA: {fecha_inicio} -> {fecha_fin} ({len(fechas)} dias)"
    )
    print(f"{'=' * 50}")

    _max_docs_env = os.getenv("SMART_MENU_MAX_DOCS")
    max_docs = int(_max_docs_env) if _max_docs_env else 999999
    total_ins = total_dup = total_err = 0

    for fecha in fechas:
        print(f"\n--- {fecha} ---")
        rows = descargar_ventas_grid(fecha)
        rows = rows[:max_docs]
        print(f"  {len(rows)} documentos")

        if not rows:
            time.sleep(0.5)
            continue

        insertadas = duplicadas = errores = 0
        for idx, row in enumerate(rows, start=1):
            header = _venta_header_from_row(row)
            id_doc = header.get("id_documento", "")
            if not id_doc:
                continue
            detalles = descargar_detalle_factura(id_doc)
            lineas = construir_lineas_hist_ventas(header, detalles)
            res = guardar_ventas(lineas)
            insertadas += res["insertadas"]
            duplicadas += res["duplicadas"]
            errores += res["errores"]

        print(f"  ins={insertadas} dup={duplicadas} err={errores}")
        total_ins += insertadas
        total_dup += duplicadas
        total_err += errores
        time.sleep(0.5)

    print(f"\n{'=' * 50}")
    print("RESUMEN TOTAL")
    print(f"  Insertadas:  {total_ins}")
    print(f"  Duplicadas:  {total_dup}")
    print(f"  Errores:     {total_err}")
    print(f"{'=' * 50}")


# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    # CLI moderno (no-interactivo)
    _cli_flags = ("--fecha", "--reemplazar", "--historico", "--strict", "--audit")
    if any(a in _cli_flags for a in sys.argv[1:]):
        p = argparse.ArgumentParser(description="Carga ventas Smart Menu -> hist_ventas")
        p.add_argument("--fecha", required=False, help="YYYY-MM-DD (default: hoy)")
        p.add_argument(
            "--reemplazar",
            action="store_true",
            help="Borra hist_ventas del día antes de reimportar",
        )
        p.add_argument(
            "--strict",
            action="store_true",
            help="Sale con codigo 1 si hay errores de insert o BD vacia con docs en grid",
        )
        p.add_argument(
            "--audit",
            metavar="FECHA",
            help="Solo lee hist_ventas en Supabase para YYYY-MM-DD (sin Smart Menu)",
        )
        p.add_argument(
            "--historico",
            nargs=2,
            metavar=("FECHA_INI", "FECHA_FIN"),
            help="Carga histórica por rango YYYY-MM-DD YYYY-MM-DD",
        )
        a = p.parse_args()

        if a.audit:
            f = _normalize_fecha_input(a.audit)
            ad = auditar_hist_ventas_dia(f)
            print(f"\n[AUDITORIA hist_ventas — Supabase — fecha {f}]")
            print(f"  Lineas:          {ad['lineas']}")
            print(f"  Suma subtotal:   {ad['sum_subtotal']:.2f}")
            print(f"  Suma descuentos: {ad['sum_desc']:.2f}")
            print(f"  Suma total:      {ad['sum_total']:.2f}")
            if ad.get("columna_estado_ok"):
                print(
                    f"  Neto (sin anulados): subtotal {ad['sum_subtotal_neto']:.2f} | "
                    f"total {ad['sum_total_neto']:.2f} | lineas anuladas {ad['lineas_anuladas']}"
                )
            print()
        elif a.historico:
            _main_carga_historica(fecha_inicio=a.historico[0], fecha_fin=a.historico[1])
        else:
            res = procesar_un_dia(
                a.fecha or date.today().strftime("%Y-%m-%d"),
                reemplazar=a.reemplazar,
            )
            if a.strict:
                if res.get("errores", 0) > 0:
                    sys.exit(1)
                audit = res.get("audit") or {}
                if res.get("docs", 0) > 0 and audit.get("lineas", 0) == 0:
                    sys.exit(1)
    else:
        # Modo legacy (interactivo)
        if len(sys.argv) > 1 and sys.argv[1] in ("--historico", "-H", "historico"):
            fi = sys.argv[2] if len(sys.argv) > 2 else None
            ff = sys.argv[3] if len(sys.argv) > 3 else None
            _main_carga_historica(fecha_inicio=fi, fecha_fin=ff)
        else:
            _main_un_dia()

