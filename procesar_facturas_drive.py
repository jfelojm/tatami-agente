import os
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from gspread.utils import ValueInputOption, rowcol_to_a1
from supabase import create_client

from codigo_factura_match import normalizar_cod_item_para_match
from config_sheets import cfg

load_dotenv(override=True)

# Hoja maestra: ítems de factura XML sin match en BD_ITEMS_PROV (para alta manual / MP).
BD_ITEMS_PENDIENTES_SHEET = "BD_ITEMS_PENDIENTES"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── GOOGLE DRIVE ──────────────────────────────────────────────
def _get_drive_service():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _get_sheet():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID"))


def listar_xmls_pendientes() -> list[dict]:
    """Lista XMLs en la carpeta de Drive."""
    service = _get_drive_service()
    folder_id = os.getenv("GOOGLE_DRIVE_FACTURAS_FOLDER_ID")
    if not folder_id:
        return []
    q = (
        f"'{folder_id}' in parents and trashed=false "
        "and (mimeType='text/xml' or mimeType='application/xml')"
    )
    results = (
        service.files()
        .list(q=q, fields="files(id,name,createdTime)")
        .execute()
    )
    return results.get("files", [])


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


def cargar_bd_items_prov() -> list[dict]:
    global _items_prov_cache
    if _items_prov_cache is not None:
        return _items_prov_cache

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
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        r = {
            headers[j].strip(): row[j].strip()
            for j in range(min(len(headers), len(row)))
        }
        if r.get("activo", "SI") != "NO":
            result.append(r)

    print(f"  {len(result)} items cargados en BD_ITEMS_PROV")
    _items_prov_cache = result
    return result


_prov_ruc_cache = None  # ruc -> cod_proveedor


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
            lookup[ruc] = cod

    print(f"  {len(lookup)} proveedores en lookup RUC")
    _prov_ruc_cache = lookup
    return lookup


def buscar_item_prov(
    ruc: str, cod_item_xml: str, descripcion: str = "", razon_social: str = ""
) -> dict | None:
    items = cargar_bd_items_prov()

    lookup_ruc = cargar_lookup_ruc()
    cod_prov = lookup_ruc.get(ruc.strip(), "")

    cod_item_norm = normalizar_cod_item_para_match(cod_item_xml, razon_social, ruc)

    for item in items:
        if item.get("cod_proveedor", "").strip() != cod_prov:
            continue
        item_cod = normalizar_cod_item_para_match(
            item.get("cod_item_prov", ""), razon_social, ruc
        )
        if item_cod == cod_item_norm:
            return item

    for item in items:
        item_cod = normalizar_cod_item_para_match(
            item.get("cod_item_prov", ""), razon_social, ruc
        )
        if item_cod == cod_item_norm:
            return item

    if descripcion:
        desc_upper = descripcion.upper()
        for item in items:
            desc = item.get("descripcion_proveedor", "").upper()
            if desc and desc in desc_upper:
                return item

    return None


# ── LÓGICA DE PRECIOS ─────────────────────────────────────────
def procesar_variacion_precio(item_prov: dict, factura: dict, item_factura: dict):
    cod_catalogo = item_prov.get("cod_item_prov", "")
    precio_ref_str = item_prov.get("precio_ref", "").strip()
    costo_efectivo = item_factura["costo_efectivo"]
    precio_u_xml = item_factura["precio_unitario_xml"]

    umbral = float(cfg("umbral_alerta_precio", os.getenv("UMBRAL_ALERTA_PRECIO", "0.05")))

    if not precio_ref_str:
        print(f"    Primera factura para {cod_catalogo} → precio_ref={costo_efectivo}")
        _actualizar_precio_ref(
            item_prov, costo_efectivo, precio_u_xml, factura["fecha_factura"]
        )
        return

    precio_ref = float(precio_ref_str.replace(",", "."))
    if precio_ref == 0:
        _actualizar_precio_ref(
            item_prov, costo_efectivo, precio_u_xml, factura["fecha_factura"]
        )
        return

    variacion = (costo_efectivo - precio_ref) / precio_ref

    if abs(variacion) > umbral:
        print(
            f"    ALERTA precio {cod_catalogo}: {precio_ref} → {costo_efectivo} ({variacion:.1%})"
        )
        _escribir_hist_precios(item_prov, factura, item_factura, precio_ref, variacion)
        _actualizar_precio_ref(
            item_prov, costo_efectivo, precio_u_xml, factura["fecha_factura"]
        )
    else:
        print(f"    Precio estable {cod_catalogo}: variación={variacion:.2%}")


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
        "precio_nuevo": item_factura["costo_efectivo"],
        "precio_unitario": item_factura["precio_unitario_xml"],
        "variacion_pct": round(variacion, 6),
        "estado": "PENDIENTE",
        "observaciones": "Variación detectada automáticamente",
    }
    try:
        supabase.table("hist_precios").insert(registro).execute()
        print(f"    → hist_precios registrado: {cod_hist}")
    except Exception as e:
        print(f"    ERROR insertando hist_precios: {e}")


