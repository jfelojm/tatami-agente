import os
import re
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

try:
    from alertas_tatami import enviar_whatsapp_texto as _enviar_whatsapp_texto

    _alertas_disponibles = True
except ImportError:
    _alertas_disponibles = False
    _enviar_whatsapp_texto = None

from codigo_factura_match import (
    cod_item_prov_para_catalogo,
    cod_proveedores_strip_sufijo_desde_bd_prov,
    normalizar_cod_item_para_match,
    normalizar_cod_proveedor_para_match,
)
from config_sheets import cfg

load_dotenv(override=True)

# Hoja maestra: ítems de factura XML sin match en BD_ITEMS_PROV (para alta manual / MP).
BD_ITEMS_PENDIENTES_SHEET = "BD_ITEMS_PENDIENTES"
FECHA_MIN_COSTO_TOTAL_PENDIENTES = "2026-05-21"
# No registrar mov_inventario ni stock Sheets desde facturas con fecha anterior a este día.
FECHA_MIN_INGRESO_FACTURA = (
    os.getenv("TATAMI_FECHA_MIN_INGRESO_FACTURA", "2026-05-29").strip()[:10]
)
COL_COSTO_TOTAL_PENDIENTES = "costo_total_xml"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── GOOGLE DRIVE / SHEETS (singleton por proceso) ─────────────
_drive_service_cache = None
_sheet_cache = None


def _get_drive_service():
    global _drive_service_cache
    if _drive_service_cache is None:
        creds = Credentials.from_service_account_file(
            os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
        )
        _drive_service_cache = build("drive", "v3", credentials=creds)
    return _drive_service_cache


def _get_sheet():
    global _sheet_cache
    if _sheet_cache is None:
        creds = Credentials.from_service_account_file(
            os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
        )
        _sheet_cache = gspread.Client(auth=creds).open_by_key(os.getenv("SPREADSHEET_ID"))
    return _sheet_cache


def _es_xml_factura_en_drive(f: dict) -> bool:
    """True si el archivo parece XML de factura (nombre .xml y MIME compatible con Drive)."""
    name = (f.get("name") or "").strip()
    mime = (f.get("mimeType") or "").strip().lower()
    if not name.lower().endswith(".xml"):
        return False
    if mime == "application/vnd.google-apps.folder":
        return False
    if mime.startswith("application/vnd.google-apps."):
        return False
    # Subidas desde Windows / correo suelen venir como octeto binario aunque sea XML válido.
    if mime in ("application/octet-stream", "binary/octet-stream"):
        return True
    if "xml" in mime or mime.startswith("text/"):
        return True
    return False


def listar_xmls_pendientes() -> list[dict]:
    """
    Lista archivos .xml en la carpeta de Drive configurada en GOOGLE_DRIVE_FACTURAS_FOLDER_ID.

    Incluye MIME application/octet-stream (Drive no siempre marca text/xml / application/xml).
    Pagina resultados (carpetas con muchas facturas).
    """
    service = _get_drive_service()
    folder_id = (os.getenv("GOOGLE_DRIVE_FACTURAS_FOLDER_ID") or "").strip()
    if not folder_id:
        return []

    q_folder = f"'{folder_id}' in parents and trashed=false"
    out: list[dict] = []
    page_token: str | None = None
    while True:
        kwargs: dict = {
            "q": q_folder,
            "fields": "nextPageToken, files(id,name,createdTime,mimeType)",
            "pageSize": 1000,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        results = service.files().list(**kwargs).execute()
        for f in results.get("files", []):
            if not _es_xml_factura_en_drive(f):
                continue
            out.append(
                {
                    "id": f["id"],
                    "name": f["name"],
                    "createdTime": f.get("createdTime"),
                    "mimeType": f.get("mimeType"),
                }
            )
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return out


def descargar_xml(file_id: str) -> str:
    service = _get_drive_service()
    content = service.files().get_media(fileId=file_id).execute()
    return content.decode("utf-8", errors="replace")


# ── PARSER XML SRI ────────────────────────────────────────────
def parsear_xml_sri(texto: str) -> dict | None:
    """
    Parsea XML SRI Ecuador (formato con CDATA anidado).
    Retorna dict con cabecera + lista de ítems.
    """
    try:
        root = ET.fromstring(texto)
    except ET.ParseError as e:
        print(f"  ERROR parseando XML externo: {e}")
        return None

    comprobante_el = root.find(".//comprobante")
    if comprobante_el is None or not comprobante_el.text:
        print("  ERROR: no se encontró <comprobante>")
        return None

    cdata = comprobante_el.text.strip()
    try:
        factura = ET.fromstring(cdata)
    except ET.ParseError as e:
        print(f"  ERROR parseando CDATA: {e}")
        return None

    it = factura.find(".//infoTributaria")
    if it is None:
        print("  ERROR: no se encontró <infoTributaria>")
        return None

    razon_social = _txt(it, "razonSocial")
    ruc = _txt(it, "ruc")
    estab = _txt(it, "estab")
    pto_emi = _txt(it, "ptoEmi")
    secuencial = _txt(it, "secuencial")
    num_factura = f"{estab}-{pto_emi}-{secuencial}"

    inf = factura.find(".//infoFactura")
    fecha_emision = _txt(inf, "fechaEmision") if inf is not None else ""
    total_sin_imp = _safe_float(
        _txt(inf, "totalSinImpuestos") if inf is not None else "0"
    )
    forma_pago_codigo = ""
    if inf is not None:
        pago = inf.find(".//pago")
        if pago is not None:
            forma_pago_codigo = _txt(pago, "formaPago")

    fecha_iso = _fecha_a_iso(fecha_emision)
    num_autorizacion = _txt(root, "numeroAutorizacion")

    items = []
    for detalle in factura.findall(".//detalle"):
        cod_principal = _txt(detalle, "codigoPrincipal").strip()
        descripcion = _txt(detalle, "descripcion").strip()
        cantidad = _safe_float(_txt(detalle, "cantidad"))
        precio_u = _safe_float(_txt(detalle, "precioUnitario"))
        total_sin_imp_item = _safe_float(_txt(detalle, "precioTotalSinImpuesto"))
        descuento = _safe_float(_txt(detalle, "descuento"))

        costo_efectivo = (
            round(total_sin_imp_item / cantidad, 6) if cantidad else 0.0
        )

        items.append(
            {
                "cod_item_xml": cod_principal,
                "descripcion_proveedor": descripcion,
                "cantidad": cantidad,
                "precio_unitario_xml": precio_u,
                "descuento": descuento,
                "precio_total_sin_impuesto": total_sin_imp_item,
                "costo_efectivo": costo_efectivo,
            }
        )

    return {
        "razon_social": razon_social,
        "ruc": ruc,
        "num_factura": num_factura,
        "num_autorizacion": num_autorizacion,
        "fecha_factura": fecha_iso,
        "total_sin_impuesto": total_sin_imp,
        "forma_pago": forma_pago_codigo,
        "items": items,
    }


# ── MATCHING FACTURA → BD_ITEMS_PROV ─────────────────────────
_items_prov_cache = None

# Una sola lectura de BD_ITEMS_PROV por recarga de cache: evita 429 en _actualizar_precio_ref.
_bd_items_prov_price_layout: dict | None = None


def _invalidar_cache_layout_precio_items_prov() -> None:
    global _bd_items_prov_price_layout
    _bd_items_prov_price_layout = None


def _clave_precio_items_prov(item_prov: dict) -> tuple[str, str]:
    """Clave proveedor + cod_item_prov para todas las filas del mismo ítem en catálogo."""
    cod_prov = normalizar_cod_proveedor_para_match(item_prov.get("cod_proveedor", ""))
    cod = (item_prov.get("cod_item_prov") or "").strip()
    return (cod_prov, cod)


def _filas_precio_bd_items_prov_1based(item_prov: dict, layout: dict) -> list[int]:
    """
    Todas las filas 1-based con el mismo (cod_proveedor, cod_item_prov).
    Si hay duplicados en hoja, el precio debe replicarse en todas hasta unificar.
    """
    key = _clave_precio_items_prov(item_prov)
    rows = layout.get("clave_to_rows_1based", {}).get(key)
    if rows:
        return list(rows)
    cod = (item_prov.get("cod_item_prov") or "").strip()
    row = layout.get("cod_to_row_1based", {}).get(cod)
    if row is not None:
        return [row]
    fila = item_prov.get("_fila_sheet_1based")
    if fila is not None:
        try:
            return [int(fila)]
        except (TypeError, ValueError):
            pass
    return []


def _fila_precio_bd_items_prov_1based(item_prov: dict, layout: dict) -> int | None:
    """Primera fila de precio (compatibilidad)."""
    filas = _filas_precio_bd_items_prov_1based(item_prov, layout)
    return filas[0] if filas else None


def _set_bd_items_prov_price_layout_from_values(
    ws: gspread.Worksheet, values: list[list[str]], header_row_idx: int
) -> None:
    """Mapa (proveedor, cod_item_prov) -> filas 1-based para actualizar precios."""
    global _bd_items_prov_price_layout
    headers = [h.strip() for h in values[header_row_idx]]
    try:
        idx_cod = headers.index("cod_item_prov")
        idx_prov = headers.index("cod_proveedor")
        col_precio_ref = headers.index("precio_ref") + 1
        col_precio_xml = headers.index("precio_unitario_xml") + 1
        col_fecha = headers.index("fecha_precio_ref") + 1
    except ValueError as e:
        print(f"  WARN columna precio en BD_ITEMS_PROV: {e}")
        _bd_items_prov_price_layout = None
        return

    cod_to_row: dict[str, int] = {}
    clave_to_rows: dict[tuple[str, str], list[int]] = {}
    for i, row in enumerate(values[header_row_idx + 1 :]):
        if len(row) <= idx_cod:
            continue
        cod = row[idx_cod].strip()
        if not cod:
            continue
        cod_prov = (
            row[idx_prov].strip() if idx_prov < len(row) else ""
        )
        row_1based = header_row_idx + i + 2
        cod_to_row.setdefault(cod, row_1based)
        key = (
            normalizar_cod_proveedor_para_match(cod_prov),
            cod,
        )
        clave_to_rows.setdefault(key, []).append(row_1based)

    _bd_items_prov_price_layout = {
        "ws": ws,
        "idx_cod": idx_cod,
        "idx_prov": idx_prov,
        "col_precio_ref": col_precio_ref,
        "col_precio_xml": col_precio_xml,
        "col_fecha": col_fecha,
        "cod_to_row_1based": cod_to_row,
        "clave_to_rows_1based": clave_to_rows,
    }


def cargar_bd_items_prov() -> list[dict]:
    global _items_prov_cache
    if _items_prov_cache is not None:
        return _items_prov_cache

    _invalidar_cache_layout_precio_items_prov()

    print("  Cargando BD_ITEMS_PROV...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_ITEMS_PROV")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any(c.strip() == "cod_item_prov" for c in row):
            header_row_idx = i
            break

    if header_row_idx is None:
        print("  ERROR: no se encontró header en BD_ITEMS_PROV")
        return []

    headers = values[header_row_idx]
    rows = values[header_row_idx + 2 :]  # salta fila [FK][LINK][PK]...
    result = []
    for i, row in enumerate(rows):
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[j].strip(): row[j].strip()
            for j in range(min(len(headers), len(row)))
        }
        if r.get("activo", "SI") != "NO":
            # Fila real en Sheets (1-based) para escribir precios aunque cod_item_prov esté vacío
            r["_fila_sheet_1based"] = header_row_idx + 3 + i
            result.append(r)

    _set_bd_items_prov_price_layout_from_values(ws, values, header_row_idx)

    print(f"  {len(result)} items cargados en BD_ITEMS_PROV")
    _items_prov_cache = result
    return result


_prov_ruc_cache = None  # ruc -> cod_proveedor


