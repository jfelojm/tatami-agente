import os
import re
from datetime import date, datetime
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


def _venta_header_from_row(row: list[str]) -> dict:
    # Indices observados en comprasloadVentas.php (DHTMLX grid)
    id_documento = (row[0] if len(row) > 0 else "").strip()
    num_documento = (row[1] if len(row) > 1 else "").strip()
    tipo_documento = (row[2] if len(row) > 2 else "").strip()
    fecha_hora = (row[3] if len(row) > 3 else "").strip()
    total_doc = (row[7] if len(row) > 7 else "").strip()
    info_pago = (row[30] if len(row) > 30 else "").strip()

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
            }
        )
    return lineas


# ── DESCARGA VENTAS DESDE SMART MENU (DHTMLX XML) ─────────────
def descargar_ventas_grid(fecha: str) -> list[list[str]]:
    fecha_inicial, fecha_final = _smartmenu_dt_range(fecha)
    url = f"{SMART_MENU_URL}/comprasloadVentas.php"
    params = {
        "fechaInicial": fecha_inicial,
        "fechaFinal": fecha_final,
        "tipo": "0",
        "campo": "",
        "valor": "",
        "sucursal": os.getenv("SMART_MENU_SUCURSAL", "1"),
        "caja": os.getenv("SMART_MENU_CAJA", "1"),
        "empleado": os.getenv("SMART_MENU_EMPLEADO", "0"),
        "tipopago": os.getenv("SMART_MENU_TIPOPAGO", "-1"),
        "tipodoc": os.getenv("SMART_MENU_TIPODOC", "-1"),
        "tipoaut": os.getenv("SMART_MENU_TIPOAUT", "T"),
        "estadoEnvio": os.getenv("SMART_MENU_ESTADO_ENVIO", "0"),
        # el parámetro dhxr* no es necesario desde requests
    }

    print(f"  Consultando: {url}")
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
    fecha: string formato YYYY-MM-DD
    Retorna lista de líneas de venta parseadas
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
def guardar_ventas(lineas: list) -> dict:
    if not lineas:
        return {"insertadas": 0, "duplicadas": 0, "errores": 0}

    insertadas = 0
    duplicadas = 0
    errores = 0

    for linea in lineas:
        try:
            supabase.table("hist_ventas").insert(linea).execute()
            insertadas += 1
        except Exception as e:
            msg = str(e)
            if "duplicate" in msg.lower() or "23505" in msg:
                duplicadas += 1
            else:
                errores += 1
                print(f"  ERROR insertando {linea['cod_venta']}: {msg}")

    return {"insertadas": insertadas, "duplicadas": duplicadas, "errores": errores}


# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    fecha = _normalize_fecha_input(input("Fecha a procesar (YYYY-MM-DD) [Enter = hoy]: ").strip())

    print(f"\n{'=' * 50}")
    print(f"MODULO VENTAS — {fecha}")
    print(f"{'=' * 50}")

    print("\n[1] Descargando cabeceras de ventas (Smart Menu)...")
    rows = descargar_ventas_grid(fecha)
    print(f"  -> {len(rows)} filas encontradas (grid XML)")

    if not rows:
        print("\n  Sin datos para procesar.")
        print(f"\n{'=' * 50}")
        raise SystemExit(0)

    max_docs = int(os.getenv("SMART_MENU_MAX_DOCS", "50") or "50")
    rows = rows[:max_docs]

    print("\n[2] Descargando detalle por venta y guardando en Supabase...")
    insertadas = duplicadas = errores = 0

    for idx, row in enumerate(rows, start=1):
        header = _venta_header_from_row(row)
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
    print(f"  Insertadas:  {insertadas}")
    print(f"  Duplicadas:  {duplicadas}")
    print(f"  Errores:     {errores}")

    print(f"\n{'=' * 50}")

