import os
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from gspread.utils import rowcol_to_a1
from supabase import create_client

load_dotenv(override=True)

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
    ruc: str, cod_item_xml: str, descripcion: str = ""
) -> dict | None:
    items = cargar_bd_items_prov()

    lookup_ruc = cargar_lookup_ruc()
    cod_prov = lookup_ruc.get(ruc.strip(), "")

    cod_item_norm = cod_item_xml.strip().lstrip("0")

    for item in items:
        if item.get("cod_proveedor", "").strip() != cod_prov:
            continue
        item_cod = item.get("cod_item_prov", "").strip().lstrip("0")
        if item_cod == cod_item_norm:
            return item

    for item in items:
        item_cod = item.get("cod_item_prov", "").strip().lstrip("0")
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

    umbral = float(os.getenv("UMBRAL_ALERTA_PRECIO", "0.05"))

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
                ]
            )
            print(f"    → precio_ref actualizado en Sheets: {cod_buscar} = {costo_efectivo}")
            return


# ── REGISTRAR ENTRADA EN MOV_INVENTARIO ──────────────────────
def registrar_entrada_inventario(item_prov: dict, item_factura: dict, factura: dict):
    cod_mp = item_prov.get("cod_mp_sistema", "").strip()
    bodega = item_prov.get("cod_bodega_destino", "").strip()
    unidad = item_prov.get("unidad_base_sistema", "").strip()
    factor = _safe_float(item_prov.get("factor_conversion", "1") or "1")
    cantidad_base = item_factura["cantidad"] * factor
    costo_u = item_factura["costo_efectivo"] / factor if factor else 0

    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
    cod_mov = f"MOV-{factura['fecha_factura'].replace('-', '')}-{cod_mp}-{ts}"

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
        "observaciones": item_factura["descripcion_proveedor"],
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


# ── FLUJO PRINCIPAL ───────────────────────────────────────────
def procesar_facturas(dry_run: bool = False):
    xmls = listar_xmls_pendientes()
    print(f"XMLs en Drive: {len(xmls)}")

    cargar_bd_items_prov()

    xmls_parseados = 0
    items_matcheados = 0
    items_warn = 0

    for archivo in xmls:
        print(f"\n{'-' * 50}")
        print(f"Procesando: {archivo['name']}")

        texto = descargar_xml(archivo["id"])
        factura = parsear_xml_sri(texto)

        if not factura:
            print("  ERROR: no se pudo parsear, saltando")
            continue

        xmls_parseados += 1

        print(f"  Proveedor:  {factura['razon_social']} ({factura['ruc']})")
        print(f"  Factura:    {factura['num_factura']} | {factura['fecha_factura']}")
        print(f"  Total:      ${factura['total_sin_impuesto']}")
        print(f"  Items:      {len(factura['items'])}")

        for item in factura["items"]:
            print(f"\n  Item: {item['cod_item_xml']} - {item['descripcion_proveedor']}")
            print(f"    cantidad={item['cantidad']} | costo_efectivo={item['costo_efectivo']}")

            item_prov = buscar_item_prov(
                factura["ruc"], item["cod_item_xml"], item["descripcion_proveedor"]
            )

            if not item_prov:
                items_warn += 1
                print(
                    f"    WARN: no encontrado en BD_ITEMS_PROV | ruc={factura['ruc']} | cod={item['cod_item_xml']} | desc={item['descripcion_proveedor']}"
                )
                continue

            items_matcheados += 1
            print(f"    Match: {item_prov.get('cod_mp_sistema')} - {item_prov.get('nombre_mp')}")

            if dry_run:
                u_compra = item_prov.get("unidad_compra") or item_prov.get(
                    "unidad_base_sistema", ""
                )
                print(
                    f"    [DRY RUN] precio_ref={item_prov.get('precio_ref')} -> nuevo={item['costo_efectivo']}"
                )
                print(
                    f"    [DRY RUN] entrada inventario: {item_prov.get('cod_mp_sistema')} +{item['cantidad']} {u_compra}"
                )
            else:
                procesar_variacion_precio(item_prov, factura, item)
                time.sleep(1)
                registrar_entrada_inventario(item_prov, item, factura)

    print(f"\n{'=' * 50}")
    print("Completado.")
    print(
        f"Resumen: XMLs parseados={xmls_parseados} | "
        f"items matcheados={items_matcheados} | "
        f"WARN mapeo manual={items_warn}"
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

    DRY_RUN = "--dry-run" in sys.argv

    print("=" * 50)
    print(f"MODULO FACTURAS - {'DRY RUN' if DRY_RUN else 'PRODUCCION'}")
    print("=" * 50)
    procesar_facturas(dry_run=DRY_RUN)