def _actualizar_precio_ref(item_prov, costo_efectivo, precio_u_xml, fecha):
    sh = _get_sheet()
    ws = sh.worksheet("BD_ITEMS_PROV")
    values = ws.get_all_values()

    header_row_idx = next(
        (i for i, r in enumerate(values) if any(c.strip() == "cod_item_prov" for c in r)),
        None,
    )
    if header_row_idx is None:
        return

    headers = values[header_row_idx]
    try:
        idx_cod = headers.index("cod_item_prov")
        col_precio_ref = headers.index("precio_ref") + 1
        col_precio_xml = headers.index("precio_unitario_xml") + 1
        col_fecha = headers.index("fecha_precio_ref") + 1
    except ValueError as e:
        print(f"  WARN columna no encontrada en BD_ITEMS_PROV: {e}")
        return

    cod_buscar = item_prov.get("cod_item_prov", "").strip()
    for i, row in enumerate(values[header_row_idx + 1 :]):
        if len(row) > idx_cod and row[idx_cod].strip() == cod_buscar:
            row_1based = header_row_idx + i + 2
            ws.batch_update(
                [
                    {
                        "range": rowcol_to_a1(row_1based, col_precio_ref),
                        "values": [[costo_efectivo]],
                    },
                    {
                        "range": rowcol_to_a1(row_1based, col_precio_xml),
                        "values": [[precio_u_xml]],
                    },
                    {
                        "range": rowcol_to_a1(row_1based, col_fecha),
                        "values": [[fecha]],
                    },
                ],
                value_input_option=ValueInputOption.user_entered,
            )
            print(f"    → precio_ref actualizado en Sheets: {cod_buscar} = {costo_efectivo}")
            return


# ── ACTUALIZAR BD_MP_SISTEMA (stock + costo) ─────────────────
_mp_sistema_cache: dict | None = None  # cod_mp -> {row_1based, stock_actual, headers...}
_mp_col_stock: int | None = None
_mp_col_costo: int | None = None


def _cargar_mp_sistema_cache():
    """Carga BD_MP_SISTEMA una sola vez por ejecución."""
    global _mp_sistema_cache, _mp_col_stock, _mp_col_costo
    if _mp_sistema_cache is not None:
        return

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
        _mp_col_stock = headers.index("stock_actual") + 1
    except ValueError as e:
        print(f"  WARN columna no encontrada en BD_MP_SISTEMA: {e}")
        _mp_sistema_cache = {}
        return

    # costo_unitario_ref es opcional — no bloquea si no existe
    try:
        _mp_col_costo = headers.index("costo_unitario_ref") + 1
    except ValueError:
        _mp_col_costo = None
        print("  WARN: columna costo_unitario_ref no encontrada en BD_MP_SISTEMA — se omite")

    cache = {}
    for i, row in enumerate(values[header_row_idx + 1:]):
        if not any(c.strip() for c in row):
            continue
        cod = row[idx_cod].strip() if len(row) > idx_cod else ""
        if not cod:
            continue
        stock_str = row[_mp_col_stock - 1].strip() if len(row) >= _mp_col_stock else "0"
        try:
            stock = float(stock_str.replace(",", ".")) if stock_str else 0.0
        except ValueError:
            stock = 0.0
        cache[cod] = {
            "row_1based": header_row_idx + i + 2,
            "stock_actual": stock,
        }

    _mp_sistema_cache = cache
    print(f"  {len(cache)} MPs cargados en cache BD_MP_SISTEMA")