def _ruc_claves_equivalentes(ruc: str) -> list[str]:
    """Claves para matchear BD_PROV: texto tal cual y RUC solo dígitos (13 en Ecuador)."""
    raw = (ruc or "").strip().strip("'")
    out: list[str] = []
    if raw:
        out.append(raw)
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return out
    if len(digits) <= 13:
        d13 = digits.zfill(13) if len(digits) < 13 else digits
        if len(d13) == 13 and d13 not in out:
            out.append(d13)
    return out


def _cod_proveedor_desde_ruc(lookup: dict[str, str], ruc: str) -> str:
    for key in _ruc_claves_equivalentes(ruc):
        v = lookup.get(key)
        if v:
            return v
    return ""


def _descripcion_coincide_catalogo_factura(desc_catalogo: str, desc_factura: str) -> bool:
    """
    True si la descripción en BD_ITEMS_PROV y la del XML describen el mismo ítem.
    Permite que el XML sea más largo o más corto (p. ej. tras copiar mal cod_item_prov).
    """
    a = (desc_catalogo or "").strip().upper()
    b = (desc_factura or "").strip().upper()
    if not a or not b:
        return False
    if min(len(a), len(b)) >= 6:
        return a in b or b in a
    return a == b or b in a


def _elegir_item_prov_cod_duplicados(
    candidatos: list[dict], descripcion: str = ""
) -> dict:
    """Elige una fila cuando varias comparten cod_item_prov (mismo proveedor)."""
    if len(candidatos) == 1:
        return candidatos[0]
    if descripcion:
        desc_hits = [
            m
            for m in candidatos
            if _descripcion_coincide_catalogo_factura(
                m.get("descripcion_proveedor", ""), descripcion
            )
        ]
        if len(desc_hits) == 1:
            return desc_hits[0]
    con_precio = [
        m for m in candidatos if (_leer_precio_ref_unidad_base(m) or 0) > 0
    ]
    if len(con_precio) == 1:
        return con_precio[0]
    if con_precio:
        return con_precio[0]
    return candidatos[0]


def cargar_lookup_ruc() -> dict[str, str]:
    global _prov_ruc_cache
    if _prov_ruc_cache is not None:
        return _prov_ruc_cache

    print("  Cargando BD_PROV para lookup RUC...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_PROV")
    values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(values):
        if any(c.strip() == "cod_proveedor" for c in row):
            header_row_idx = i
            break

    if header_row_idx is None:
        print("  WARN: no se encontró header en BD_PROV")
        _prov_ruc_cache = {}
        return _prov_ruc_cache

    headers = values[header_row_idx]
    rows = values[header_row_idx + 1 :]

    try:
        col_cod = headers.index("cod_proveedor")
        col_ruc = headers.index("RUC")
    except ValueError:
        print("  WARN: columnas cod_proveedor/RUC no encontradas en BD_PROV")
        _prov_ruc_cache = {}
        return _prov_ruc_cache

    lookup: dict[str, str] = {}
    for row in rows:
        if len(row) <= max(col_cod, col_ruc):
            continue
        cod = row[col_cod].strip()
        ruc = row[col_ruc].strip()
        if cod and ruc:
            for key in _ruc_claves_equivalentes(ruc):
                if key:
                    lookup[key] = cod

    print(f"  {len(lookup)} proveedores en lookup RUC")
    _prov_ruc_cache = lookup
    return lookup


def buscar_item_prov(
    ruc: str, cod_item_xml: str, descripcion: str = "", razon_social: str = ""
) -> dict | None:
    items = cargar_bd_items_prov()

    lookup_ruc = cargar_lookup_ruc()
    ruc_s = (ruc or "").strip().strip("'")
    cod_prov = _cod_proveedor_desde_ruc(lookup_ruc, ruc_s)
    cod_prov_n = normalizar_cod_proveedor_para_match(cod_prov)

    cod_item_norm = normalizar_cod_item_para_match(cod_item_xml, razon_social, ruc_s)

    cod_matches: list[dict] = []
    for item in items:
        if normalizar_cod_proveedor_para_match(item.get("cod_proveedor", "")) != cod_prov_n:
            continue
        item_cod = normalizar_cod_item_para_match(
            item.get("cod_item_prov", ""), razon_social, ruc_s
        )
        if item_cod == cod_item_norm:
            cod_matches.append(item)
    if cod_matches:
        return _elegir_item_prov_cod_duplicados(cod_matches, descripcion)

    # Misma factura / proveedor: descripción única (útil si cod_item_prov quedó como cod_mp u otro valor)
    if descripcion and cod_prov_n:
        hits: list[dict] = []
        for item in items:
            if normalizar_cod_proveedor_para_match(item.get("cod_proveedor", "")) != cod_prov_n:
                continue
            if _descripcion_coincide_catalogo_factura(
                item.get("descripcion_proveedor", ""), descripcion
            ):
                hits.append(item)
        if len(hits) == 1:
            return hits[0]

    # cod_mp_sistema en catálogo coincide con código de línea del XML (confusión de columnas)
    if cod_prov_n and cod_item_norm:
        mp_hits = [
            it
            for it in items
            if normalizar_cod_proveedor_para_match(it.get("cod_proveedor", "")) == cod_prov_n
            and normalizar_cod_item_para_match(
                it.get("cod_mp_sistema", ""), razon_social, ruc_s
            )
            == cod_item_norm
        ]
        if len(mp_hits) == 1:
            return mp_hits[0]

    for item in items:
        item_cod = normalizar_cod_item_para_match(
            item.get("cod_item_prov", ""), razon_social, ruc_s
        )
        if item_cod == cod_item_norm:
            return item

    if cod_item_norm:
        mp_any = [
            it
            for it in items
            if normalizar_cod_item_para_match(
                it.get("cod_mp_sistema", ""), razon_social, ruc_s
            )
            == cod_item_norm
        ]
        if len(mp_any) == 1:
            return mp_any[0]

    if descripcion:
        desc_upper = descripcion.upper()
        for item in items:
            desc = item.get("descripcion_proveedor", "").upper()
            if desc and desc in desc_upper:
                return item

    return None


# ── LÓGICA DE PRECIOS ─────────────────────────────────────────
def procesar_variacion_precio(
    item_prov: dict,
    factura: dict,
    item_factura: dict,
    *,
    solo_escritura_precio: bool = False,
):
    """
    Sincroniza BD_ITEMS_PROV con cada línea de factura matcheada.

    precio_ref = USD por unidad_base_sistema (costo_efectivo ÷ factor_conversion).
    precio_unitario_xml = precio unitario en XML del proveedor (suele ser por caja/pack).

    hist_precios + mensaje de ALERTA solo si la variación vs precio_ref anterior supera umbral.

    solo_escritura_precio: solo escribe las 3 columnas en Sheets (p. ej. backfill masivo
    desde XML viejos sin llenar hist_precios ni comparar umbral).
    """
    costo_efectivo = item_factura["costo_efectivo"]
    precio_u_xml = item_factura["precio_unitario_xml"]
    fecha_f = factura["fecha_factura"]
    precio_ref_nuevo = _precio_ref_unidad_base(item_prov, costo_efectivo)

    if solo_escritura_precio:
        _actualizar_precio_ref(item_prov, precio_ref_nuevo, precio_u_xml, fecha_f)
        _patch_item_prov_cache_tras_precio(item_prov, precio_ref_nuevo, precio_u_xml, fecha_f)
        return

    cod_catalogo = item_prov.get("cod_item_prov", "")
    umbral = float(cfg("umbral_alerta_precio", os.getenv("UMBRAL_ALERTA_PRECIO", "0.05")))

    precio_ref_ant = _leer_precio_ref_unidad_base(item_prov)

    if precio_ref_ant is None:
        print(
            f"    Primera factura o sin precio_ref previo para {cod_catalogo} "
            f"-> {precio_ref_nuevo} USD/uni"
        )
    elif precio_ref_ant == 0:
        print(f"    precio_ref=0 para {cod_catalogo} -> {precio_ref_nuevo} USD/uni")
    else:
        variacion = (precio_ref_nuevo - precio_ref_ant) / precio_ref_ant
        if abs(variacion) > umbral:
            print(
                f"    ALERTA precio {cod_catalogo}: {precio_ref_ant} -> {precio_ref_nuevo} "
                f"USD/uni ({variacion:.1%})"
            )
            _escribir_hist_precios(
                item_prov, factura, item_factura, precio_ref_ant, variacion
            )
        else:
            print(f"    Precio estable {cod_catalogo}: variación={variacion:.2%}")

    _actualizar_precio_ref(item_prov, precio_ref_nuevo, precio_u_xml, fecha_f)
    _patch_item_prov_cache_tras_precio(item_prov, precio_ref_nuevo, precio_u_xml, fecha_f)


def _patch_item_prov_cache_tras_precio(
    item_prov: dict,
    precio_ref_unitario: float,
    precio_u_xml: float,
    fecha_f: str,
) -> None:
    """Evita comparaciones de variación obsoletas en la misma corrida (cache BD_ITEMS_PROV)."""
    item_prov["precio_ref"] = str(precio_ref_unitario)
    item_prov["precio_unitario_xml"] = str(precio_u_xml)
    item_prov["fecha_precio_ref"] = fecha_f


def _escribir_hist_precios(item_prov, factura, item_factura, precio_ref, variacion):
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    cod_hist = f"HIST-{ts}"
    registro = {
        "cod_hist": cod_hist,
        "descripcion_proveedor": item_prov.get("descripcion_proveedor", ""),
        "cod_proveedor": item_prov.get("cod_proveedor", ""),
        "cod_item_prov": item_prov.get("cod_item_prov", ""),
        "fecha_factura": factura["fecha_factura"],
        "num_factura": factura["num_factura"],
        "precio_anterior": precio_ref,
        "precio_nuevo": _precio_ref_unidad_base(
            item_prov, item_factura["costo_efectivo"]
        ),
        "precio_unitario": item_factura["precio_unitario_xml"],
        "variacion_pct": round(variacion, 6),
        "estado": "PENDIENTE",
        "observaciones": "Variación detectada automáticamente",
    }
    try:
        supabase.table("hist_precios").insert(registro).execute()
        print(f"    -> hist_precios registrado: {cod_hist}")
    except Exception as e:
        print(f"    ERROR insertando hist_precios: {e}")


def _cell_a1_local(row_1based: int, col_1based: int) -> str:
    """A1 sin nombre de pestaña (ws.batch_update ya fija la hoja)."""
    ref = rowcol_to_a1(row_1based, col_1based)
    if "!" in ref:
        return ref.rsplit("!", 1)[-1]
    return ref


def _sheets_batch_update_con_retry(ws: gspread.Worksheet, data: list[dict]) -> None:
    """batch_update con backoff exponencial en 429 (hasta 3 intentos)."""
    for attempt in range(3):
        try:
            ws.batch_update(data, value_input_option=ValueInputOption.user_entered)
            return
        except Exception as e:
            msg = str(e)
            if ("429" in msg or "Quota" in msg) and attempt < 2:
                wait = 65 * (attempt + 1)
                print(
                    f"  WARN Sheets 429; reintentando en {wait}s "
                    f"(intento {attempt + 1}/3)..."
                )
                time.sleep(wait)
            else:
                raise


