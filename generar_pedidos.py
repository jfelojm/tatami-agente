import math
import os
from datetime import date, datetime
import pytz

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from collections import defaultdict

load_dotenv(override=True)

# ── Constantes ──────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# Nombres a validar (se compara contra nombre_proveedor upper)
PROVEEDORES_PILOTO_TOKENS = {"ITALDELI", "GALABDISTRI", "MARAMAR", "PACHECO", "ELJURI"}


# ── Conexiones ───────────────────────────────────────────────
def conectar_sheets():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def _detectar_header_row_idx(values: list[list[str]], header_key: str) -> int | None:
    for i, row in enumerate(values):
        if any((c or "").strip() == header_key for c in row):
            return i
    return None


# ── Carga de hojas ───────────────────────────────────────────
def cargar_bd_prov(sheet):
    """Retorna dict cod_proveedor → {nombre, lead_time, ventana_pedido, ...}"""
    ws = sheet.worksheet("BD_PROV")
    values = ws.get_all_values()
    header_row_idx = _detectar_header_row_idx(values, "cod_proveedor")
    if header_row_idx is None:
        print("WARN: no se encontro header cod_proveedor en BD_PROV")
        return {}

    headers = [h.strip() for h in values[header_row_idx]]
    rows = values[header_row_idx + 1 :]

    def idx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    col_cod = idx("cod_proveedor")
    col_nombre = idx("razon_social")  # en la hoja BD_PROV el nombre se llama razon_social
    col_lt = idx("lead_time_dias")
    col_ventana = idx("ventana_pedido")
    col_pago = idx("condicion_pago")
    col_freq = idx("frecuencia_entrega_dias")
    col_activo = idx("activo")
    col_inv = idx("proveedor_inventario")
    col_ruc = idx("RUC")  # opcional

    if col_cod is None or col_nombre is None:
        print("WARN: columnas cod_proveedor/razon_social no encontradas en BD_PROV")
        return {}

    proveedores = {}
    for row in rows:
        if not any((c or "").strip() for c in row):
            continue
        cod = (row[col_cod] if col_cod < len(row) else "").strip()
        nombre = (row[col_nombre] if col_nombre < len(row) else "").strip()
        if not cod or not nombre:
            continue

        # Fila de leyenda (p.ej. [FK]) o filas no-datos
        if cod.startswith("["):
            continue

        # Activo
        if col_activo is not None:
            activo = (row[col_activo] if col_activo < len(row) else "").strip().upper()
            if activo == "NO":
                continue

        # Solo proveedores marcados como inventario
        if col_inv is not None:
            inv = (row[col_inv] if col_inv < len(row) else "").strip().upper()
            if inv == "NO":
                continue

        nombre_u = nombre.strip().upper()
        if not any(t in nombre_u for t in PROVEEDORES_PILOTO_TOKENS):
            continue

        lead_time = 1
        if col_lt is not None:
            try:
                lead_time = int((row[col_lt] if col_lt < len(row) else "1") or 1)
            except ValueError:
                lead_time = 1

        freq = 7
        if col_freq is not None:
            try:
                freq = int((row[col_freq] if col_freq < len(row) else "7") or 7)
            except ValueError:
                freq = 7

        proveedores[cod] = {
            "nombre": nombre,
            "ruc": (row[col_ruc] if col_ruc is not None and col_ruc < len(row) else "").strip(),
            "lead_time": lead_time,
            "ventana_pedido": (row[col_ventana] if col_ventana is not None and col_ventana < len(row) else "").strip(),
            "condicion_pago": (row[col_pago] if col_pago is not None and col_pago < len(row) else "").strip(),
            "frecuencia_entrega_dias": freq,
        }

    return proveedores