def _flush_mp_sistema(deltas_stock: dict[str, float], deltas_costo: dict[str, float]):
    """
    Aplica batch_update a BD_MP_SISTEMA con los acumulados de la factura.
    deltas_stock: cod_mp -> cantidad a SUMAR al stock_actual
    deltas_costo: cod_mp -> nuevo costo_unitario_ref (reemplaza, no suma)
    """
    if not deltas_stock and not deltas_costo:
        return
    _cargar_mp_sistema_cache()
    if not _mp_sistema_cache:
        return

    sh = _get_sheet()
    ws = sh.worksheet("BD_MP_SISTEMA")
    updates = []

    for cod_mp, delta in deltas_stock.items():
        info = _mp_sistema_cache.get(cod_mp)
        if not info:
            print(f"  WARN: cod_mp={cod_mp} no encontrado en BD_MP_SISTEMA cache")
            continue
        nuevo_stock = round(info["stock_actual"] + delta, 4)
        _mp_sistema_cache[cod_mp]["stock_actual"] = nuevo_stock
        updates.append({
            "range": rowcol_to_a1(info["row_1based"], _mp_col_stock),
            "values": [[nuevo_stock]],
        })
        print(f"    -> stock_actual {cod_mp}: +{round(delta,4)} => {nuevo_stock}")

    if _mp_col_costo:
        for cod_mp, nuevo_costo in deltas_costo.items():
            info = _mp_sistema_cache.get(cod_mp)
            if not info:
                continue
            updates.append({
                "range": rowcol_to_a1(info["row_1based"], _mp_col_costo),
                "values": [[round(nuevo_costo, 6)]],
            })
            print(f"    -> costo_unitario_ref {cod_mp}: {round(nuevo_costo, 6)}")

    if not updates:
        return

    batch_size = 50
    for i in range(0, len(updates), batch_size):
        ws.batch_update(
            updates[i: i + batch_size],
            value_input_option=ValueInputOption.user_entered,
        )
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


def registrar_entrada_inventario(item_prov: dict, item_factura: dict, factura: dict):
    cod_mp = item_prov.get("cod_mp_sistema", "").strip()
    bodega = item_prov.get("cod_bodega_destino", "").strip()
    unidad = item_prov.get("unidad_base_sistema", "").strip()
    factor = _safe_float(item_prov.get("factor_conversion", "1") or "1")
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
            f"    → mov_inventario ENTRADA: {cod_mp} +{round(cantidad_base, 2)} {unidad}"
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
        print(f"  [DRY RUN] facturas_procesadas → {estado} (matcheados={items_matcheados}, sin_match={items_warn})")
        return

    estado = "COMPLETA" if items_warn == 0 else "PARCIAL"
    registro = {
        "num_factura": factura["num_factura"],
        "ruc_proveedor": factura["ruc"],
        "drive_file_id": archivo.get("id", ""),
        "fecha_factura": factura["fecha_factura"],
        "fecha_proceso": datetime.now().isoformat(),
        "items_procesados": items_matcheados,
        "items_sin_match": items_warn,
        "estado": estado,
    }
    try:
        # Upsert: si ya existe (era PARCIAL) actualiza, si no existe inserta
        supabase.table("facturas_procesadas").upsert(
            registro,
            on_conflict="num_factura,ruc_proveedor"
        ).execute()
        print(f"  → facturas_procesadas: {estado} (matcheados={items_matcheados}, sin_match={items_warn})")
    except Exception as e:
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


_items_pendientes_cache_keys: set[str] | None = None


def _pendientes_load_keys(ws: gspread.Worksheet) -> set[str]:
    """Claves ya registradas (columna clave_unica)."""
    vals = ws.get_all_values()
    if len(vals) < 2:
        return set()
    # Columna A = índice 0
    out = set()
    for row in vals[1:]:
        if row and (row[0] or "").strip():
            out.add((row[0] or "").strip())
    return out