def _actualizar_precio_ref(item_prov, precio_ref_unitario, precio_u_xml, fecha):
    global _bd_items_prov_price_layout, _items_prov_cache
    layout = _bd_items_prov_price_layout
    if layout is None:
        _items_prov_cache = None
        cargar_bd_items_prov()
        layout = _bd_items_prov_price_layout

    if layout is None:
        print("  WARN no se pudo resolver layout BD_ITEMS_PROV para precios")
        return

    ws = layout["ws"]
    cod_buscar = item_prov.get("cod_item_prov", "").strip()
    filas = _filas_precio_bd_items_prov_1based(item_prov, layout)

    if not filas:
        _invalidar_cache_layout_precio_items_prov()
        _items_prov_cache = None
        cargar_bd_items_prov()
        layout = _bd_items_prov_price_layout
        if layout is None:
            print(f"  WARN no layout BD_ITEMS_PROV tras recarga (cod_item_prov={cod_buscar!r})")
            return
        filas = _filas_precio_bd_items_prov_1based(item_prov, layout)
        if not filas:
            print(
                f"  WARN fila precio BD_ITEMS_PROV no resuelta "
                f"(cod_item_prov={cod_buscar!r}, sin _fila_sheet_1based)"
            )
            return

    col_precio_ref = layout["col_precio_ref"]
    col_precio_xml = layout["col_precio_xml"]
    col_fecha = layout["col_fecha"]

    updates: list[dict] = []
    for row_1based in filas:
        updates.extend(
            [
                {
                    "range": _cell_a1_local(row_1based, col_precio_ref),
                    "values": [[precio_ref_unitario]],
                },
                {
                    "range": _cell_a1_local(row_1based, col_precio_xml),
                    "values": [[precio_u_xml]],
                },
                {
                    "range": _cell_a1_local(row_1based, col_fecha),
                    "values": [[fecha]],
                },
            ]
        )
    _sheets_batch_update_con_retry(ws, updates)

    dup_note = f" ({len(filas)} filas)" if len(filas) > 1 else ""
    print(
        f"    -> precio_ref actualizado en Sheets{dup_note}: {cod_buscar} = "
        f"{precio_ref_unitario} USD/uni base"
    )


# ── ACTUALIZAR BD_MP_SISTEMA (stock + costo) ─────────────────
_mp_sistema_cache: dict | None = None  # (cod_mp, cod_bodega) -> {row_1based, stock_actual}
_mp_col_stock: int | None = None
_mp_col_costo: int | None = None


def _mp_cache_key(cod_mp: str, cod_bodega: str) -> tuple[str, str]:
    from bodegas_config import normalizar_cod_bodega

    return (cod_mp.strip(), normalizar_cod_bodega(cod_bodega))


def _cargar_mp_sistema_cache():
    """Carga BD_MP_SISTEMA una sola vez por ejecución (clave MP × bodega)."""
    global _mp_sistema_cache, _mp_col_stock, _mp_col_costo
    if _mp_sistema_cache is not None:
        return

    from bodegas_config import normalizar_cod_bodega

    print("  Cargando BD_MP_SISTEMA para actualizar stock/costo...")
    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()

    header_row_idx = next(
        (i for i, r in enumerate(values) if any(c.strip() == "cod_mp_sistema" for c in r)),
        None,
    )
    if header_row_idx is None:
        print("  WARN: no se encontro header cod_mp_sistema en BD_MP_SISTEMA")
        _mp_sistema_cache = {}
        return

    headers = [h.strip() for h in values[header_row_idx]]
    try:
        idx_cod = headers.index("cod_mp_sistema")
        idx_bod = headers.index("cod_bodega")
        _mp_col_stock = headers.index("stock_actual") + 1
    except ValueError as e:
        print(f"  WARN columna no encontrada en BD_MP_SISTEMA: {e}")
        _mp_sistema_cache = {}
        return

    try:
        _mp_col_costo = headers.index("costo_unitario_ref") + 1
    except ValueError:
        _mp_col_costo = None
        print("  WARN: costo_unitario_ref no encontrada — se omite")

    cache = {}
    for i, row in enumerate(values[header_row_idx + 1:]):
        if not any(c.strip() for c in row):
            continue
        cod = row[idx_cod].strip() if len(row) > idx_cod else ""
        bod = normalizar_cod_bodega(row[idx_bod] if len(row) > idx_bod else "")
        if not cod or not bod:
            continue
        stock_str = row[_mp_col_stock - 1].strip() if len(row) >= _mp_col_stock else "0"
        try:
            stock = float(stock_str.replace(",", ".")) if stock_str else 0.0
        except ValueError:
            stock = 0.0
        cache[_mp_cache_key(cod, bod)] = {
            "row_1based": header_row_idx + i + 2,
            "stock_actual": stock,
            "cod_mp": cod,
            "cod_bodega": bod,
        }

    _mp_sistema_cache = cache
    print(f"  {len(cache)} filas MP×bodega en cache BD_MP_SISTEMA")


def _flush_mp_sistema(
    deltas_stock: dict[tuple[str, str], float],
    deltas_costo: dict[tuple[str, str], float],
):
    """
    deltas_stock / deltas_costo: clave (cod_mp, cod_bodega).
    """
    if not deltas_stock and not deltas_costo:
        return
    _cargar_mp_sistema_cache()
    if not _mp_sistema_cache:
        return

    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    updates = []

    for key, delta in deltas_stock.items():
        info = _mp_sistema_cache.get(key)
        if not info:
            print(
                f"  WARN: no fila BD_MP_SISTEMA para {key[0]} @ {key[1]} "
                "(crear fila MP×bodega antes de ingresar)"
            )
            continue
        nuevo_stock = round(info["stock_actual"] + delta, 4)
        _mp_sistema_cache[key]["stock_actual"] = nuevo_stock
        updates.append({
            "range": rowcol_to_a1(info["row_1based"], _mp_col_stock),
            "values": [[nuevo_stock]],
        })
        print(f"    -> stock {key[0]}@{key[1]}: +{round(delta,4)} => {nuevo_stock}")

    if _mp_col_costo:
        for key, nuevo_costo in deltas_costo.items():
            info = _mp_sistema_cache.get(key)
            if not info:
                continue
            updates.append({
                "range": rowcol_to_a1(info["row_1based"], _mp_col_costo),
                "values": [[round(nuevo_costo, 6)]],
            })
            print(f"    -> costo {key[0]}@{key[1]}: {round(nuevo_costo, 6)}")

    if not updates:
        return

    batch_size = 50
    for i in range(0, len(updates), batch_size):
        _sheets_batch_update_con_retry(ws, updates[i : i + batch_size])
    print(f"  -> BD_MP_SISTEMA actualizado: {len(updates)} celdas")


# ── REGISTRAR ENTRADA EN MOV_INVENTARIO ──────────────────────
def mov_entrada_factura_linea_ya_registrada(
    num_documento: str,
    cod_mp: str,
    item_factura: dict,
) -> bool:
    """
    True si ya existe una ENTRADA de esta factura para esta línea (mismo MP + ítem XML).
    Permite reprocesar facturas PARCIAL tras dar de alta BD_ITEMS_PROV sin duplicar stock.
    """
    cod_xml = (item_factura.get("cod_item_xml") or "").strip()
    desc = (item_factura.get("descripcion_proveedor") or "").strip()
    marker = f"| ITEM_XML:{cod_xml}" if cod_xml else ""
    try:
        res = (
            supabase.table("mov_inventario")
            .select("observaciones")
            .eq("num_documento", num_documento)
            .eq("tipo_mov", "ENTRADA")
            .eq("origen_documento", "FACTURA")
            .eq("cod_mp_sistema", cod_mp)
            .execute()
        )
    except Exception as e:
        print(f"    WARN comprobando duplicados mov_inventario: {e}")
        return False

    for row in res.data or []:
        obs = (row.get("observaciones") or "")
        if cod_xml and marker in obs:
            return True
        # Compatibilidad: movimientos viejos sin marcador ITEM_XML
        if "| ITEM_XML:" not in obs and obs.strip() == desc:
            return True
    return False


