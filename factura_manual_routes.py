"""
Ingreso de facturas manuales desde Google Sheets (Apps Script, botón ACEPTAR).

POST /api/factura_manual/enviar
  Header: X-Tatami-Factura-Secret = FACTURA_SHEETS_INGEST_SECRET (.env)
  Payload:
    {
      "usuario": "mary@...",
      "proveedor": "161 — Sumba Chocho Ana Lucrecia",
      "num_factura": "001-001-000123",
      "fecha_factura": "2026-06-09",
      "idempotency_key": "opcional",
      "lineas": [
        {"descripcion": "Litros de Leche", "cantidad": 10, "costo_unitario": 1.10},
        ...
      ]
    }

Por cada línea resuelve el ítem en BD_ITEMS_PROV del proveedor (match exacto por
descripción — el dropdown del Sheets garantiza el texto) y crea ENTRADA en
mov_inventario con la misma lógica que las facturas XML. Actualiza stock en
BD_MP_SISTEMA y registra en facturas_procesadas (meta.origen = MANUAL_SHEETS).

Respuesta: {"ok": true, "trx": "TRX-20260609-153012", "entradas": 3, ...}
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

PROVEEDORES_PERMITIDOS = {"161", "164", "165"}  # Sumba Chocho, Loja Lasso, Inguil Lazo


def _norm_desc(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def _items_por_descripcion(cod_proveedor: str) -> dict[str, dict]:
    from procesar_facturas_drive import cargar_bd_items_prov

    out: dict[str, dict] = {}
    for it in cargar_bd_items_prov():
        if (it.get("cod_proveedor") or "").strip() != cod_proveedor:
            continue
        if (it.get("activo") or "SI").strip().upper() == "NO":
            continue
        desc = _norm_desc(it.get("descripcion_proveedor") or it.get("descripcion") or "")
        if desc and desc not in out:
            out[desc] = it
    return out


def _ruc_proveedor(cod_proveedor: str) -> str:
    from staging_common import find_header_row, open_master

    ws = open_master().worksheet("BD_PROV")
    vals = ws.get_all_values()
    hi = find_header_row(vals, "cod_proveedor")
    if hi is None:
        return ""
    h = [(c or "").strip() for c in vals[hi]]
    icod = h.index("cod_proveedor")
    iruc = next((h.index(k) for k in ("RUC", "ruc", "ruc_proveedor") if k in h), None)
    for row in vals[hi + 1 :]:
        if (row[icod] if icod < len(row) else "").strip() == cod_proveedor:
            return (row[iruc] if iruc is not None and iruc < len(row) else "").strip()
    return ""


@router.post("/enviar")
async def recibir_factura_manual(request: Request):
    secret = request.headers.get("X-Tatami-Factura-Secret")
    expected = (os.getenv("FACTURA_SHEETS_INGEST_SECRET") or "").strip()
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="No autorizado")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="El cuerpo debe ser un objeto JSON")

    try:
        return _procesar_factura_manual(payload)
    except HTTPException:
        raise
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        print(f"ERROR factura_manual: {tb}")
        raise HTTPException(
            status_code=503,
            detail=f"Error interno factura manual: {type(e).__name__}: {e}",
        ) from e


def _procesar_factura_manual(payload: dict):
    proveedor_raw = str(payload.get("proveedor") or "").strip()
    num_factura = str(payload.get("num_factura") or "").strip()
    fecha_factura = str(payload.get("fecha_factura") or "").strip()[:10]
    usuario = str(payload.get("usuario") or "").strip()
    lineas = payload.get("lineas")
    modo_prueba = bool(payload.get("modo_prueba"))

    m = re.match(r"^(\d+)", proveedor_raw)
    cod_proveedor = m.group(1) if m else ""
    if cod_proveedor not in PROVEEDORES_PERMITIDOS:
        raise HTTPException(
            status_code=400,
            detail=f"Proveedor no habilitado para ingreso manual: '{proveedor_raw}'",
        )
    if not num_factura:
        raise HTTPException(status_code=400, detail="Falta num_factura")
    try:
        datetime.strptime(fecha_factura, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"fecha_factura inválida: '{fecha_factura}'")
    if not isinstance(lineas, list) or not lineas:
        raise HTTPException(status_code=400, detail="lineas debe ser un arreglo no vacío")

    from procesar_facturas_drive import (
        _flush_mp_sistema,
        fecha_factura_permite_ingreso_stock,
        mov_entrada_factura_linea_ya_registrada,
        registrar_entrada_inventario,
        registrar_factura_procesada,
    )
    from bodegas_config import resolver_bodega_entrada_linea

    if not fecha_factura_permite_ingreso_stock(fecha_factura):
        raise HTTPException(
            status_code=400,
            detail=f"Fecha {fecha_factura} anterior al mínimo de ingreso de stock",
        )

    # Dedup: misma factura del mismo proveedor ya COMPLETA
    from supabase import create_client

    sb_url = (os.getenv("SUPABASE_URL") or "").strip()
    sb_key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not sb_url or not sb_key:
        raise HTTPException(status_code=503, detail="Faltan SUPABASE_URL o SUPABASE_KEY en el servidor")
    sb = create_client(sb_url, sb_key)
    ruc = _ruc_proveedor(cod_proveedor) or f"MANUAL-{cod_proveedor}"
    prev = (
        sb.table("facturas_procesadas")
        .select("estado")
        .eq("num_factura", num_factura)
        .eq("ruc_proveedor", ruc)
        .execute()
        .data
        or []
    )
    if prev and prev[0].get("estado") == "COMPLETA":
        raise HTTPException(
            status_code=409,
            detail=f"La factura {num_factura} de proveedor {cod_proveedor} ya fue ingresada",
        )

    catalogo = _items_por_descripcion(cod_proveedor)
    if not catalogo:
        raise HTTPException(
            status_code=400,
            detail=f"Proveedor {cod_proveedor} sin ítems activos en BD_ITEMS_PROV",
        )

    if modo_prueba:
        # Valida y muestra el resultado sin tocar inventario ni registros
        resultados = []
        ok_count = 0
        err_count = 0
        for ln in lineas:
            desc = str(ln.get("descripcion") or "").strip()
            try:
                cantidad = float(ln.get("cantidad") or 0)
                costo_u = float(ln.get("costo_unitario") or 0)
            except (TypeError, ValueError):
                cantidad, costo_u = 0.0, -1.0
            res = {"descripcion": desc, "cantidad": cantidad, "costo_unitario": costo_u}
            item_prov = catalogo.get(_norm_desc(desc))
            if not item_prov:
                res["estado"] = "SIN_CATALOGO"
                err_count += 1
            elif cantidad <= 0 or costo_u < 0:
                res["estado"] = "DATOS_INVALIDOS"
                err_count += 1
            else:
                factor = float(
                    str(item_prov.get("factor_conversion") or "1").replace(",", ".") or 1
                )
                res["cod_mp_sistema"] = (item_prov.get("cod_mp_sistema") or "").strip()
                res["entraria_stock"] = round(cantidad * factor, 2)
                res["unidad_base"] = (item_prov.get("unidad_base_sistema") or "").strip()
                res["estado"] = "OK_PRUEBA"
                ok_count += 1
            resultados.append(res)
        return {
            "ok": err_count == 0,
            "trx": "PRUEBA — nada se ingresó",
            "modo_prueba": True,
            "num_factura": num_factura,
            "cod_proveedor": cod_proveedor,
            "entradas": ok_count,
            "errores": err_count,
            "lineas": resultados,
        }

    trx = "TRX-" + datetime.now().strftime("%Y%m%d-%H%M%S")

    factura = {
        "fecha_factura": fecha_factura,
        "num_factura": num_factura,
        "ruc": ruc,
        "_meta": {
            "origen": "MANUAL_SHEETS",
            "trx": trx,
            "cod_proveedor": cod_proveedor,
            "usuario": usuario,
        },
    }

    resultados: list[dict] = []
    deltas_stock: dict[tuple[str, str], float] = {}
    ok_count = 0
    err_count = 0

    for ln in lineas:
        desc = str(ln.get("descripcion") or "").strip()
        try:
            cantidad = float(ln.get("cantidad") or 0)
            costo_u = float(ln.get("costo_unitario") or 0)
        except (TypeError, ValueError):
            cantidad, costo_u = 0.0, -1.0

        res = {"descripcion": desc, "cantidad": cantidad, "costo_unitario": costo_u}
        item_prov = catalogo.get(_norm_desc(desc))
        if not item_prov:
            res["estado"] = "SIN_CATALOGO"
            err_count += 1
            resultados.append(res)
            continue
        if cantidad <= 0 or costo_u < 0:
            res["estado"] = "DATOS_INVALIDOS"
            err_count += 1
            resultados.append(res)
            continue

        item_factura = {
            "cod_item_xml": f"{trx}:{_norm_desc(desc)[:40]}",
            "descripcion_proveedor": desc,
            "cantidad": cantidad,
            "costo_efectivo": costo_u,
            "precio_total_sin_impuesto": round(cantidad * costo_u, 4),
        }
        res["cod_mp_sistema"] = (item_prov.get("cod_mp_sistema") or "").strip()

        if mov_entrada_factura_linea_ya_registrada(
            num_factura, res["cod_mp_sistema"], item_factura
        ):
            res["estado"] = "YA_INGRESADA"
            ok_count += 1
            resultados.append(res)
            continue

        if registrar_entrada_inventario(item_prov, item_factura, factura):
            res["estado"] = "OK"
            ok_count += 1
            bodega, _err = resolver_bodega_entrada_linea(item_prov)
            factor = float(str(item_prov.get("factor_conversion") or "1").replace(",", ".") or 1)
            if bodega:
                key = (res["cod_mp_sistema"], bodega)
                deltas_stock[key] = deltas_stock.get(key, 0.0) + cantidad * factor
        else:
            res["estado"] = "ERROR_INGRESO"
            err_count += 1
        resultados.append(res)

    if deltas_stock:
        try:
            _flush_mp_sistema(deltas_stock, {})
        except Exception as e:
            print(f"WARN factura_manual: stock Sheets no actualizado: {e}")

    registrar_factura_procesada(factura, {"id": ""}, ok_count, err_count, dry_run=False)

    return {
        "ok": err_count == 0,
        "trx": trx,
        "num_factura": num_factura,
        "cod_proveedor": cod_proveedor,
        "entradas": ok_count,
        "errores": err_count,
        "lineas": resultados,
    }