def _ensure_bd_items_pendientes_sheet(sh):
    """Crea la hoja y cabeceras si no existe."""
    global _items_pendientes_cache_keys
    try:
        ws = sh.worksheet(BD_ITEMS_PENDIENTES_SHEET)
        if _items_pendientes_cache_keys is None:
            _items_pendientes_cache_keys = _pendientes_load_keys(ws)
        return ws
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=BD_ITEMS_PENDIENTES_SHEET, rows=2000, cols=26
        )
        headers = [
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
        ]
        ws.update(
            range_name="A1:V1",
            values=[headers],
            value_input_option=ValueInputOption.user_entered,
        )
        print(f"  [BD_ITEMS_PENDIENTES] Hoja creada: {BD_ITEMS_PENDIENTES_SHEET}")
        _items_pendientes_cache_keys = set()
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
) -> bool:
    """
    Registra una línea en BD_ITEMS_PENDIENTES para ítems sin match en BD_ITEMS_PROV.
    Idempotente por clave_unica (no duplica si ya existe).
    Retorna True si se insertó una fila nueva.
    """
    if dry_run:
        print(
            f"    [DRY RUN] registraría en {BD_ITEMS_PENDIENTES_SHEET}: "
            f"{item['cod_item_xml']} — {item['descripcion_proveedor'][:60]}"
        )
        return False

    sh = _get_sheet()
    ws = _ensure_bd_items_pendientes_sheet(sh)
    global _items_pendientes_cache_keys
    if _items_pendientes_cache_keys is None:
        _items_pendientes_cache_keys = _pendientes_load_keys(ws)

    clave = _clave_item_pendiente(factura, item)
    if clave in _items_pendientes_cache_keys:
        print(f"    INFO: ya listado en {BD_ITEMS_PENDIENTES_SHEET} — {clave[:40]}...")
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

    allv = ws.get_all_values()
    next_row = len(allv) + 1

    fecha_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    drive_id = (archivo.get("id") or "").strip()
    nombre_xml = (archivo.get("name") or "").strip()

    # Fórmulas: referencia BD_MP_SISTEMA por cod_mp en columna P (16)
    # nombre: INDEX/MATCH ; unidad: INDEX/MATCH ; link Drive
    p_col = "P"
    fq_nom = (
        f'=IF(${p_col}{next_row}="","", IFERROR(INDEX(BD_MP_SISTEMA!${col_nom_l}:${col_nom_l}, '
        f'MATCH(${p_col}{next_row}, BD_MP_SISTEMA!${col_cod_l}:${col_cod_l}, 0)), "NO EN BD"))'
    )
    fq_uni = (
        f'=IF(${p_col}{next_row}="","", IFERROR(INDEX(BD_MP_SISTEMA!${col_uni_l}:${col_uni_l}, '
        f'MATCH(${p_col}{next_row}, BD_MP_SISTEMA!${col_cod_l}:${col_cod_l}, 0)), ""))'
    )
    # drive_file_id está en columna M (13)
    fq_link = (
        f'=IF(M{next_row}="","", HYPERLINK("https://drive.google.com/file/d/" & M{next_row} '
        f'& "/view", "Ver XML"))'
    )
    # Ayuda para copiar a BD_ITEMS_PROV (mismo valor que columnas H / P / I)
    fq_pcod = f"=H{next_row}"
    fq_pmp = f"=P{next_row}"
    fq_pdesc = f"=I{next_row}"

    row_vals = [
        clave,
        fecha_iso,
        factura["fecha_factura"],
        factura["num_factura"],
        factura["ruc"],
        factura.get("razon_social", ""),
        cod_proveedor,
        item["cod_item_xml"],
        item["descripcion_proveedor"],
        str(item["cantidad"]).replace(".", ","),
        str(item["precio_unitario_xml"]).replace(".", ","),
        str(item["costo_efectivo"]).replace(".", ","),
        drive_id,
        nombre_xml,
        "PENDIENTE",
        "",  # cod_mp_asignado — usuario completa
        fq_nom,
        fq_uni,
        fq_link,
        fq_pcod,
        fq_pmp,
        fq_pdesc,
    ]

    ws.append_row(row_vals, value_input_option=ValueInputOption.user_entered)
    _items_pendientes_cache_keys.add(clave)
    print(f"    → {BD_ITEMS_PENDIENTES_SHEET}: registrado pendiente {item['cod_item_xml']}")
    return True


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


def backfill_items_pendientes_desde_drive(*, dry_run: bool = False) -> dict[str, int]:
    """
    Recorre todos los XML en GOOGLE_DRIVE_FACTURAS_FOLDER_ID, detecta líneas sin match
    en BD_ITEMS_PROV y las escribe en BD_ITEMS_PENDIENTES.
    No crea mov_inventario ni actualiza precios/stock.
    """
    global _items_pendientes_cache_keys, _bd_mp_sistema_col_cache
    _items_pendientes_cache_keys = None
    _bd_mp_sistema_col_cache = None

    cargar_bd_items_prov()
    lookup_ruc = cargar_lookup_ruc()
    sh = _get_sheet()
    _ensure_bd_items_pendientes_sheet(sh)

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
        cod_prov = lookup_ruc.get(factura["ruc"].strip(), "")
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
                time.sleep(0.35)
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