def cargar_bd_items_prov(sheet):
    """
    Retorna dict cod_mp_sistema -> lista de items proveedor.
    Se lee BD_ITEMS_PROV con headers reales y se salta fila de leyenda.
    """
    ws = sheet.worksheet("BD_ITEMS_PROV")
    values = ws.get_all_values()
    header_row_idx = _detectar_header_row_idx(values, "cod_item_prov")
    if header_row_idx is None:
        print("WARN: no se encontro header cod_item_prov en BD_ITEMS_PROV")
        return {}

    headers = [h.strip() for h in values[header_row_idx]]
    rows = values[header_row_idx + 2 :]  # salta fila [FK][LINK][PK]...

    def idx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    col_cod_mp = idx("cod_mp_sistema")
    col_cod_prov = idx("cod_proveedor")
    col_desc = idx("descripcion_proveedor")
    col_unidad = idx("unidad_compra")
    col_factor = idx("factor_conversion")
    col_activo = idx("activo")

    if col_cod_mp is None or col_cod_prov is None:
        print("WARN: columnas cod_mp_sistema/cod_proveedor no encontradas en BD_ITEMS_PROV")
        return {}

    mapping: dict[str, list[dict]] = {}
    for row in rows:
        if not any((c or "").strip() for c in row):
            continue

        cod_mp = (row[col_cod_mp] if col_cod_mp < len(row) else "").strip()
        cod_prov = (row[col_cod_prov] if col_cod_prov < len(row) else "").strip()
        if not cod_mp or not cod_prov:
            continue

        if cod_mp.startswith("[") or cod_prov.startswith("["):
            continue

        if col_activo is not None:
            activo = (row[col_activo] if col_activo < len(row) else "").strip().upper()
            if activo == "NO":
                continue

        unidad = (row[col_unidad] if col_unidad is not None and col_unidad < len(row) else "").strip()
        factor = 1.0
        if col_factor is not None and col_factor < len(row):
            try:
                factor = float(str(row[col_factor]).replace(",", ".")) if row[col_factor] else 1.0
            except ValueError:
                factor = 1.0

        mapping.setdefault(cod_mp, []).append(
            {
                "cod_proveedor": cod_prov,
                "descripcion_proveedor": (row[col_desc] if col_desc is not None and col_desc < len(row) else "").strip(),
                "unidad_compra": unidad,
                "cantidad_unidad_compra": factor,  # base_por_unidad_compra
            }
        )

    return mapping


def cargar_bd_mp_sistema(sheet):
    """Retorna lista de MPs con stock_actual, par_level, nombre, unidad_base"""
    ws = sheet.worksheet("BD_MP_SISTEMA")
    all_values = ws.get_all_values()
    if len(all_values) < 4:
        return []

    headers = [h.strip() for h in all_values[2]]  # fila 3 = índice 2
    rows = all_values[3:]

    def get(row: list[str], key: str) -> str:
        try:
            i = headers.index(key)
        except ValueError:
            return ""
        return (row[i] if i < len(row) else "").strip()

    mps = []
    for row in rows:
        if not any((c or "").strip() for c in row):
            continue
        cod = get(row, "cod_mp_sistema")
        if not cod:
            continue
        try:
            stock = float(get(row, "stock_actual").replace(",", ".") or "0")
            par = float(get(row, "par_level").replace(",", ".") or "0")
        except ValueError:
            continue

        mps.append(
            {
                "cod_mp_sistema": cod,
                "nombre_mp": get(row, "nombre_mp"),
                "stock_actual": stock,
                "par_level": par,
                "unidad_base": get(row, "unidad_base"),
            }
        )
    return mps


# ── Lógica de ventana ────────────────────────────────────────
DIA_MAP = {
    "LUN": 0,
    "MAR": 1,
    "MIE": 2,
    "JUE": 3,
    "VIE": 4,
    "SAB": 5,
    "DOM": 6,
}


def proveedor_activo_hoy(ventana_pedido: str, hoy: date) -> bool:
    """Verifica si HOY está dentro de la ventana de pedido del proveedor."""
    if not ventana_pedido:
        return True
    dias = [d.strip().upper() for d in ventana_pedido.split(",") if d.strip()]
    return hoy.weekday() in [DIA_MAP[d] for d in dias if d in DIA_MAP]