def _parse_factor_positivo(raw) -> float | None:
    """Factor de compra → base: debe ser explícito y > 0 (no se asume 1 por defecto)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = float(s.replace(",", "."))
    except ValueError:
        return None
    if v <= 0:
        return None
    return v


def _precio_ref_unidad_base(item_prov: dict, costo_compra_efectivo: float) -> float:
    """
    USD por unidad_base_sistema.
    costo_compra_efectivo viene del XML siempre por unidad_compra (bloque, caja, etc.).
    """
    fac = _parse_factor_positivo(item_prov.get("factor_conversion"))
    if fac and fac > 0:
        return round(costo_compra_efectivo / fac, 6)
    return round(costo_compra_efectivo, 6)


def _leer_precio_ref_unidad_base(item_prov: dict) -> float | None:
    """Lee precio_ref como USD por unidad_base."""
    from recalcular_stock_sheets import _precio_unitario_desde_items_prov

    s = (item_prov.get("precio_ref") or "").strip()
    if not s:
        return None
    from numeros_sheets import parse_numero_sheets

    precio = parse_numero_sheets(s)
    if precio <= 0:
        return None
    fac = _parse_factor_positivo(item_prov.get("factor_conversion")) or 1.0
    return _precio_unitario_desde_items_prov(precio, fac)


def conversion_compra_definida(item_prov: dict) -> tuple[bool, str]:
    """
    Exige factor_conversion numérico > 0 y unidad_compra no vacía en BD_ITEMS_PROV.
    Evita desviaciones de inventario por asumir factor 1 cuando la celda está vacía.
    """
    fac = _parse_factor_positivo(item_prov.get("factor_conversion"))
    if fac is None:
        return (
            False,
            "factor_conversion vacío, no numérico o ≤0 en BD_ITEMS_PROV "
            "(rellena el factor explícito; no se asume 1)",
        )
    u_compra = (item_prov.get("unidad_compra") or "").strip()
    if not u_compra:
        return (
            False,
            "unidad_compra vacía en BD_ITEMS_PROV "
            "(indica la unidad en que factura el proveedor: botella, kg, caja, etc.)",
        )
    return True, ""


def registrar_entrada_inventario(
    item_prov: dict,
    item_factura: dict,
    factura: dict,
    *,
    cod_bodega_destino: str | None = None,
):
    ok_conv, motivo_conv = conversion_compra_definida(item_prov)
    if not ok_conv:
        print(f"    ALERTA INVENTARIO: {motivo_conv}")
        return False

    cod_mp = item_prov.get("cod_mp_sistema", "").strip()
    from bodegas_config import resolver_bodega_entrada_linea

    bodega, err = resolver_bodega_entrada_linea(
        item_prov,
        bodega_override=cod_bodega_destino,
        confirmada=bool(cod_bodega_destino),
    )
    if err or not bodega:
        print(f"    ALERTA BODEGA: {err} — no se registra ENTRADA para {cod_mp}")
        return False
    unidad = item_prov.get("unidad_base_sistema", "").strip()
    factor = _parse_factor_positivo(item_prov.get("factor_conversion"))
    assert factor is not None
    cantidad_base = item_factura["cantidad"] * factor
    costo_u = item_factura["costo_efectivo"] / factor if factor else 0

    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
    cod_mov = f"MOV-{factura['fecha_factura'].replace('-', '')}-{cod_mp}-{ts}"

    cod_xml = (item_factura.get("cod_item_xml") or "").strip()
    desc = (item_factura.get("descripcion_proveedor") or "").strip()
    observaciones = f"{desc} | ITEM_XML:{cod_xml}" if cod_xml else desc

    mov = {
        "cod_mov": cod_mov,
        "fecha": f"{factura['fecha_factura']}T00:00:00",
        "tipo_mov": "ENTRADA",
        "cod_mp_sistema": cod_mp,
        "nombre_mp": item_prov.get("nombre_mp", ""),
        "cod_bodega_origen": None,
        "cod_bodega_destino": bodega,
        "cantidad_mov": round(cantidad_base, 4),
        "unidad_base": unidad,
        "costo_unitario": round(costo_u, 6),
        "costo_total": round(item_factura["precio_total_sin_impuesto"], 4),
        "origen_documento": "FACTURA",
        "num_documento": factura["num_factura"],
        "registrado_por": "AGENTE",
        "observaciones": observaciones,
    }

    try:
        supabase.table("mov_inventario").insert(mov).execute()
        print(
            f"    -> mov_inventario ENTRADA: {cod_mp} +{round(cantidad_base, 2)} {unidad}"
        )
        return True
    except Exception as e:
        print(f"    ERROR insertando mov_inventario: {e}")
        return False


# ── DEDUPLICACIÓN — facturas_procesadas ──────────────────────
def factura_ya_procesada(num_factura: str, ruc_proveedor: str) -> bool:
    """
    Retorna True si la factura ya existe en facturas_procesadas con estado COMPLETA.
    Las PARCIAL se reprocesán para intentar resolver ítems sin match previos.
    """
    try:
        res = (
            supabase.table("facturas_procesadas")
            .select("estado")
            .eq("num_factura", num_factura)
            .eq("ruc_proveedor", ruc_proveedor)
            .execute()
        )
        if not res.data:
            return False
        estado = res.data[0].get("estado", "")
        if estado == "COMPLETA":
            print(f"  SKIP: factura ya procesada (COMPLETA) — {num_factura}")
            return True
        if estado == "PARCIAL":
            print(f"  REPROCESANDO: factura previa PARCIAL — {num_factura} (intentando resolver sin match)")
        return False
    except Exception as e:
        print(f"  WARN verificando facturas_procesadas: {e} — procesando igual")
        return False


def registrar_factura_procesada(
    factura: dict,
    archivo: dict,
    items_matcheados: int,
    items_warn: int,
    dry_run: bool = False,
):
    """
    Escribe o actualiza el registro en facturas_procesadas al finalizar una factura.
    Estado: COMPLETA si items_warn == 0, PARCIAL si hubo algún sin match.
    """
    if dry_run:
        estado = "COMPLETA" if items_warn == 0 else "PARCIAL"
        print(f"  [DRY RUN] facturas_procesadas -> {estado} (matcheados={items_matcheados}, sin_match={items_warn})")
        return

    estado = "COMPLETA" if items_warn == 0 else "PARCIAL"
    meta = factura.get("_meta") or {}
    if factura.get("_pendiente_bodega"):
        meta["pendiente_bodega"] = factura["_pendiente_bodega"]
    razon = (factura.get("razon_social") or "").strip()
    if razon:
        meta["razon_social"] = razon
    total_xml = factura.get("total_sin_impuesto")
    if total_xml is not None:
        try:
            meta["total_sin_impuesto_xml"] = round(float(total_xml), 2)
        except (TypeError, ValueError):
            pass

    registro = {
        "num_factura": factura["num_factura"],
        "ruc_proveedor": factura["ruc"],
        "drive_file_id": archivo.get("id", ""),
        "fecha_factura": factura["fecha_factura"],
        "fecha_proceso": datetime.now().isoformat(),
        "items_procesados": items_matcheados,
        "items_sin_match": items_warn,
        "estado": estado,
        "meta": meta,
    }
    try:
        supabase.table("facturas_procesadas").upsert(
            registro,
            on_conflict="num_factura,ruc_proveedor",
        ).execute()
        print(f"  -> facturas_procesadas: {estado} (matcheados={items_matcheados}, sin_match={items_warn})")
    except Exception as e:
        err = str(e)
        if "meta" in err.lower() and registro.get("meta"):
            registro.pop("meta", None)
            try:
                supabase.table("facturas_procesadas").upsert(
                    registro,
                    on_conflict="num_factura,ruc_proveedor",
                ).execute()
                print(
                    "  -> facturas_procesadas (sin meta; ejecute "
                    "sql/add_facturas_procesadas_meta_bodega.sql)"
                )
            except Exception as e2:
                print(f"  ERROR registrando facturas_procesadas: {e2}")
        else:
            print(f"  ERROR registrando facturas_procesadas: {e}")


# ── HOJA BD_ITEMS_PENDIENTES (ítems sin match → revisión / alta MP) ─
def _col_letter_1based(col_idx: int) -> str:
    """Convierte índice de columna 1-based a letra(s) tipo A, B, ..., AA."""
    return "".join(c for c in rowcol_to_a1(1, col_idx) if c.isalpha())


# Una sola lectura de cabecera BD_MP_SISTEMA por ejecución (evita 429 en backfill).
_bd_mp_sistema_col_cache: tuple[int, int, int] | None = None


def _bd_mp_sistema_column_indexes(sh) -> tuple[int, int, int] | None:
    """Índices 1-based de columnas cod_mp_sistema, nombre_mp, unidad_base en BD_MP_SISTEMA."""
    global _bd_mp_sistema_col_cache
    if _bd_mp_sistema_col_cache is not None:
        return _bd_mp_sistema_col_cache

    ws = sh.worksheet("BD_MP_SISTEMA")
    values = ws.get_all_values()
    header_row_idx = None
    for i, row in enumerate(values):
        if any((c or "").strip() == "cod_mp_sistema" for c in row):
            header_row_idx = i
            break
    if header_row_idx is None:
        return None
    headers = [(c or "").strip() for c in values[header_row_idx]]
    try:
        ic = headers.index("cod_mp_sistema") + 1
        inom = headers.index("nombre_mp") + 1
        iu = headers.index("unidad_base") + 1
        _bd_mp_sistema_col_cache = (ic, inom, iu)
        return _bd_mp_sistema_col_cache
    except ValueError:
        return None


_items_pendientes_cache_keys: dict[str, str] | None = None
_items_pendientes_ignorados_por_item: dict[str, str] | None = None
_cod_proveedores_strip_cache: frozenset[str] | None = None


def _cod_proveedores_strip_para_catalogo() -> frozenset[str]:
    global _cod_proveedores_strip_cache
    if _cod_proveedores_strip_cache is not None:
        return _cod_proveedores_strip_cache
    try:
        sh = _get_sheet()
        vals = sh.worksheet("BD_PROV").get_all_values()
        _cod_proveedores_strip_cache = cod_proveedores_strip_sufijo_desde_bd_prov(vals)
    except Exception:
        _cod_proveedores_strip_cache = frozenset()
    return _cod_proveedores_strip_cache


# Evita get_all_values() por cada fila nueva (backfill masivo → 429 en lecturas).
_pendientes_vals_cache: list[list[str]] | None = None
_pendientes_hi_cache: int | None = None
_pendientes_next_row_cache: int | None = None
_pendientes_ws_cache: gspread.Worksheet | None = None


def _pendientes_invalidate_sheet_cache() -> None:
    global _pendientes_vals_cache, _pendientes_hi_cache, _pendientes_next_row_cache
    global _pendientes_ws_cache
    _pendientes_vals_cache = None
    _pendientes_hi_cache = None
    _pendientes_next_row_cache = None
    _pendientes_ws_cache = None


def _pendientes_worksheet(sh) -> gspread.Worksheet:
    """Una sola resolución de pestaña (evita fetch_sheet_metadata por fila)."""
    global _pendientes_ws_cache
    if _pendientes_ws_cache is None:
        _pendientes_ws_cache = _ensure_bd_items_pendientes_sheet(sh)
    return _pendientes_ws_cache


def _pendientes_boot_sheet_cache(ws: gspread.Worksheet) -> bool:
    """Carga la hoja pendientes una vez y fija la próxima fila para append."""
    global _pendientes_vals_cache, _pendientes_hi_cache, _pendientes_next_row_cache
    if _pendientes_vals_cache is not None and _pendientes_hi_cache is not None:
        return True
    allv = ws.get_all_values()
    hi = _pendientes_header_row_idx(allv)
    if hi is None:
        return False
    _pendientes_vals_cache = allv
    _pendientes_hi_cache = hi
    _pendientes_next_row_cache = _pendientes_next_row_1based_for_append(allv, hi)
    return True


def _pendientes_header_row_idx(values: list[list[str]]) -> int | None:
    """Fila 0-based donde está la cabecera con columna clave_unica (puede no ser la fila 1)."""
    for i, row in enumerate(values):
        cells = [(c or "").strip() for c in row]
        if "clave_unica" in cells:
            return i
        # Cabecera legacy corrupta (A1 = "ama" en lugar de clave_unica).
        if cells and cells[0] == "ama" and "fecha_registro" in cells:
            return i
    return None


def _pendientes_col_clave_unica(headers: list[str]) -> int:
    """Índice 0-based de clave_unica (o legacy 'ama')."""
    for name in ("clave_unica", "ama"):
        try:
            return headers.index(name)
        except ValueError:
            continue
    return 0


def fecha_factura_permite_ingreso_stock(fecha_factura: str) -> bool:
    """False si la factura es anterior a FECHA_MIN_INGRESO_FACTURA (sin mov ni stock)."""
    f = (fecha_factura or "").strip()[:10]
    if not f or len(f) < 10:
        return False
    return f >= FECHA_MIN_INGRESO_FACTURA


def reparar_bd_items_pendientes_cabecera() -> dict[str, str | int | bool]:
    """
    Corrige cabecera BD_ITEMS_PENDIENTES: A1 'ama' → clave_unica;
    agrega columnas faltantes (motivo_pendiente, origen_factura).
    """
    sh = _get_sheet()
    ws = sh.worksheet(BD_ITEMS_PENDIENTES_SHEET)
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        return {"ok": False, "error": "no se detectó fila de cabecera"}

    headers = [(c or "").strip() for c in vals[hi]]
    cambios: list[str] = []
    row_1based = hi + 1

    if headers and headers[0] == "ama":
        ws.update(
            values=[["clave_unica"]],
            range_name=rowcol_to_a1(row_1based, 1),
            value_input_option=ValueInputOption.user_entered,
        )
        headers[0] = "clave_unica"
        cambios.append("A1: ama → clave_unica")

    canon = _pendientes_headers_canonicos()
    for col_name in ("motivo_pendiente", "origen_factura"):
        if col_name not in headers:
            col_1 = len(headers) + 1
            ws.update(
                values=[[col_name]],
                range_name=rowcol_to_a1(row_1based, col_1),
                value_input_option=ValueInputOption.user_entered,
            )
            headers.append(col_name)
            cambios.append(f"+columna {col_name}")

    _pendientes_invalidate_sheet_cache()
    global _items_pendientes_cache_keys, _items_pendientes_ignorados_por_item
    _items_pendientes_cache_keys = None
    _items_pendientes_ignorados_por_item = None

    return {
        "ok": True,
        "cambios": cambios,
        "filas_datos": max(0, len(vals) - hi - 1),
        "cabeceras": len(headers),
    }


def _pendientes_load_keys(ws: gspread.Worksheet) -> dict[str, str]:
    """Claves ya registradas con su estado: {clave_unica: estado}; ítem+ruc IGNORADO aparte."""
    global _items_pendientes_ignorados_por_item
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        _items_pendientes_ignorados_por_item = {}
        return {}
    headers = [(c or "").strip() for c in vals[hi]]
    try:
        idx_estado = headers.index("estado")
    except ValueError:
        idx_estado = None
    try:
        idx_cod_item = headers.index("cod_item_xml")
    except ValueError:
        idx_cod_item = None
    try:
        idx_ruc = headers.index("ruc_proveedor")
    except ValueError:
        idx_ruc = None

    out: dict[str, str] = {}
    ignorados: dict[str, str] = {}

    idx_clave = _pendientes_col_clave_unica(headers)

    for row in vals[hi + 1 :]:
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        cell = (row[idx_clave] if idx_clave < len(row) else row[0] or "").strip()
        if not cell or cell in ("clave_unica", "ama"):
            continue
        estado = (
            (row[idx_estado] if idx_estado is not None and idx_estado < len(row) else "")
            .strip()
            .upper()
            or "PENDIENTE"
        )
        out[cell] = estado

        if estado == "IGNORADO" and idx_cod_item is not None and idx_ruc is not None:
            cod_item = (row[idx_cod_item] if idx_cod_item < len(row) else "").strip()
            ruc = (row[idx_ruc] if idx_ruc < len(row) else "").strip()
            if cod_item and ruc:
                ignorados[f"{cod_item}|{ruc}"] = "IGNORADO"

    _items_pendientes_ignorados_por_item = ignorados
    return out


def _costo_total_xml_desde_item(item: dict) -> float:
    """Total línea factura SRI: precioTotalSinImpuesto o cantidad × precioUnitario."""
    total = _safe_float(item.get("precio_total_sin_impuesto"))
    if total > 0:
        return round(total, 6)
    cant = _safe_float(item.get("cantidad"))
    pu = _safe_float(item.get("precio_unitario_xml"))
    return round(cant * pu, 6) if cant > 0 and pu > 0 else 0.0


def _pendientes_col_letter(headers: list[str], col_name: str) -> str:
    try:
        return _col_letter_1based(headers.index(col_name) + 1)
    except ValueError:
        return "A"


def _pendientes_headers_canonicos() -> list[str]:
    return [
        "clave_unica",
        "fecha_registro",
        "fecha_factura",
        "num_factura",
        "ruc_proveedor",
        "razon_social",
        "cod_proveedor",
        "cod_item_xml",
        "descripcion_xml",
        "cantidad",
        "precio_unitario_xml",
        "costo_efectivo",
        COL_COSTO_TOTAL_PENDIENTES,
        "drive_file_id",
        "nombre_archivo_xml",
        "estado",
        "cod_mp_asignado",
        "nombre_mp_desde_bd",
        "unidad_base_desde_bd",
        "link_xml",
        "plantilla_cod_item_prov",
        "plantilla_cod_mp",
        "plantilla_descripcion_prov",
        "motivo_pendiente",
        "origen_factura",
    ]


def _pendientes_headers_actuales() -> list[str]:
    """Cabeceras de la hoja en caché (o canónicas si aún no se cargó)."""
    if _pendientes_vals_cache is not None and _pendientes_hi_cache is not None:
        row = _pendientes_vals_cache[_pendientes_hi_cache]
        return [(c or "").strip() for c in row]
    return _pendientes_headers_canonicos()


def _pendientes_next_row_1based_for_append(vals: list[list[str]], hi: int) -> int:
    """Número de fila 1-based donde append_row colocará la próxima fila (fórmulas A{row}, etc.)."""
    last_0 = hi
    for i in range(hi + 1, len(vals)):
        row = vals[i]
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        last_0 = i
    return last_0 + 2


def _ensure_bd_items_pendientes_sheet(sh):
    """Crea la hoja y cabeceras si no existe."""
    global _items_pendientes_cache_keys, _items_pendientes_ignorados_por_item
    try:
        ws = sh.worksheet(BD_ITEMS_PENDIENTES_SHEET)
        if _items_pendientes_cache_keys is None:
            _items_pendientes_cache_keys = _pendientes_load_keys(ws)
        return ws
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=BD_ITEMS_PENDIENTES_SHEET, rows=2000, cols=26
        )
        headers = _pendientes_headers_canonicos()
        ws.update(
            range_name=f"A1:{_col_letter_1based(len(headers))}1",
            values=[headers],
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"  [BD_ITEMS_PENDIENTES] Hoja creada: {BD_ITEMS_PENDIENTES_SHEET}")
        _items_pendientes_cache_keys = {}
        _items_pendientes_ignorados_por_item = {}
        _pendientes_invalidate_sheet_cache()
        return ws


def _clave_item_pendiente(factura: dict, item: dict) -> str:
    return "|".join(
        [
            factura["num_factura"].strip(),
            factura["ruc"].strip(),
            item["cod_item_xml"].strip(),
        ]
    )


def registrar_item_pendiente_factura(
    factura: dict,
    item: dict,
    archivo: dict,
    cod_proveedor: str,
    *,
    dry_run: bool,
    motivo_pendiente: str = "SIN_MATCH",
    origen_factura: str = "",
) -> bool:
    """
    Registra una línea en BD_ITEMS_PENDIENTES para ítems sin match en BD_ITEMS_PROV.
    Idempotente por clave_unica (no duplica si ya existe).
    motivo_pendiente: SIN_MATCH | DUDA_OPERADOR | etc.
    Retorna True si se insertó una fila nueva.
    """
    sh = _get_sheet()
    ws = _pendientes_worksheet(sh)
    global _items_pendientes_cache_keys, _items_pendientes_ignorados_por_item
    global _pendientes_vals_cache, _pendientes_next_row_cache
    if _items_pendientes_cache_keys is None:
        _items_pendientes_cache_keys = _pendientes_load_keys(ws)

    clave_item = f"{item['cod_item_xml'].strip()}|{factura['ruc'].strip()}"
    if clave_item in (_items_pendientes_ignorados_por_item or {}):
        return False

    clave = _clave_item_pendiente(factura, item)
    if clave in _items_pendientes_cache_keys:
        estado_existente = (_items_pendientes_cache_keys[clave] or "").strip().upper()
        if estado_existente == "IGNORADO":
            return False
        print(f"    INFO: ya listado en {BD_ITEMS_PENDIENTES_SHEET} — {clave[:40]}...")
        return False

    if dry_run:
        print(
            f"    [DRY RUN] registraría en {BD_ITEMS_PENDIENTES_SHEET}: "
            f"{item['cod_item_xml']} — {item['descripcion_proveedor'][:60]}"
        )
        return False

    idx = _bd_mp_sistema_column_indexes(sh)
    if not idx:
        print(f"    WARN: no se pudieron detectar columnas en BD_MP_SISTEMA; fila sin fórmulas.")
        col_cod_l = col_nom_l = col_uni_l = "A"
    else:
        ic, inom, iu = idx
        col_cod_l = _col_letter_1based(ic)
        col_nom_l = _col_letter_1based(inom)
        col_uni_l = _col_letter_1based(iu)

    if not _pendientes_boot_sheet_cache(ws):
        print(
            f"    ERROR: no se encontró la fila de cabecera (clave_unica) en "
            f"{BD_ITEMS_PENDIENTES_SHEET}; no se puede registrar el pendiente."
        )
        return False
    hi = _pendientes_hi_cache
    next_row = _pendientes_next_row_cache
    assert hi is not None and next_row is not None

    fecha_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    drive_id = (archivo.get("id") or "").strip()
    nombre_xml = (archivo.get("name") or "").strip()

    headers_row = _pendientes_headers_actuales()
    p_col = _pendientes_col_letter(headers_row, "cod_mp_asignado")
    drive_col = _pendientes_col_letter(headers_row, "drive_file_id")
    fq_nom = (
        f'=IF(${p_col}{next_row}="","", IFERROR(INDEX(BD_MP_SISTEMA!${col_nom_l}:${col_nom_l}, '
        f'MATCH(${p_col}{next_row}, BD_MP_SISTEMA!${col_cod_l}:${col_cod_l}, 0)), "NO EN BD"))'
    )
    fq_uni = (
        f'=IF(${p_col}{next_row}="","", IFERROR(INDEX(BD_MP_SISTEMA!${col_uni_l}:${col_uni_l}, '
        f'MATCH(${p_col}{next_row}, BD_MP_SISTEMA!${col_cod_l}:${col_cod_l}, 0)), ""))'
    )
    fq_link = (
        f'=IF({drive_col}{next_row}="","", HYPERLINK("https://drive.google.com/file/d/" & '
        f'{drive_col}{next_row} & "/view", "Ver XML"))'
    )
    # Sugerencia para copiar a BD_ITEMS_PROV (COLEMUN: sin sufijo -N; H conserva XML crudo)
    cod_plantilla = cod_item_prov_para_catalogo(
        item["cod_item_xml"],
        razon_social=factura.get("razon_social", ""),
        ruc=factura["ruc"],
        cod_proveedor=cod_proveedor,
        cod_proveedores_strip=_cod_proveedores_strip_para_catalogo(),
    )
    fq_pmp = f"=P{next_row}"
    fq_pdesc = f"=I{next_row}"

    motivo = (motivo_pendiente or "SIN_MATCH").strip().upper() or "SIN_MATCH"
    origen_row = (origen_factura or "").strip().upper()

    base_row = {
        "clave_unica": clave,
        "fecha_registro": fecha_iso,
        "fecha_factura": factura["fecha_factura"],
        "num_factura": factura["num_factura"],
        "ruc_proveedor": factura["ruc"],
        "razon_social": factura.get("razon_social", ""),
        "cod_proveedor": cod_proveedor,
        "cod_item_xml": item["cod_item_xml"],
        "descripcion_xml": item["descripcion_proveedor"],
        "cantidad": str(item["cantidad"]).replace(".", ","),
        "precio_unitario_xml": str(item["precio_unitario_xml"]).replace(".", ","),
        "costo_efectivo": str(item["costo_efectivo"]).replace(".", ","),
        COL_COSTO_TOTAL_PENDIENTES: str(_costo_total_xml_desde_item(item)).replace(
            ".", ","
        ),
        "drive_file_id": drive_id,
        "nombre_archivo_xml": nombre_xml,
        "estado": "PENDIENTE",
        "cod_mp_asignado": "",
        "nombre_mp_desde_bd": fq_nom,
        "unidad_base_desde_bd": fq_uni,
        "link_xml": fq_link,
        "plantilla_cod_item_prov": cod_plantilla,
        "plantilla_cod_mp": fq_pmp,
        "plantilla_descripcion_prov": fq_pdesc,
        "motivo_pendiente": motivo,
        "origen_factura": origen_row,
    }

    headers = _pendientes_headers_actuales()
    row_vals = [base_row.get(h, "") for h in headers]

    try:
        for attempt in range(3):
            try:
                ws.append_row(row_vals, value_input_option=ValueInputOption.user_entered)
                break
            except Exception as e:
                msg = str(e)
                if ("429" in msg or "Quota" in msg) and attempt < 2:
                    wait = 65 * (attempt + 1)
                    print(
                        f"  WARN Sheets 429 append pendiente; reintentando en {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                raise
    except Exception as e:
        print(
            f"    ERROR: no se pudo escribir en {BD_ITEMS_PENDIENTES_SHEET} "
            f"(clave={clave[:48]}…): {e}"
        )
        return False
    if _pendientes_vals_cache is not None:
        while len(_pendientes_vals_cache) < next_row - 1:
            _pendientes_vals_cache.append([])
        if len(_pendientes_vals_cache) >= next_row - 1:
            if len(_pendientes_vals_cache) == next_row - 1:
                _pendientes_vals_cache.append(row_vals)
            else:
                _pendientes_vals_cache[next_row - 1] = row_vals
        _pendientes_next_row_cache = next_row + 1
    _items_pendientes_cache_keys[clave] = "PENDIENTE"
    print(f"    -> {BD_ITEMS_PENDIENTES_SHEET}: registrado pendiente {item['cod_item_xml']}")
    return True


def sincronizar_items_pendientes_factura_drive(
    factura: dict,
    archivo: dict,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Solo hoja BD_ITEMS_PENDIENTES: líneas del XML que aún no tienen match en BD_ITEMS_PROV.
    No inserta mov_inventario ni precios. Idempotente por clave num|ruc|cod_item_xml.

    Las facturas marcadas COMPLETA en Supabase se omiten del flujo completo para no
    duplicar movimientos; igual conviene volver a intentar pendientes por si el catálogo
    cambió o faltó registrar una línea en su momento.
    """
    nuevos = 0
    ya_en_hoja = 0
    lookup_ruc = cargar_lookup_ruc()
    cod_prov = _cod_proveedor_desde_ruc(lookup_ruc, str(factura.get("ruc", "")).strip())
    cargar_bd_items_prov()
    for item in factura.get("items") or []:
        item_prov = buscar_item_prov(
            factura["ruc"],
            item["cod_item_xml"],
            item["descripcion_proveedor"],
            factura.get("razon_social", ""),
        )
        if item_prov:
            continue
        ok = registrar_item_pendiente_factura(
            factura,
            item,
            archivo,
            cod_prov,
            dry_run=dry_run,
        )
        if ok:
            nuevos += 1
            if not dry_run:
                time.sleep(0.35)
        else:
            ya_en_hoja += 1
    return nuevos, ya_en_hoja


