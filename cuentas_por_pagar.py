"""
Cuentas por pagar: sincroniza facturas recibidas (SRI + manuales) a cxp_facturas.

- Total con IVA y plazo salen del XML autorizado (sri_comprobantes_recibidos).
- Facturas manuales sin XML: total = suma de líneas en mov_inventario (sin IVA, total_estimado).
- Días de crédito: BD_PROV.dias_credito / condicion_pago; si no hay, plazo del XML; si no, contado.
- Facturas con fecha anterior a CXP_FECHA_CORTE entran como PAGADA (saldo inicial).
- Nunca pisa el estado de pago ni los datos de pago de una factura ya registrada.

Uso:
  python cuentas_por_pagar.py --dry-run
  python cuentas_por_pagar.py --produccion
  python cuentas_por_pagar.py --produccion --desde 2026-09-01
"""

from __future__ import annotations

import argparse
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv(override=True)

FECHA_CORTE = os.getenv("CXP_FECHA_CORTE", "2026-10-01")
DESDE_DEFAULT = "2026-01-01"
PLAZO_XML_MAX = 120
TABLA = "cxp_facturas"
TABLA_HIST = "cxp_historial"


def _supabase():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _fecha_iso(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _txt(el, tag: str) -> str:
    if el is None:
        return ""
    x = el.find(f".//{tag}")
    return (x.text or "").strip() if x is not None and x.text else ""


def parsear_totales_xml(xml: str) -> dict | None:
    """Cabecera financiera del XML SRI: subtotal, IVA, total, forma de pago y plazo."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    comp = root.find(".//comprobante")
    if comp is None or not comp.text:
        factura = root if root.find(".//infoFactura") is not None else None
    else:
        try:
            factura = ET.fromstring(comp.text.strip())
        except ET.ParseError:
            return None
    if factura is None:
        return None
    it = factura.find(".//infoTributaria")
    inf = factura.find(".//infoFactura")
    if it is None or inf is None:
        return None

    iva = 0.0
    for ti in inf.findall(".//totalConImpuestos/totalImpuesto"):
        if _txt(ti, "codigo") == "2":
            iva += _num(_txt(ti, "valor")) or 0.0

    plazo = None
    forma = ""
    pago = inf.find(".//pagos/pago")
    if pago is not None:
        forma = _txt(pago, "formaPago")
        p = _num(_txt(pago, "plazo"))
        unidad = _txt(pago, "unidadTiempo").lower()
        if p is not None:
            if unidad.startswith("mes"):
                p *= 30
            elif unidad.startswith("a"):
                p *= 365
            plazo = int(round(p))

    return {
        "num_factura": f"{_txt(it, 'estab')}-{_txt(it, 'ptoEmi')}-{_txt(it, 'secuencial')}",
        "ruc": _txt(it, "ruc"),
        "razon_social": _txt(it, "razonSocial"),
        "fecha_emision": _fecha_iso(_txt(inf, "fechaEmision")),
        "subtotal": round(_num(_txt(inf, "totalSinImpuestos")) or 0.0, 2),
        "iva": round(iva, 2),
        "total": round(_num(_txt(inf, "importeTotal")) or 0.0, 2),
        "forma_pago_sri": forma,
        "plazo_xml": plazo,
    }


def items_xml(xml: str) -> list[dict]:
    try:
        root = ET.fromstring(xml)
        comp = root.find(".//comprobante")
        factura = ET.fromstring(comp.text.strip()) if comp is not None and comp.text else root
    except ET.ParseError:
        return []
    out = []
    for d in factura.findall(".//detalles/detalle"):
        iva = 0.0
        tarifa = None
        for imp in d.findall(".//impuestos/impuesto"):
            if _txt(imp, "codigo") == "2":
                iva += _num(_txt(imp, "valor")) or 0.0
                tarifa = _num(_txt(imp, "tarifa"))
        out.append(
            {
                "codigo": _txt(d, "codigoPrincipal") or _txt(d, "codigoInterno"),
                "descripcion": _txt(d, "descripcion"),
                "cantidad": _num(_txt(d, "cantidad")) or 0.0,
                "precio_unitario": _num(_txt(d, "precioUnitario")) or 0.0,
                "descuento": _num(_txt(d, "descuento")) or 0.0,
                "subtotal": round(_num(_txt(d, "precioTotalSinImpuesto")) or 0.0, 2),
                "iva": round(iva, 2),
                "tarifa_iva": tarifa,
            }
        )
    return out


def detalle_factura(sb, fila: dict) -> dict:
    """Ítems de la factura (XML SRI si existe; si no, líneas ingresadas a inventario) + cruce con inventario."""
    num, ruc = fila["num_factura"], fila["ruc_proveedor"]
    fp = (
        sb.table("facturas_procesadas").select("estado,meta").eq("num_factura", num)
        .eq("ruc_proveedor", ruc).limit(1).execute().data or []
    )
    estado_inv = fp[0]["estado"] if fp else None
    trx = str(((fp[0].get("meta") or {}) if fp else {}).get("trx") or "")

    xml = ""
    q = sb.table("sri_comprobantes_recibidos").select("xml_autorizado")
    q = q.eq("clave_acceso", fila["clave_acceso"]) if fila.get("clave_acceso") else q.eq("num_factura", num).eq("ruc_emisor", ruc)
    r = q.limit(1).execute().data or []
    if r:
        xml = r[0].get("xml_autorizado") or ""

    mq = (
        sb.table("mov_inventario")
        .select("cod_mp_sistema,nombre_mp,cantidad_mov,unidad_base,costo_total,cod_bodega_destino,observaciones,fecha")
        .eq("num_documento", num).eq("tipo_mov", "ENTRADA")
    )
    if trx:
        mq = mq.ilike("observaciones", f"%{trx}%")
    movs = mq.execute().data or []
    if not trx and fila.get("fecha_emision"):
        mismos = [m for m in movs if (m.get("fecha") or "")[:10] == fila["fecha_emision"]]
        movs = mismos or movs

    def _cod_mov(m: dict) -> str:
        mt = re.search(r"ITEM_XML:([^|]+)", m.get("observaciones") or "")
        return mt.group(1).strip() if mt else ""

    def _norm_cod(c: str) -> str:
        c = (c or "").strip().upper()
        return c.lstrip("0") or c

    def _ingreso(m: dict) -> dict:
        return {
            "cod_mp": m.get("cod_mp_sistema"), "nombre_mp": m.get("nombre_mp"),
            "cantidad": m.get("cantidad_mov"), "unidad": m.get("unidad_base"),
            "bodega": m.get("cod_bodega_destino"), "costo_total": m.get("costo_total"),
        }

    if xml:
        items = items_xml(xml)
        por_cod: dict[str, list[dict]] = {}
        for m in movs:
            por_cod.setdefault(_norm_cod(_cod_mov(m)), []).append(m)
        usados = set()
        for it in items:
            cod = _norm_cod(it["codigo"])
            it["ingresos"] = [] if cod in usados else [_ingreso(m) for m in por_cod.get(cod, [])]
            usados.add(cod)
        fuente = "XML"
    elif movs:
        items = [
            {
                "codigo": _cod_mov(m).split(":")[-1] if _cod_mov(m) else "",
                "descripcion": (m.get("observaciones") or "").split("|")[0].strip(),
                "cantidad": None, "precio_unitario": None, "descuento": 0.0,
                "subtotal": round(float(m.get("costo_total") or 0), 2), "iva": None, "tarifa_iva": None,
                "ingresos": [_ingreso(m)],
            }
            for m in movs
        ]
        fuente = "INVENTARIO"
    else:
        items, fuente = [], "NINGUNO"

    return {
        "fuente": fuente,
        "estado_inventario": estado_inv,
        "items": items,
        "sin_ingreso": sum(1 for it in items if not it.get("ingresos")),
    }


def cargar_credito_proveedores() -> dict[str, dict]:
    """RUC → {cod_proveedor, razon_social, condicion_pago, dias_credito (int|None)}."""
    from procesar_facturas_drive import _get_sheet

    vals = _get_sheet().worksheet("BD_PROV").get_all_values()
    hi = next(
        (i for i, r in enumerate(vals) if "cod_proveedor" in [c.strip() for c in r]), None
    )
    if hi is None:
        return {}
    h = [c.strip() for c in vals[hi]]
    out: dict[str, dict] = {}
    for row in vals[hi + 1 :]:
        r = {h[j]: (row[j].strip() if j < len(row) else "") for j in range(len(h))}
        ruc = re.sub(r"\D", "", r.get("RUC") or r.get("ruc") or "")
        if not ruc:
            continue
        dias = _num(r.get("dias_credito"))
        info = {
            "cod_proveedor": (r.get("cod_proveedor") or "").strip(),
            "razon_social": (r.get("razon_social") or "").strip(),
            "condicion_pago": (r.get("condicion_pago") or "").strip().lower(),
            "dias_credito": int(dias) if dias is not None else None,
        }
        prev = out.get(ruc)
        if prev is None or (prev["dias_credito"] is None and info["dias_credito"] is not None):
            out[ruc] = info
    return out


def resolver_credito(ruc: str, plazo_xml: int | None, proveedores: dict) -> tuple[int, str]:
    prov = proveedores.get(re.sub(r"\D", "", ruc or ""))
    if prov:
        if prov["dias_credito"] is not None:
            return max(prov["dias_credito"], 0), "BD_PROV"
        if prov["condicion_pago"] == "contado":
            return 0, "BD_PROV"
    if plazo_xml and 0 < plazo_xml <= PLAZO_XML_MAX:
        return plazo_xml, "XML"
    return 0, "SIN_DATO"


def _vencimiento(fecha_emision: str | None, dias: int) -> str | None:
    if not fecha_emision:
        return None
    return (date.fromisoformat(fecha_emision) + timedelta(days=dias)).isoformat()


def _paginar(query_fn, page: int = 500) -> list[dict]:
    out: list[dict] = []
    off = 0
    while True:
        b = query_fn().range(off, off + page - 1).execute().data or []
        out.extend(b)
        if len(b) < page:
            return out
        off += page


def _fuentes_sri(sb, desde: str) -> list[dict]:
    rows = _paginar(
        lambda: sb.table("sri_comprobantes_recibidos")
        .select("clave_acceso,num_factura,ruc_emisor,razon_social,fecha_emision,xml_autorizado")
        .gte("fecha_emision", desde)
        .order("clave_acceso"),
        page=200,
    )
    out = []
    for r in rows:
        xml = r.get("xml_autorizado") or ""
        if not xml.strip():
            continue
        t = parsear_totales_xml(xml)
        if not t:
            print(f"  WARN XML ilegible {r['clave_acceso'][:12]}")
            continue
        out.append(
            {
                **t,
                "num_factura": r.get("num_factura") or t["num_factura"],
                "ruc": r.get("ruc_emisor") or t["ruc"],
                "razon_social": t["razon_social"] or r.get("razon_social") or "",
                "fecha_emision": t["fecha_emision"] or r.get("fecha_emision"),
                "clave_acceso": r["clave_acceso"],
                "origen": "SRI",
                "total_estimado": False,
            }
        )
    return out


def subtotal_desde_movimientos(sb, num_factura: str, trx: str = "", fecha: str = "") -> float | None:
    q = (
        sb.table("mov_inventario")
        .select("costo_total,fecha")
        .eq("num_documento", num_factura)
        .eq("tipo_mov", "ENTRADA")
    )
    if trx:
        q = q.ilike("observaciones", f"%{trx}%")
    rows = q.execute().data or []
    if fecha and not trx:
        rows = [r for r in rows if (r.get("fecha") or "")[:10] == fecha]
    if not rows:
        return None
    return round(sum(float(r.get("costo_total") or 0) for r in rows), 2)


def _fuentes_manuales(sb, desde: str, claves_sri: set[tuple[str, str]]) -> list[dict]:
    rows = _paginar(
        lambda: sb.table("facturas_procesadas")
        .select("num_factura,ruc_proveedor,fecha_factura,meta")
        .gte("fecha_factura", desde)
        .order("num_factura")
    )
    out = []
    for r in rows:
        key = (r["num_factura"], r["ruc_proveedor"])
        if key in claves_sri:
            continue
        meta = r.get("meta") or {}
        fecha = (r.get("fecha_factura") or "")[:10] or None
        subtotal = _num(meta.get("total_sin_impuesto_xml"))
        if subtotal is None and fecha and fecha >= FECHA_CORTE:
            subtotal = subtotal_desde_movimientos(
                sb, r["num_factura"], str(meta.get("trx") or ""), fecha
            )
        out.append(
            {
                "num_factura": r["num_factura"],
                "ruc": r["ruc_proveedor"],
                "razon_social": (meta.get("razon_social") or "").strip(),
                "fecha_emision": fecha,
                "subtotal": subtotal,
                "iva": None,
                "total": subtotal,
                "forma_pago_sri": None,
                "plazo_xml": None,
                "clave_acceso": meta.get("clave_acceso"),
                "origen": "MANUAL" if meta.get("origen") == "MANUAL_SHEETS" else "SRI",
                "total_estimado": True,
                "cod_proveedor": meta.get("cod_proveedor") or None,
            }
        )
    return out


def _registro_nuevo(f: dict, proveedores: dict) -> dict:
    dias, fuente = resolver_credito(f["ruc"], f.get("plazo_xml"), proveedores)
    prov = proveedores.get(re.sub(r"\D", "", f["ruc"] or "")) or {}
    rec = {
        "num_factura": f["num_factura"],
        "ruc_proveedor": f["ruc"],
        "razon_social": f.get("razon_social") or prov.get("razon_social") or "",
        "cod_proveedor": prov.get("cod_proveedor") or f.get("cod_proveedor") or None,
        "clave_acceso": f.get("clave_acceso"),
        "fecha_emision": f.get("fecha_emision"),
        "subtotal": f.get("subtotal"),
        "iva": f.get("iva"),
        "total": f.get("total"),
        "total_estimado": bool(f.get("total_estimado")),
        "forma_pago_sri": f.get("forma_pago_sri"),
        "plazo_xml": f.get("plazo_xml"),
        "dias_credito": dias,
        "fuente_credito": fuente,
        "fecha_vencimiento": _vencimiento(f.get("fecha_emision"), dias),
        "origen": f.get("origen") or "SRI",
        "estado_pago": "PENDIENTE",
        "pagado_por": None,
        "notas": None,
    }
    if (f.get("fecha_emision") or "9999") < FECHA_CORTE:
        rec.update(
            {
                "estado_pago": "PAGADA",
                "pagado_por": "CORTE_INICIAL",
                "notas": f"Saldo inicial: factura anterior al corte {FECHA_CORTE}",
            }
        )
    return rec


_CAMPOS_MONTO = (
    "razon_social", "cod_proveedor", "clave_acceso", "fecha_emision", "subtotal",
    "iva", "total", "total_estimado", "forma_pago_sri", "plazo_xml",
)
_CAMPOS_CREDITO = ("dias_credito", "fuente_credito", "fecha_vencimiento")


def _cambios(actual: dict, nuevo: dict) -> dict:
    campos = list(_CAMPOS_MONTO)
    if actual.get("estado_pago") == "PENDIENTE" and actual.get("fuente_credito") != "MANUAL":
        campos += _CAMPOS_CREDITO
    out = {}
    for c in campos:
        a, n = actual.get(c), nuevo.get(c)
        if n is None and c not in ("cod_proveedor",):
            continue
        if isinstance(a, (int, float)) or isinstance(n, (int, float)):
            if a is not None and n is not None and abs(float(a) - float(n)) < 0.005:
                continue
        elif str(a or "") == str(n or ""):
            continue
        out[c] = n
    return out


def _cambio_credito(act: dict, proveedores: dict) -> dict:
    """Recalcula crédito de una factura pendiente ya registrada (fuera de la ventana de fuentes)."""
    if act.get("estado_pago") != "PENDIENTE" or act.get("fuente_credito") == "MANUAL":
        return {}
    dias, fuente = resolver_credito(act["ruc_proveedor"], act.get("plazo_xml"), proveedores)
    venc = _vencimiento(act.get("fecha_emision"), dias)
    if dias == act.get("dias_credito") and fuente == act.get("fuente_credito") and venc == act.get("fecha_vencimiento"):
        return {}
    return {"dias_credito": dias, "fuente_credito": fuente, "fecha_vencimiento": venc}


def actualizar_credito_bd_prov(ruc: str, dias: int, razon_social: str = "", usuario: str = "") -> dict:
    """Escribe dias_credito/condicion_pago en BD_PROV para todas las filas con ese RUC; si no existe, la crea."""
    from gspread.utils import rowcol_to_a1

    from procesar_facturas_drive import _get_sheet

    ws = _get_sheet().worksheet("BD_PROV")
    vals = ws.get_all_values()
    hi = next(i for i, r in enumerate(vals) if "cod_proveedor" in [c.strip() for c in r])
    h = [c.strip() for c in vals[hi]]
    col = {k: h.index(k) for k in h if k}
    col_ruc = col.get("RUC", col.get("ruc"))
    ruc_n = re.sub(r"\D", "", ruc or "")
    condicion = "contado" if dias == 0 else "credito"

    updates, filas = [], []
    for i, row in enumerate(vals[hi + 1 :], start=hi + 2):
        if col_ruc is not None and col_ruc < len(row) and re.sub(r"\D", "", row[col_ruc]) == ruc_n:
            filas.append({"fila": i, "proveedor": row[col["razon_social"]] if col.get("razon_social", 99) < len(row) else ""})
            updates.append({"range": rowcol_to_a1(i, col["dias_credito"] + 1), "values": [[dias]]})
            updates.append({"range": rowcol_to_a1(i, col["condicion_pago"] + 1), "values": [[condicion]]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        return {"accion": "ACTUALIZADO", "filas": filas}

    cods = [vals[j][col["cod_proveedor"]] for j in range(hi + 1, len(vals)) if col["cod_proveedor"] < len(vals[j])]
    nums = [int(c) for c in cods if str(c).strip().isdigit()]
    nuevo_cod = str((max(nums) if nums else 0) + 1).zfill(3)
    fila = [""] * len(h)
    valores = {
        "razon_social": razon_social or ruc_n,
        "cod_proveedor": nuevo_cod,
        "RUC": ruc_n,
        "ruc": ruc_n,
        "proveedor_inventario": "NO",
        "activo": "SI",
        "condicion_pago": condicion,
        "dias_credito": dias,
        "observaciones": f"Alta desde control de pagos ({usuario})" if usuario else "Alta desde control de pagos",
    }
    for k, v in valores.items():
        if k in col:
            fila[col[k]] = v
    ws.append_row(fila, value_input_option="USER_ENTERED", table_range=rowcol_to_a1(hi + 1, 1))
    return {"accion": "CREADO", "cod_proveedor": nuevo_cod, "filas": []}


def aplicar_credito_proveedor(sb, ruc: str, dias: int) -> list[str]:
    """Aplica días de crédito del proveedor a sus facturas pendientes (salvo ediciones manuales por factura)."""
    pend = (
        sb.table(TABLA).select("id,fecha_emision,dias_credito,fecha_vencimiento,fuente_credito")
        .eq("ruc_proveedor", ruc).eq("estado_pago", "PENDIENTE").neq("fuente_credito", "MANUAL")
        .execute().data or []
    )
    ahora = datetime.now().isoformat()
    cambiadas = []
    for r in pend:
        venc = _vencimiento(r.get("fecha_emision"), dias)
        if r.get("dias_credito") == dias and r.get("fecha_vencimiento") == venc and r.get("fuente_credito") == "BD_PROV":
            continue
        sb.table(TABLA).update(
            {"dias_credito": dias, "fuente_credito": "BD_PROV", "fecha_vencimiento": venc, "actualizado_at": ahora}
        ).eq("id", r["id"]).execute()
        cambiadas.append(r["id"])
    return cambiadas


def sincronizar(desde: str = DESDE_DEFAULT, dry_run: bool = True, sb=None, proveedores=None) -> dict:
    sb = sb or _supabase()
    proveedores = proveedores if proveedores is not None else cargar_credito_proveedores()

    sri = _fuentes_sri(sb, desde)
    claves_sri = {(f["num_factura"], f["ruc"]) for f in sri}
    manuales = _fuentes_manuales(sb, desde, claves_sri)
    fuentes = sri + manuales

    try:
        actuales = _paginar(lambda: sb.table(TABLA).select("*").order("id"))
    except Exception as e:
        if not dry_run:
            raise
        print(f"  WARN {TABLA} no disponible ({e}); dry-run asume tabla vacía")
        actuales = []
    existentes = {(r["num_factura"], r["ruc_proveedor"]): r for r in actuales}

    nuevos: list[dict] = []
    updates: list[tuple[str, dict]] = []
    for f in fuentes:
        rec = _registro_nuevo(f, proveedores)
        act = existentes.get((rec["num_factura"], rec["ruc_proveedor"]))
        if act is None:
            nuevos.append(rec)
        else:
            ch = _cambios(act, rec)
            if ch:
                updates.append((act["id"], ch))

    vistos = {(f["num_factura"], f["ruc"]) for f in fuentes}
    for act in actuales:
        if (act["num_factura"], act["ruc_proveedor"]) in vistos:
            continue
        ch = _cambio_credito(act, proveedores)
        if ch:
            updates.append((act["id"], ch))

    if not dry_run:
        for i in range(0, len(nuevos), 200):
            sb.table(TABLA).insert(nuevos[i : i + 200]).execute()
        ahora = datetime.now().isoformat()
        for fid, ch in updates:
            sb.table(TABLA).update({**ch, "actualizado_at": ahora}).eq("id", fid).execute()

    return {
        "fuentes_sri": len(sri),
        "fuentes_manuales": len(manuales),
        "nuevos": nuevos,
        "actualizados": len(updates),
        "dry_run": dry_run,
    }


def registrar_factura_manual(
    num_factura: str,
    ruc: str,
    fecha_emision: str,
    subtotal: float,
    cod_proveedor: str = "",
    usuario: str = "",
) -> None:
    """Alta inmediata desde el ingreso manual de facturas (sin XML)."""
    sb = _supabase()
    prev = (
        sb.table(TABLA).select("id").eq("num_factura", num_factura)
        .eq("ruc_proveedor", ruc).limit(1).execute().data
    )
    if prev:
        return
    rec = _registro_nuevo(
        {
            "num_factura": num_factura,
            "ruc": ruc,
            "fecha_emision": _fecha_iso(fecha_emision),
            "subtotal": round(subtotal, 2),
            "total": round(subtotal, 2),
            "total_estimado": True,
            "origen": "MANUAL",
            "cod_proveedor": cod_proveedor or None,
        },
        cargar_credito_proveedores(),
    )
    ins = sb.table(TABLA).insert(rec).execute().data or []
    if ins and usuario:
        sb.table(TABLA_HIST).insert(
            {"factura_id": ins[0]["id"], "accion": "ALTA_MANUAL", "usuario": usuario}
        ).execute()


def _imprimir_resumen(res: dict) -> None:
    from collections import Counter

    nuevos = res["nuevos"]
    print(f"Fuentes: {res['fuentes_sri']} XML SRI + {res['fuentes_manuales']} sin XML")
    print(f"Nuevos: {len(nuevos)} | Actualizados: {res['actualizados']}")
    if not nuevos:
        return
    print("Estado:", dict(Counter(n.get("estado_pago", "PENDIENTE") for n in nuevos)))
    pend = [n for n in nuevos if n.get("estado_pago", "PENDIENTE") == "PENDIENTE"]
    print(f"Pendientes (desde {FECHA_CORTE}): {len(pend)}  total ${sum(n['total'] or 0 for n in pend):,.2f}")
    print("Fuente crédito (pendientes):", dict(Counter(n["fuente_credito"] for n in pend)))
    sin_total = [n for n in pend if not n.get("total")]
    if sin_total:
        print(f"Pendientes sin total: {len(sin_total)}")
        for n in sin_total:
            print(f"  {n['num_factura']} {n['ruc_proveedor']} {n['razon_social']}")
    hoy = date.today().isoformat()
    venc = [n for n in pend if (n.get("fecha_vencimiento") or "9999") < hoy]
    print(f"Ya vencidas hoy: {len(venc)}  ${sum(n['total'] or 0 for n in venc):,.2f}")
    por_prov = Counter()
    for n in pend:
        por_prov[n["razon_social"] or n["ruc_proveedor"]] += n["total"] or 0
    print("Top proveedores pendientes:")
    for p, t in por_prov.most_common(12):
        print(f"  ${t:>10,.2f}  {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--produccion", action="store_true")
    ap.add_argument("--desde", default=DESDE_DEFAULT)
    a = ap.parse_args()
    res = sincronizar(desde=a.desde, dry_run=not a.produccion)
    _imprimir_resumen(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