# ── FLUJO PRINCIPAL ───────────────────────────────────────────
def procesar_facturas(dry_run: bool = False, reprocesar: bool = False):
    if reprocesar and not dry_run:
        print(
            "MODO --reprocesar: se ignoran facturas ya COMPLETA; "
            "puede duplicar mov_inventario, precios y stock en BD."
        )
    xmls = listar_xmls_pendientes()
    print(f"XMLs en Drive: {len(xmls)}")

    cargar_bd_items_prov()

    xmls_parseados = 0
    xmls_saltados = 0
    total_matcheados = 0
    total_warn = 0

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
            continue

        xmls_parseados += 1

        print(f"  Proveedor:  {factura['razon_social']} ({factura['ruc']})")
        print(f"  Factura:    {factura['num_factura']} | {factura['fecha_factura']}")
        print(f"  Total:      ${factura['total_sin_impuesto']}")
        print(f"  Items:      {len(factura['items'])}")

        items_matcheados = 0
        items_warn = 0
        cod_prov_factura = cargar_lookup_ruc().get(factura["ruc"].strip(), "")
        # Acumuladores para batch_update de BD_MP_SISTEMA al final de la factura
        deltas_stock: dict[str, float] = {}   # cod_mp -> cantidad a sumar
        deltas_costo: dict[str, float] = {}   # cod_mp -> nuevo costo_unitario_ref

        for item in factura["items"]:
            print(f"\n  Item: {item['cod_item_xml']} - {item['descripcion_proveedor']}")
            print(f"    cantidad={item['cantidad']} | costo_efectivo={item['costo_efectivo']}")

            item_prov = buscar_item_prov(
                factura["ruc"],
                item["cod_item_xml"],
                item["descripcion_proveedor"],
                factura.get("razon_social", ""),
            )

            if not item_prov:
                items_warn += 1
                print(
                    f"    WARN: no encontrado en BD_ITEMS_PROV | ruc={factura['ruc']} | cod={item['cod_item_xml']} | desc={item['descripcion_proveedor']}"
                )
                registrar_item_pendiente_factura(
                    factura,
                    item,
                    archivo,
                    cod_prov_factura,
                    dry_run=dry_run,
                )
                continue

            items_matcheados += 1
            cod_mp = item_prov.get("cod_mp_sistema", "").strip()
            print(f"    Match: {cod_mp} - {item_prov.get('nombre_mp')}")

            if cod_mp and mov_entrada_factura_linea_ya_registrada(
                factura["num_factura"], cod_mp, item
            ):
                print(
                    "    INFO: esta línea ya tiene ENTRADA en mov_inventario — "
                    "no se duplica precio/mov/stock (útil al cerrar facturas PARCIAL)"
                )
                continue

            if dry_run:
                u_compra = item_prov.get("unidad_compra") or item_prov.get(
                    "unidad_base_sistema", ""
                )
                factor = _safe_float(item_prov.get("factor_conversion", "1") or "1")
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
                time.sleep(1)
                ok = registrar_entrada_inventario(item_prov, item, factura)
                if ok and cod_mp:
                    # Acumular delta stock (en unidades base)
                    factor = _safe_float(item_prov.get("factor_conversion", "1") or "1")
                    cantidad_base = item["cantidad"] * factor
                    deltas_stock[cod_mp] = deltas_stock.get(cod_mp, 0.0) + cantidad_base
                    # Costo: reemplaza con el más reciente de esta factura
                    costo_u = item["costo_efectivo"] / factor if factor else 0
                    deltas_costo[cod_mp] = costo_u

        # ── Actualizar BD_MP_SISTEMA (stock + costo) ──────────
        if not dry_run and (deltas_stock or deltas_costo):
            _flush_mp_sistema(deltas_stock, deltas_costo)

        # ── Registrar en facturas_procesadas ──────────────────
        registrar_factura_procesada(factura, archivo, items_matcheados, items_warn, dry_run)

        total_matcheados += items_matcheados
        total_warn += items_warn

    print(f"\n{'=' * 50}")
    print("Completado.")
    print(
        f"Resumen: XMLs procesados={xmls_parseados} | saltados (COMPLETA)={xmls_saltados} | "
        f"items matcheados={total_matcheados} | WARN sin match={total_warn}"
    )


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


if __name__ == "__main__":
    import sys

    if "--crear-hoja-items-pendientes" in sys.argv:
        print("Creando pestaña de ítems pendientes (si no existe)...")
        crear_hoja_bd_items_pendientes()
        sys.exit(0)

    if "--backfill-items-pendientes" in sys.argv:
        dry = "--dry-run" in sys.argv
        print(
            "Backfill BD_ITEMS_PENDIENTES desde XML en Drive "
            f"({'DRY RUN' if dry else 'escribiendo hoja'})..."
        )
        backfill_items_pendientes_desde_drive(dry_run=dry)
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