def crear_hoja_bd_items_pendientes() -> None:
    """
    Crea la pestaña BD_ITEMS_PENDIENTES en el libro maestro si aún no existe.
    Útil para verla antes de procesar facturas con ítems sin match.
    """
    sh = _get_sheet()
    _ensure_bd_items_pendientes_sheet(sh)
    print(
        f"OK — pestaña '{BD_ITEMS_PENDIENTES_SHEET}' lista en el spreadsheet "
        f"(SPREADSHEET_ID del .env)."
    )


def _ensure_columna_costo_total_pendientes(ws: gspread.Worksheet, sh) -> list[str]:
    """
    Inserta columna costo_total_xml entre costo_efectivo y drive_file_id (posición M)
    si la hoja aún no la tiene. Devuelve cabeceras actualizadas.
    """
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        return _pendientes_headers_canonicos()
    headers = [(c or "").strip() for c in vals[hi]]
    if COL_COSTO_TOTAL_PENDIENTES in headers:
        return headers

    try:
        idx_ins = headers.index("costo_efectivo") + 1
    except ValueError:
        idx_ins = 12

    sh.batch_update(
        {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": idx_ins,
                            "endIndex": idx_ins + 1,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        }
    )
    _pendientes_invalidate_sheet_cache()
    time.sleep(1.5)
    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        return _pendientes_headers_canonicos()
    headers = [(c or "").strip() for c in vals[hi]]
    if COL_COSTO_TOTAL_PENDIENTES not in headers:
        col_1 = idx_ins + 1
        ws.update(
            values=[[COL_COSTO_TOTAL_PENDIENTES]],
            range_name=rowcol_to_a1(hi + 1, col_1),
            value_input_option=ValueInputOption.user_entered,
        )
        headers = headers[:idx_ins] + [COL_COSTO_TOTAL_PENDIENTES] + headers[idx_ins:]
    print(f"  Columna {COL_COSTO_TOTAL_PENDIENTES} insertada en {BD_ITEMS_PENDIENTES_SHEET}")
    return headers