def saludo_por_hora() -> str:
    tz = pytz.timezone("America/Guayaquil")
    hora = datetime.now(tz).hour
    if hora < 12:
        return "Buenos días"
    if hora < 19:
        return "Buenas tardes"
    return "Buenas noches"


def generar_mensaje_whatsapp(items: list[dict]) -> str:
    lineas = [f"{it['nombre_mp']} {it['unidades_a_pedir']}" for it in items]
    return (
        f"{saludo_por_hora()}, le saludo de Tatami.\n"
        "Necesito hacer el siguiente pedido:\n\n"
        + "\n".join(lineas)
        + "\n\nMuchas gracias."
    )


# ── Generación de pedidos ────────────────────────────────────
def generar_pedidos(dry_run: bool = False):
    # hoy = date.today()
    hoy = date(2026, 5, 5)  # martes - activa ITALDELI, PACHECO, ELJURI
    dia_nombre = [
        "Lunes",
        "Martes",
        "Miercoles",
        "Jueves",
        "Viernes",
        "Sabado",
        "Domingo",
    ][hoy.weekday()]
    print(f"\n{'='*60}")
    print(f"GENERACION DE PEDIDOS - {dia_nombre} {hoy.strftime('%d/%m/%Y')}")
    print(f"{'='*60}\n")

    sheet = conectar_sheets()
    proveedores = cargar_bd_prov(sheet)
    items_prov = cargar_bd_items_prov(sheet)
    mps = cargar_bd_mp_sistema(sheet)

    mps_bajo = [
        mp
        for mp in mps
        if mp["par_level"] > 0 and mp["stock_actual"] < mp["par_level"]
    ]
    print(f"MPs bajo par level: {len(mps_bajo)} de {len(mps)} totales\n")

    # ── DEBUG ────────────────────────────────────────────────────
    print("DEBUG - Proveedores cargados:")
    for cod, p in proveedores.items():
        print(f"  {cod} | {p['nombre']} | ventana: {p['ventana_pedido']}")

    print(f"\nDEBUG - Items en BD_ITEMS_PROV: {len(items_prov)} MPs mapeados")

    sin_mapeo = 0
    con_mapeo_sin_prov = 0
    con_mapeo_fuera_ventana = 0
    con_mapeo_ok = 0

    ejemplos_sin_mapeo: list[str] = []
    ejemplos_sin_prov: list[str] = []

    for mp in mps_bajo:
        cod_mp = mp["cod_mp_sistema"]
        if cod_mp not in items_prov:
            sin_mapeo += 1
            if len(ejemplos_sin_mapeo) < 5:
                ejemplos_sin_mapeo.append(f"{cod_mp} | {mp['nombre_mp']}")
            continue

        for item in items_prov[cod_mp]:
            cod_prov = item["cod_proveedor"]
            if cod_prov not in proveedores:
                con_mapeo_sin_prov += 1
                if len(ejemplos_sin_prov) < 5:
                    ejemplos_sin_prov.append(
                        f"{cod_mp} | {mp['nombre_mp']} -> cod_prov: '{cod_prov}'"
                    )
                continue
            if not proveedor_activo_hoy(proveedores[cod_prov]["ventana_pedido"], hoy):
                con_mapeo_fuera_ventana += 1
                continue
            con_mapeo_ok += 1

    print(f"\nDEBUG - MPs bajo par level ({len(mps_bajo)}):")
    print(f"  Sin entrada en BD_ITEMS_PROV     : {sin_mapeo}")
    print(f"  Mapeados pero proveedor no piloto: {con_mapeo_sin_prov}")
    print(f"  Mapeados pero fuera de ventana   : {con_mapeo_fuera_ventana}")
    print(f"  Listos para generar pedido       : {con_mapeo_ok}")

    print("\nDEBUG - Ejemplos sin mapeo (primeros 5):")
    for s in ejemplos_sin_mapeo:
        print(f"  {s}")

    print("\nDEBUG - Ejemplos con mapeo pero proveedor no en piloto (primeros 5):")
    for s in ejemplos_sin_prov:
        print(f"  {s}")
    # ── FIN DEBUG ─────────────────────────────────────────────────

    pedidos_por_proveedor: dict[str, list[dict]] = {}

    for mp in mps_bajo:
        cod_mp = mp["cod_mp_sistema"]
        cantidad_a_pedir = mp["par_level"] - mp["stock_actual"]

        if cod_mp not in items_prov:
            continue

        for item in items_prov[cod_mp]:
            cod_prov = item["cod_proveedor"]
            if cod_prov not in proveedores:
                continue

            prov_data = proveedores[cod_prov]
            if not proveedor_activo_hoy(prov_data["ventana_pedido"], hoy):
                continue

            cant_uc = float(item.get("cantidad_unidad_compra", 1) or 1)
            unidad_compra = item.get("unidad_compra", "")

            if cant_uc > 0:
                unidades_a_pedir = math.ceil(cantidad_a_pedir / cant_uc)
            else:
                unidades_a_pedir = math.ceil(cantidad_a_pedir)

            pedidos_por_proveedor.setdefault(cod_prov, []).append(
                {
                    "cod_mp_sistema": cod_mp,
                    "nombre_mp": mp["nombre_mp"],
                    "descripcion_proveedor": item["descripcion_proveedor"],
                    "stock_actual": mp["stock_actual"],
                    "par_level": mp["par_level"],
                    "unidad_base": mp["unidad_base"],
                    "cantidad_base": round(cantidad_a_pedir, 2),
                    "unidades_a_pedir": unidades_a_pedir,
                    "unidad_compra": unidad_compra,
                }
            )

    if not pedidos_por_proveedor:
        print("No hay pedidos que generar hoy - stocks sobre par level.")
        return

    def deduplicar_items(items: list[dict]) -> list[dict]:
        """Si el mismo MP aparece 2+ veces, suma unidades y usa la primera descripcion."""
        agrupado: dict[str, dict] = {}
        for it in items:
            key = (it.get("cod_mp_sistema") or "").strip() or it.get("nombre_mp") or ""
            if key not in agrupado:
                agrupado[key] = dict(it)
            else:
                agrupado[key]["unidades_a_pedir"] += it.get("unidades_a_pedir", 0)
                agrupado[key]["cantidad_base"] += it.get("cantidad_base", 0)
        return list(agrupado.values())

    mensajes_generados = []
    for cod_prov, items in pedidos_por_proveedor.items():
        prov = proveedores[cod_prov]
        items = deduplicar_items(items)
        mensaje = generar_mensaje_whatsapp(items)

        mensajes_generados.append(
            {
                "cod_proveedor": cod_prov,
                "nombre": prov["nombre"],
                "n_items": len(items),
                "items": items,
                "mensaje": mensaje,
            }
        )

    print(f"PEDIDOS A GENERAR HOY: {len(mensajes_generados)} proveedor(es)\n")
    print("-" * 60)

    for p in mensajes_generados:
        print(f"\nPROVEEDOR: {p['nombre']} ({p['cod_proveedor']}) - {p['n_items']} item(s)")
        print(f"Condicion pago: {proveedores[p['cod_proveedor']]['condicion_pago']}")
        for it in p["items"]:
            print(f"  {it['nombre_mp']}")
            print(f"    Stock actual: {it['stock_actual']:.0f} {it['unidad_base']}")
            print(f"    Par level:    {it['par_level']:.0f} {it['unidad_base']}")
            print(f"    A pedir:      {it['unidades_a_pedir']} {it['unidad_compra']}")

        print("\nMENSAJE WHATSAPP:")
        for linea in p["mensaje"].split("\n"):
            print(f"  {linea}")
        print("-" * 60)

    print("=" * 60)
    print(f"{'[DRY-RUN] ' if dry_run else ''}Revision completa. Envio manual.")
    print("=" * 60)


if __name__ == "__main__":
    import sys

    dry_run = "--dry-run" in sys.argv
    generar_pedidos(dry_run=dry_run)