def backfill_costo_total_pendientes_desde_xml(
    *,
    desde_fecha: str = FECHA_MIN_COSTO_TOTAL_PENDIENTES,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Rellena costo_total_xml en BD_ITEMS_PENDIENTES para filas con fecha_factura >= desde_fecha.
    Valor desde XML (precioTotalSinImpuesto o cantidad × precioUnitario).
    """
    from numeros_sheets import parse_numero_sheets

    desde = (desde_fecha or FECHA_MIN_COSTO_TOTAL_PENDIENTES).strip()[:10]
    _pendientes_invalidate_sheet_cache()
    sh = _get_sheet()
    ws = _pendientes_worksheet(sh)
    headers = _ensure_columna_costo_total_pendientes(ws, sh)

    try:
        idx_clave = headers.index("clave_unica")
        idx_fecha = headers.index("fecha_factura")
        idx_total = headers.index(COL_COSTO_TOTAL_PENDIENTES)
    except ValueError as e:
        print(f"ERROR columnas pendientes: {e}")
        return {"actualizadas": 0, "omitidas_fecha": 0, "sin_xml": 0}

    totales_xml: dict[str, float] = {}
    xmls = listar_xmls_pendientes()
    print(f"XMLs en Drive: {len(xmls)} | desde_fecha={desde}")
    for archivo in xmls:
        texto = descargar_xml(archivo["id"])
        factura = parsear_xml_sri(texto)
        if not factura:
            continue
        fecha_f = (factura.get("fecha_factura") or "")[:10]
        if fecha_f < desde:
            continue
        for item in factura.get("items") or []:
            clave = _clave_item_pendiente(factura, item)
            totales_xml[clave] = _costo_total_xml_desde_item(item)

    vals = ws.get_all_values()
    hi = _pendientes_header_row_idx(vals)
    if hi is None:
        return {"actualizadas": 0, "omitidas_fecha": 0, "sin_xml": 0}

    updates: list[dict] = []
    stats = {"actualizadas": 0, "omitidas_fecha": 0, "sin_xml": 0, "ya_ok": 0}

    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        if not row or not any((c or "").strip() for c in row):
            continue
        if str(row[0]).strip().startswith("["):
            continue
        fecha_row = (
            (row[idx_fecha] if idx_fecha < len(row) else "") or ""
        ).strip()[:10]
        if fecha_row < desde:
            stats["omitidas_fecha"] += 1
            continue
        clave = (row[idx_clave] if idx_clave < len(row) else "").strip()
        if not clave:
            continue
        total = totales_xml.get(clave)
        if total is None or total <= 0:
            stats["sin_xml"] += 1
            continue
        actual = parse_numero_sheets(
            row[idx_total] if idx_total < len(row) else 0, 0.0
        )
        if abs(actual - total) <= 1e-6:
            stats["ya_ok"] += 1
            continue
        updates.append(
            {
                "range": rowcol_to_a1(i, idx_total + 1),
                "values": [[total]],
            }
        )
        stats["actualizadas"] += 1

    print(
        f"Filas a actualizar: {stats['actualizadas']} | ya correctas: {stats['ya_ok']} | "
        f"sin match XML: {stats['sin_xml']} | antes de {desde}: {stats['omitidas_fecha']}"
    )
    if dry_run:
        print("[DRY RUN] no escribe en Sheets")
        return stats

    for j in range(0, len(updates), 50):
        ws.batch_update(
            updates[j : j + 50],
            value_input_option=ValueInputOption.user_entered,
        )
        time.sleep(1)
    if updates:
        print(f"Escrito {len(updates)} celdas en {COL_COSTO_TOTAL_PENDIENTES}")
    return stats


def backfill_items_pendientes_desde_drive(*, dry_run: bool = False) -> dict[str, int]:
    """
    Recorre todos los XML en GOOGLE_DRIVE_FACTURAS_FOLDER_ID, detecta líneas sin match
    en BD_ITEMS_PROV y las escribe en BD_ITEMS_PENDIENTES.
    No crea mov_inventario ni actualiza precios/stock.
    """
    global _items_pendientes_cache_keys, _items_pendientes_ignorados_por_item
    _items_pendientes_cache_keys = None
    _items_pendientes_ignorados_por_item = None
    _pendientes_invalidate_sheet_cache()

    cargar_bd_items_prov()
    lookup_ruc = cargar_lookup_ruc()
    sh = _get_sheet()
    _bd_mp_sistema_column_indexes(sh)
    ws_pend = _pendientes_worksheet(sh)
    if not dry_run:
        _pendientes_boot_sheet_cache(ws_pend)

    xmls = listar_xmls_pendientes()
    if not xmls:
        print("No hay XMLs en la carpeta de Drive (GOOGLE_DRIVE_FACTURAS_FOLDER_ID).")
        return {"xmls": 0, "insertadas": 0, "sin_match": 0, "ya_catalogados": 0, "ya_en_hoja": 0}

    stats = {
        "xmls": 0,
        "insertadas": 0,
        "sin_match": 0,
        "ya_catalogados": 0,
        "ya_en_hoja": 0,
    }

    print(f"XMLs en carpeta: {len(xmls)}")
    for archivo in xmls:
        texto = descargar_xml(archivo["id"])
        factura = parsear_xml_sri(texto)
        if not factura:
            print(f"  SKIP parse: {archivo.get('name')}")
            continue
        stats["xmls"] += 1
        cod_prov = _cod_proveedor_desde_ruc(lookup_ruc, factura["ruc"].strip())
        print(f"\n  {archivo.get('name')} | {factura['num_factura']} | items={len(factura['items'])}")

        for item in factura["items"]:
            item_prov = buscar_item_prov(
                factura["ruc"],
                item["cod_item_xml"],
                item["descripcion_proveedor"],
                factura.get("razon_social", ""),
            )
            if item_prov:
                stats["ya_catalogados"] += 1
                continue
            stats["sin_match"] += 1
            ok = registrar_item_pendiente_factura(
                factura,
                item,
                archivo,
                cod_prov,
                dry_run=dry_run,
            )
            if ok and not dry_run:
                time.sleep(0.12)
            if dry_run:
                continue
            if ok:
                stats["insertadas"] += 1
            else:
                stats["ya_en_hoja"] += 1

    print("\n" + "=" * 50)
    print(
        f"Backfill: XMLs leídos={stats['xmls']} | líneas sin match={stats['sin_match']} | "
        f"nuevas filas={stats['insertadas']} | ya en hoja={stats['ya_en_hoja']} | "
        f"ítems ya en BD_ITEMS_PROV={stats['ya_catalogados']}"
    )
    return stats


def _lineas_whatsapp_items_sin_match(factura: dict, descripciones_sin_match: list[str]) -> list[str]:
    """Una línea por ítem: cod_item_xml | descripción (empareja por descripcion_proveedor)."""
    items = factura.get("items") or []
    lineas: list[str] = []
    for desc in descripciones_sin_match:
        d = (desc or "").strip()
        if not d:
            continue
        cod = "?"
        texto = d
        for it in items:
            if (it.get("descripcion_proveedor") or "").strip() == d:
                cod = str(it.get("cod_item_xml") or "").strip() or "?"
                texto = (it.get("descripcion_proveedor") or "").strip() or d
                break
        lineas.append(f"- {cod} | {texto}")
    return lineas


def _sin_match_entradas_para_alerta(sin_match: list) -> list[str]:
    """Textos para alertas / conteo WARN: excluye filas IGNORADO o REGISTRADO en pendientes."""
    out: list[str] = []
    for it in sin_match or []:
        if isinstance(it, dict):
            est = (it.get("estado") or "").strip().upper()
            if est in ("IGNORADO", "REGISTRADO"):
                continue
            d = (it.get("descripcion") or "").strip()
            if d:
                out.append(d)
        else:
            s = str(it).strip()
            if s:
                out.append(s)
    return out


def _sin_match_count_alerta(sin_match: list) -> int:
    return len(_sin_match_entradas_para_alerta(sin_match))


def _batch_update_precios_items_prov_cells(
    ws: gspread.Worksheet,
    col_precio_ref: int,
    col_precio_xml: int,
    col_fecha: int,
    by_row: dict[int, tuple[float, float, str]],
) -> int:
    """
    Escribe precio_ref / precio_unitario_xml / fecha_precio_ref agrupando filas
    en pocas llamadas a la API (límite de escrituras por minuto en Sheets).
    """
    if not by_row:
        return 0
    rows_sorted = list(by_row.items())
    # Cada fila = 3 rangos; ~30 filas por POST para quedar bajo cuotas típicas.
    rows_per_http = 30
    n_ok = 0
    for start in range(0, len(rows_sorted), rows_per_http):
        chunk = rows_sorted[start : start + rows_per_http]
        data: list[dict] = []
        for row_1based, (c, u, f) in chunk:
            data.extend(
                [
                    {
                        "range": _cell_a1_local(row_1based, col_precio_ref),
                        "values": [[c]],
                    },
                    {
                        "range": _cell_a1_local(row_1based, col_precio_xml),
                        "values": [[u]],
                    },
                    {
                        "range": _cell_a1_local(row_1based, col_fecha),
                        "values": [[f]],
                    },
                ]
            )
        _sheets_batch_update_con_retry(ws, data)
        n_ok += len(chunk)
        time.sleep(1.2)
    return n_ok


def sincronizar_precios_items_prov_desde_todos_xml_drive(*, dry_run: bool = False) -> dict:
    """
    Para ítems ya mapeados en BD_ITEMS_PROV: recorre **todos** los XML de la carpeta
    de facturas (incluidos los de facturas ya COMPLETA en Supabase), ordenados por
    fecha de emisión y número.

    Actualiza solo precio_ref, precio_unitario_xml y fecha_precio_ref en Sheets.
    No inserta mov_inventario, no modifica BD_MP_SISTEMA ni facturas_procesadas.

    Tras dar de alta filas en BD_ITEMS_PROV que antes no matcheaban, la **última**
    factura histórica por fecha define los precios en catálogo.

    Sin hist_precios. En modo escritura, los cambios se envían a Sheets en **lotes**
    (pocas peticiones) para evitar error 429 por exceso de escrituras por minuto.
    """
    global _items_prov_cache
    print("=" * 50)
    print(
        "SYNC PRECIOS BD_ITEMS_PROV desde todos los XML en Drive "
        f"({'DRY RUN' if dry_run else 'escribiendo hoja'})"
    )
    print("=" * 50)

    xmls = listar_xmls_pendientes()
    if not xmls:
        print("AVISO: cero XML en carpeta (GOOGLE_DRIVE_FACTURAS_FOLDER_ID).")
        return {
            "xmls_en_carpeta": 0,
            "xmls_parseados": 0,
            "facturas": 0,
            "lineas_match": 0,
            "lineas_sin_match": 0,
        }

    filas: list[tuple[str, str, dict]] = []
    for archivo in xmls:
        try:
            texto = descargar_xml(archivo["id"])
        except Exception as e:
            print(f"  WARN descarga {archivo.get('name')}: {e}")
            continue
        factura = parsear_xml_sri(texto)
        if not factura:
            print(f"  WARN parseo {archivo.get('name')}")
            continue
        factura["_archivo_drive"] = archivo
        fecha = (factura.get("fecha_factura") or "").strip()
        num = (factura.get("num_factura") or "").strip()
        filas.append((fecha, num, factura))

    # Sin fecha al final para no pisar datos buenos con orden ambiguo
    filas.sort(key=lambda t: (t[0] or "9999-99-99", t[1]))

    _items_prov_cache = None
    cargar_bd_items_prov()

    layout = _bd_items_prov_price_layout
    if not dry_run and layout is None:
        print("  ERROR: no se pudo cargar layout de precios en BD_ITEMS_PROV (revisar columnas).")
        return {
            "xmls_en_carpeta": len(xmls),
            "xmls_parseados": len(filas),
            "facturas": len(filas),
            "lineas_match": 0,
            "lineas_sin_match": 0,
            "filas_escritas": 0,
        }

    lineas_match = 0
    lineas_sin_match = 0
    by_row: dict[int, tuple[float, float, str]] = {}

    for fecha, num, factura in filas:
        print(f"\n--- {num} | {fecha} | items={len(factura.get('items') or [])}")
        for item in factura.get("items") or []:
            item_prov = buscar_item_prov(
                factura["ruc"],
                item["cod_item_xml"],
                item["descripcion_proveedor"],
                factura.get("razon_social", ""),
            )
            if not item_prov:
                lineas_sin_match += 1
                continue
            lineas_match += 1
            cod = (item_prov.get("cod_item_prov") or "").strip()
            if dry_run:
                print(
                    f"  [DRY] {cod} <- costo_efectivo={item['costo_efectivo']} "
                    f"xml_u={item['precio_unitario_xml']}"
                )
                continue

            filas_precio = _filas_precio_bd_items_prov_1based(item_prov, layout)
            if not filas_precio:
                print(
                    f"  WARN sin fila en hoja para item (cod_item_prov={cod!r}) "
                    "(revisar duplicados o cabecera BD_ITEMS_PROV)"
                )
                continue

            precio_tuple = (
                _precio_ref_unidad_base(item_prov, item["costo_efectivo"]),
                item["precio_unitario_xml"],
                factura["fecha_factura"],
            )
            for row_1based in filas_precio:
                by_row[row_1based] = precio_tuple

    filas_escritas = 0
    if not dry_run and by_row and layout is not None:
        filas_escritas = _batch_update_precios_items_prov_cells(
            layout["ws"],
            layout["col_precio_ref"],
            layout["col_precio_xml"],
            layout["col_fecha"],
            by_row,
        )
        print(f"\n  Escritura lote: {filas_escritas} filas (ultima factura por item gana).")

    _items_prov_cache = None
    _invalidar_cache_layout_precio_items_prov()
    print("\n" + "=" * 50)
    print(
        f"Listo. XMLs parseados={len(filas)} | lineas con match={lineas_match} | "
        f"sin match (omitidas)={lineas_sin_match}"
    )
    return {
        "xmls_en_carpeta": len(xmls),
        "xmls_parseados": len(filas),
        "facturas": len(filas),
        "lineas_match": lineas_match,
        "lineas_sin_match": lineas_sin_match,
        "filas_escritas": filas_escritas if not dry_run else 0,
    }


# ── FLUJO PRINCIPAL ───────────────────────────────────────────
def procesar_facturas(dry_run: bool = False, reprocesar: bool = False) -> dict:
    if reprocesar and not dry_run:
        print(
            "MODO --reprocesar: se ignoran facturas ya COMPLETA; "
            "puede duplicar mov_inventario, precios y stock en BD."
        )
    xmls = listar_xmls_pendientes()
    sin_xmls = len(xmls) == 0
    print(f"XMLs en Drive: {len(xmls)}")
    print(
        f"Ingreso stock/mov: solo facturas con fecha >= {FECHA_MIN_INGRESO_FACTURA} "
        "(TATAMI_FECHA_MIN_INGRESO_FACTURA)."
    )

    global _items_pendientes_cache_keys, _items_pendientes_ignorados_por_item
    _items_pendientes_cache_keys = None
    _items_pendientes_ignorados_por_item = None

    cargar_bd_items_prov()
    cargar_lookup_ruc()

    xmls_parseados = 0
    xmls_saltados = 0
    total_matcheados = 0
    total_warn = 0
    completas = 0
    parciales = 0
    total_usd = 0.0
    sin_match: list[dict[str, str]] = []

    for archivo in xmls:
        print(f"\n{'-' * 50}")
        print(f"Procesando: {archivo['name']}")

        texto = descargar_xml(archivo["id"])
        factura = parsear_xml_sri(texto)

        if not factura:
            print("  ERROR: no se pudo parsear, saltando")
            continue

        # ── Deduplicación ──────────────────────────────────────
        if (
            not dry_run
            and not reprocesar
            and factura_ya_procesada(factura["num_factura"], factura["ruc"])
        ):
            xmls_saltados += 1
            factura["_archivo_drive"] = archivo
            nuevos_p, ya_p = sincronizar_items_pendientes_factura_drive(
                factura, archivo, dry_run=dry_run
            )
            num_f = (factura.get("num_factura") or "").strip()
            ruc_f = (factura.get("ruc") or "").strip()
            print(
                f"  SKIP: factura ya COMPLETA en facturas_procesadas — {num_f} | RUC {ruc_f}. "
                "No se re-ejecuta mov/precios/stock desde este XML."
            )
            print(
                f"  INFO: {BD_ITEMS_PENDIENTES_SHEET} (solo líneas sin match en catálogo): "
                f"+{nuevos_p} filas nuevas, {ya_p} claves ya existían (idempotente). "
                "Las líneas con match en BD_ITEMS_PROV no van a pendientes por diseño."
            )
            continue

        xmls_parseados += 1

        # Adjuntar metadata Drive para la hoja de pendientes (sin cambiar parsear_xml_sri)
        factura["_archivo_drive"] = archivo

        resultado = procesar_factura_dict(factura, dry_run=dry_run, origen="XML")

        lineas_sin_match = resultado.get("sin_match") or []
        descs_alerta = _sin_match_entradas_para_alerta(lineas_sin_match)
        if not dry_run and len(descs_alerta) > 0 and _alertas_disponibles:
            try:
                mo = (os.getenv("ALERTA_WA_MOISES") or "").strip()
                if mo and _enviar_whatsapp_texto:
                    proveedor = (factura.get("razon_social") or "").strip() or "(sin proveedor)"
                    num_fac = (factura.get("num_factura") or "").strip() or "(sin número)"
                    n = len(descs_alerta)
                    bullets = _lineas_whatsapp_items_sin_match(factura, descs_alerta)
                    cuerpo = (
                        "⚠️ Factura sin mapear completo\n"
                        f"Proveedor: {proveedor}\n"
                        f"Factura: {num_fac}\n"
                        f"Ítems sin match ({n}):\n"
                        + "\n".join(bullets)
                        + "\n\nAcción: completar BD_ITEMS_PROV y volver a correr."
                    )
                    ok, msg = _enviar_whatsapp_texto(mo, cuerpo)
                    if not ok:
                        print(f"  WARN: WA sin_match factura {num_fac}: {msg}")
            except Exception as e:
                print(f"  WARN: WA sin_match factura: {e}")

        items_matcheados = int(resultado.get("matcheados") or 0)
        items_warn = len(resultado.get("warn") or []) + _sin_match_count_alerta(
            resultado.get("sin_match") or []
        )

        est = (resultado.get("estado") or "PARCIAL").strip().upper()
        if est == "COMPLETA":
            completas += 1
        else:
            parciales += 1
        for it in resultado.get("sin_match") or []:
            if isinstance(it, dict):
                d = (it.get("descripcion") or "").strip()
                e = (it.get("estado") or "PENDIENTE").strip().upper()
                if d and e not in ("IGNORADO", "REGISTRADO"):
                    sin_match.append({"descripcion": d, "estado": e})
            else:
                d = str(it).strip()
                if d:
                    sin_match.append({"descripcion": d, "estado": "PENDIENTE"})
        total_usd += _safe_float(factura.get("total_sin_impuesto"))

        # ── Registrar en facturas_procesadas ──────────────────
        registrar_factura_procesada(
            factura, archivo, items_matcheados, items_warn, dry_run
        )

        total_matcheados += items_matcheados
        total_warn += items_warn

    print(f"\n{'=' * 50}")
    print("Completado.")
    print(
        f"Resumen: XMLs procesados={xmls_parseados} | saltados (COMPLETA)={xmls_saltados} | "
        f"items matcheados={total_matcheados} | WARN sin match={total_warn}"
    )
    print(
        f"\n>>> FACTURAS DRIVE: en_carpeta={len(xmls)} | "
        f"aplicados_esta_corrida={xmls_parseados} | omitidos_ya_COMPLETA={xmls_saltados}"
    )
    if len(xmls) > 0 and xmls_parseados == 0:
        if xmls_saltados >= len(xmls):
            print(
                "    AVISO: Todos los XML estaban ya como COMPLETA en facturas_procesadas; "
                "no se insertan movimientos otra vez. Si falta una entrada, revisar esa tabla "
                "o usar procesar_facturas_drive.py --reprocesar (riesgo de duplicar)."
            )
        elif xmls_saltados > 0:
            print(
                f"    AVISO: Ninguna factura aplicada: {xmls_saltados} omitidas (COMPLETA); "
                f"{len(xmls) - xmls_saltados} archivo(s) no procesados (revisar errores de parseo arriba)."
            )
        else:
            print(
                "    AVISO: Hay XML en carpeta pero ninguno entro al flujo (revisar errores "
                "de parseo arriba o conexion a Supabase)."
            )
    elif len(xmls) == 0:
        print(
            "    AVISO: Cero XML listados (carpeta vacia, GOOGLE_DRIVE_FACTURAS_FOLDER_ID, "
            "o permisos de la cuenta de servicio)."
        )

    resumen = {
        "xmls_en_carpeta": len(xmls),
        "xmls_omitidos_completa": xmls_saltados,
        "total_procesadas": xmls_parseados,
        "completas": completas,
        "parciales": parciales,
        "sin_xmls": sin_xmls,
        "total_usd": round(total_usd, 2),
        "sin_match": sin_match,
    }
    return resumen


# ── HELPERS ───────────────────────────────────────────────────
def _txt(el, tag: str) -> str:
    if el is None:
        return ""
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0


def _fecha_a_iso(fecha: str) -> str:
    fecha = fecha.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(fecha, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return fecha


def procesar_factura_dict(
    factura: dict,
    dry_run: bool = False,
    origen: str = "XML",  # "XML" | "VISION"
    lineas_duda: set[int] | None = None,
) -> dict:
    """
    Procesa una factura ya parseada (dict con keys: num_factura,
    ruc, items, fecha_emision, etc.).
    Retorna:
    {
        "estado": "COMPLETA" | "PARCIAL",
        "matcheados": int,
        "sin_match": list[dict[str, str]],  # descripcion + estado (pendientes hoja)
        "warn": list[str],
    }
    """
    origen = (origen or "XML").strip().upper() or "XML"
    if origen not in ("XML", "VISION"):
        origen = "XML"

    print(f"  Proveedor:  {factura.get('razon_social')} ({factura.get('ruc')})")
    print(f"  Factura:    {factura.get('num_factura')} | {factura.get('fecha_factura')}")
    print(f"  Total:      ${factura.get('total_sin_impuesto')}")
    print(f"  Items:      {len(factura.get('items') or [])}")

    from bodegas_config import (
        clave_linea_factura,
        nombre_bodega,
        resolver_bodega_entrada_linea,
    )

    sin_match: list[dict[str, str]] = []
    warns: list[str] = []
    pendiente_bodega: list[dict] = []
    items_matcheados = 0

    lookup_ruc = cargar_lookup_ruc()
    cod_prov_factura = _cod_proveedor_desde_ruc(lookup_ruc, str(factura.get("ruc", "")).strip())
    archivo = factura.get("_archivo_drive") or {"id": "", "name": ""}

    bodegas_linea: dict[str, str] = factura.get("bodegas_linea") or {}
    bodegas_confirmadas: bool = bool(factura.get("bodegas_confirmadas"))

    deltas_stock: dict[tuple[str, str], float] = {}
    deltas_costo: dict[tuple[str, str], float] = {}
    lineas_duda_set = set(lineas_duda or set())
    items_duda_operador = 0

    for item in factura.get("items", []) or []:
        print(f"\n  Item: {item['cod_item_xml']} - {item['descripcion_proveedor']}")
        print(f"    cantidad={item['cantidad']} | costo_efectivo={item['costo_efectivo']}")

        linea_n = item.get("linea")
        if lineas_duda_set and linea_n is not None:
            try:
                en_duda = int(linea_n) in lineas_duda_set
            except (TypeError, ValueError):
                en_duda = False
        else:
            en_duda = False

        if en_duda:
            print(
                "    DUDA (operador): va a BD_ITEMS_PENDIENTES — "
                "sin entrada inventario ni actualización de precios."
            )
            registrar_item_pendiente_factura(
                factura,
                item,
                archivo,
                cod_prov_factura,
                dry_run=dry_run,
                motivo_pendiente="DUDA_OPERADOR",
                origen_factura=origen,
            )
            sin_match.append(
                {
                    "descripcion": item["descripcion_proveedor"],
                    "estado": "DUDA_OPERADOR",
                    "linea": str(linea_n),
                }
            )
            items_duda_operador += 1
            continue

        item_prov = buscar_item_prov(
            factura["ruc"],
            item["cod_item_xml"],
            item["descripcion_proveedor"],
            factura.get("razon_social", ""),
        )

        if not item_prov:
            msg = (
                f"no encontrado en BD_ITEMS_PROV | ruc={factura['ruc']} | "
                f"cod={item['cod_item_xml']} | desc={item['descripcion_proveedor']}"
            )
            print(f"    WARN: {msg}")
            registrar_item_pendiente_factura(
                factura,
                item,
                archivo,
                cod_prov_factura,
                dry_run=dry_run,
                motivo_pendiente="SIN_MATCH",
                origen_factura=origen,
            )
            clave_item_chk = f"{item['cod_item_xml'].strip()}|{factura['ruc'].strip()}"
            if clave_item_chk in (_items_pendientes_ignorados_por_item or {}):
                continue
            clave = _clave_item_pendiente(factura, item)
            estado_row = ""
            if _items_pendientes_cache_keys is not None:
                estado_row = (_items_pendientes_cache_keys.get(clave) or "").strip().upper()
            if estado_row not in ("IGNORADO", "REGISTRADO"):
                sin_match.append(
                    {
                        "descripcion": item["descripcion_proveedor"],
                        "estado": estado_row or "PENDIENTE",
                    }
                )
            continue

        items_matcheados += 1
        cod_mp = item_prov.get("cod_mp_sistema", "").strip()
        print(f"    Match: {cod_mp} - {item_prov.get('nombre_mp')}")

        if not cod_mp:
            msg = "fila en BD_ITEMS_PROV sin cod_mp_sistema (no hay mov ni costo en BD_MP_SISTEMA)"
            print(f"    WARN: {msg}")
            warns.append(msg)
            if not dry_run:
                procesar_variacion_precio(item_prov, factura, item)
            continue

        # Para movimientos: agregamos ORIGEN a observaciones sin tocar registrar_entrada_inventario.
        # registrar_entrada_inventario arma observaciones desde descripcion_proveedor + ITEM_XML.
        item_mov = dict(item)
        item_mov["descripcion_proveedor"] = (
            f"{item.get('descripcion_proveedor', '').strip()} | ORIGEN:{origen}"
        )

        clave_ln = clave_linea_factura(factura["ruc"], item["cod_item_xml"])
        bodega_override = (bodegas_linea.get(clave_ln) or "").strip() or None

        bodega_dest, err_bod = resolver_bodega_entrada_linea(
            item_prov,
            bodega_override=bodega_override,
            confirmada=bodegas_confirmadas and bool(bodega_override),
        )
        if err_bod == "REQUIERE_CONFIRMACION":
            pendiente_bodega.append(
                {
                    "clave": clave_ln,
                    "descripcion": item["descripcion_proveedor"],
                    "cod_mp": cod_mp,
                    "default": (item_prov.get("cod_bodega_destino") or "").strip(),
                    "solicitada": bodega_override,
                }
            )
            print(
                f"    PENDIENTE BODEGA: default {nombre_bodega(item_prov.get('cod_bodega_destino'))} "
                f"≠ solicitada {nombre_bodega(bodega_override)} — confirme con responsable"
            )
            if not dry_run:
                procesar_variacion_precio(item_prov, factura, item)
            continue
        if err_bod or not bodega_dest:
            msg = f"BODEGA: {err_bod or 'sin destino'} — ítem {item['cod_item_xml']}"
            print(f"    WARN: {msg}")
            warns.append(msg)
            if not dry_run:
                procesar_variacion_precio(item_prov, factura, item)
            continue

        if mov_entrada_factura_linea_ya_registrada(
            factura["num_factura"], cod_mp, item_mov
        ):
            print(
                "    INFO: esta línea ya tiene ENTRADA en mov_inventario — "
                "no se duplica mov/stock; se actualizan igual precios en BD_ITEMS_PROV desde el XML."
            )
            if not dry_run:
                procesar_variacion_precio(item_prov, factura, item)
            continue

        if not fecha_factura_permite_ingreso_stock(factura.get("fecha_factura", "")):
            print(
                f"    SKIP INGRESO: fecha factura {factura.get('fecha_factura')} "
                f"< {FECHA_MIN_INGRESO_FACTURA} — sin mov/stock "
                f"(protege inventario barra/cocina ya cuadrado)."
            )
            if not dry_run:
                procesar_variacion_precio(item_prov, factura, item)
            continue

        ok_conv, motivo_conv = conversion_compra_definida(item_prov)
        if not ok_conv:
            msg = (
                f"ALERTA INVENTARIO: {motivo_conv} — no se registra entrada ni "
                "stock/costo en BD_MP_SISTEMA hasta corregir factor y unidad_compra."
            )
            print(f"    {msg}")
            print(
                "    INFO: se actualiza igual precio_ref / precio_unitario_xml / fecha en BD_ITEMS_PROV desde la factura."
            )
            warns.append(motivo_conv)
            if not dry_run:
                procesar_variacion_precio(item_prov, factura, item)
            continue

        if dry_run:
            u_compra = (item_prov.get("unidad_compra") or "").strip()
            factor = _parse_factor_positivo(item_prov.get("factor_conversion"))
            assert factor is not None
            cantidad_base = item["cantidad"] * factor
            costo_u = item["costo_efectivo"] / factor if factor else 0
            print(
                f"    [DRY RUN] precio_ref={item_prov.get('precio_ref')} -> nuevo={item['costo_efectivo']}"
            )
            print(
                f"    [DRY RUN] entrada inventario: {cod_mp} +{round(cantidad_base,4)} {u_compra}"
            )
            print(
                f"    [DRY RUN] BD_MP_SISTEMA: stock_actual +{round(cantidad_base,4)} | costo_unitario_ref={round(costo_u,6)}"
            )
        else:
            procesar_variacion_precio(item_prov, factura, item)
            ok = registrar_entrada_inventario(
                item_prov,
                item_mov,
                factura,
                cod_bodega_destino=bodega_dest,
            )
            if ok and cod_mp:
                factor = _parse_factor_positivo(item_prov.get("factor_conversion"))
                assert factor is not None
                cantidad_base = item["cantidad"] * factor
                key = _mp_cache_key(cod_mp, bodega_dest)
                deltas_stock[key] = deltas_stock.get(key, 0.0) + cantidad_base
                costo_u = item["costo_efectivo"] / factor if factor else 0
                deltas_costo[key] = costo_u

    if not dry_run and (deltas_stock or deltas_costo):
        _flush_mp_sistema(deltas_stock, deltas_costo)

    if not dry_run and pendiente_bodega:
        factura["_pendiente_bodega"] = pendiente_bodega
        try:
            from alertas_tatami import alerta_wa_factura_bodega_pendiente

            alerta_wa_factura_bodega_pendiente(factura, pendiente_bodega)
        except Exception as e:
            print(f"  WARN: alerta bodega pendiente: {e}")

    estado = "COMPLETA"
    if _sin_match_count_alerta(sin_match) > 0 or warns or pendiente_bodega:
        estado = "PARCIAL"
    return {
        "estado": estado,
        "matcheados": items_matcheados,
        "sin_match": sin_match,
        "warn": warns,
        "pendiente_bodega": pendiente_bodega,
        "lineas_duda": items_duda_operador,
    }


if __name__ == "__main__":
    import sys

    if "--crear-hoja-items-pendientes" in sys.argv:
        print("Creando pestaña de ítems pendientes (si no existe)...")
        crear_hoja_bd_items_pendientes()
        sys.exit(0)

    if "--reparar-hoja-items-pendientes" in sys.argv:
        print("Reparando cabecera BD_ITEMS_PENDIENTES...")
        r = reparar_bd_items_pendientes_cabecera()
        print(r)
        sys.exit(0 if r.get("ok") else 1)

    if "--backfill-items-pendientes" in sys.argv:
        dry = "--dry-run" in sys.argv
        print(
            "Backfill BD_ITEMS_PENDIENTES desde XML en Drive "
            f"({'DRY RUN' if dry else 'escribiendo hoja'})..."
        )
        backfill_items_pendientes_desde_drive(dry_run=dry)
        sys.exit(0)

    if "--backfill-costo-total-pendientes" in sys.argv:
        dry = "--dry-run" in sys.argv
        desde = FECHA_MIN_COSTO_TOTAL_PENDIENTES
        for i, arg in enumerate(sys.argv):
            if arg == "--desde-fecha" and i + 1 < len(sys.argv):
                desde = sys.argv[i + 1].strip()
        print(
            f"Backfill {COL_COSTO_TOTAL_PENDIENTES} en BD_ITEMS_PENDIENTES "
            f"(desde {desde}) {'DRY RUN' if dry else 'escribiendo'}..."
        )
        backfill_costo_total_pendientes_desde_xml(desde_fecha=desde, dry_run=dry)
        sys.exit(0)

    if "--solo-precios-desde-xml" in sys.argv:
        dry = "--dry-run" in sys.argv
        print(
            "Modo --solo-precios-desde-xml: solo BD_ITEMS_PROV (3 columnas de precio); "
            "sin mov_inventario / BD_MP_SISTEMA / facturas_procesadas.\n"
        )
        sincronizar_precios_items_prov_desde_todos_xml_drive(dry_run=dry)
        sys.exit(0)

    DRY_RUN = "--dry-run" in sys.argv
    REPROCESAR = "--reprocesar" in sys.argv

    print("=" * 50)
    tag = "DRY RUN" if DRY_RUN else "PRODUCCION"
    if REPROCESAR:
        tag += " + REPROCESAR"
    print(f"MODULO FACTURAS - {tag}")
    print("=" * 50)
    procesar_facturas(dry_run=DRY_RUN, reprocesar=REPROCESAR)
